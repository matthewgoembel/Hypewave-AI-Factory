"""
Near-zero latency trade detector via Polygon WebSocket.

Subscribes to Polymarket's CLOB contract OrderFilled events filtered to the
watched whale's address. Fires a callback the moment a trade lands on-chain
(~0.5–2 s) vs the REST data-API path (4–8 s).

Requires: pip install websockets web3
Requires: ALCHEMY_POLYGON_WS in .env
  e.g.  wss://polygon-mainnet.g.alchemy.com/v2/<YOUR_KEY>
  or    wss://polygon-mainnet.quiknode.pro/<YOUR_KEY>/
"""

import asyncio
import json
import logging
import os
import time
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

import requests
import websockets
from web3 import Web3

log = logging.getLogger("mempool_watcher")

# ── Polymarket CLOB contracts on Polygon (mainnet) ────────────────────────────
CLOB_EXCHANGE     = Web3.to_checksum_address("0x4bFb41d5b3570DEFd03c39A9a4D8de6Bd8b8982e")
NEG_RISK_EXCHANGE = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")

# Compute topic hash at import time — avoids hardcoding a hex string that could be wrong
# OrderFilled(bytes32 orderHash, address maker, address taker,
#             uint256 makerAssetId, uint256 takerAssetId,
#             uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee)
ORDER_FILLED_SIG   = "OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
ORDER_FILLED_TOPIC = "0x" + Web3.keccak(text=ORDER_FILLED_SIG).hex()

USDC_ASSET_ID = 0   # asset ID 0 = USDC/collateral in CTF Exchange

# Market cache refresh interval (seconds) — BTC/ETH markets roll every 5 min
CACHE_REFRESH_S = 180


def _pad_address(addr: str) -> str:
    """Left-pad an Ethereum address to 32 bytes for topic filtering."""
    clean = addr.lower().replace("0x", "")
    return "0x" + clean.zfill(64)


class MempoolWatcher:
    """
    Watches Polymarket CLOB contract OrderFilled events in real-time.

    Detection path:
        Trade confirmed in block  →  log event fires  →  callback  →  signal queued
    Typical latency: 200–1000 ms  (vs 4–8 s for REST polling)
    """

    def __init__(self, whale_address: str, on_trade: Callable, ws_url: str = None):
        self.whale    = Web3.to_checksum_address(whale_address)
        self.on_trade = on_trade
        self.ws_url   = ws_url or os.getenv("ALCHEMY_POLYGON_WS", "")
        self._running = False

        # token_id (str) → { market_id, title, outcome, outcome_idx }
        self._market_cache: dict = {}
        self._cache_built_at: float = 0.0

        if not self.ws_url:
            raise ValueError(
                "ALCHEMY_POLYGON_WS not set in .env — "
                "get a free key at alchemy.com and add:\n"
                "ALCHEMY_POLYGON_WS=wss://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY"
            )

    # ── Market metadata cache ──────────────────────────────────────────────────

    def _refresh_market_cache(self, force: bool = False):
        """Fetch active markets and build token_id → metadata map."""
        if not force and (time.time() - self._cache_built_at) < CACHE_REFRESH_S:
            return
        count = 0
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"limit": 500, "closed": "false"},
                timeout=8,
            )
            r.raise_for_status()
            markets = r.json()
            if not isinstance(markets, list):
                markets = markets.get("data", []) if isinstance(markets, dict) else []

            for market in markets:
                cid      = market.get("conditionId", "")
                title    = market.get("question", market.get("title", ""))
                outcomes = market.get("outcomes", [])
                if isinstance(outcomes, str):
                    outcomes = [o.strip() for o in outcomes.split(",")]

                # Primary: clobTokenIds list (standard gamma API field)
                for i, tid in enumerate(market.get("clobTokenIds") or []):
                    if tid:
                        self._market_cache[str(tid)] = {
                            "market_id":   cid,
                            "title":       title,
                            "outcome":     outcomes[i] if i < len(outcomes) else "",
                            "outcome_idx": i,
                        }
                        count += 1

                # Fallback: tokens array (older format)
                for token in (market.get("tokens") or []):
                    tid = str(token.get("token_id", ""))
                    if tid and tid not in self._market_cache:
                        self._market_cache[tid] = {
                            "market_id":   cid,
                            "title":       title,
                            "outcome":     token.get("outcome", ""),
                            "outcome_idx": token.get("outcome_index", -1),
                        }
                        count += 1

            self._cache_built_at = time.time()
            log.info(f"[MempoolWatcher] Market cache: {count} tokens")
            print(f"[Mempool] Market cache: {count} tokens")
        except Exception as e:
            log.warning(f"[MempoolWatcher] Market cache failed: {e}")
            print(f"[Mempool] Market cache failed ({e})")

    def _lookup_token(self, token_id: str) -> dict:
        """Single-token fallback lookup when cache misses."""
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"clob_token_ids": token_id},
                timeout=5,
            )
            r.raise_for_status()
            data    = r.json()
            markets = data if isinstance(data, list) else data.get("data", [])
            for market in markets:
                cid      = market.get("conditionId", "")
                title    = market.get("question", market.get("title", ""))
                outcomes = market.get("outcomes", [])
                if isinstance(outcomes, str):
                    outcomes = [o.strip() for o in outcomes.split(",")]
                for i, tid in enumerate(market.get("clobTokenIds") or []):
                    meta = {
                        "market_id":   cid,
                        "title":       title,
                        "outcome":     outcomes[i] if i < len(outcomes) else "",
                        "outcome_idx": i,
                    }
                    self._market_cache[str(tid)] = meta
                    if str(tid) == token_id:
                        return meta
        except Exception:
            pass
        return {}

    # ── Log decoding ───────────────────────────────────────────────────────────

    def _decode_log(self, log_entry: dict) -> Optional[dict]:
        """
        Decode an OrderFilled log entry into a signal dict.
        Returns None if the maker isn't our whale or decoding fails.
        """
        try:
            topics = log_entry.get("topics", [])
            if len(topics) < 4:
                return None

            # topic[0] = event sig, topic[1] = orderHash, topic[2] = maker, topic[3] = taker
            maker_raw = topics[2]                           # 0x000...whale_address
            maker     = "0x" + maker_raw[-40:]             # last 20 bytes = address

            if maker.lower() != self.whale.lower():
                return None

            # ABI-decode the non-indexed data field
            # Layout: makerAssetId | takerAssetId | makerAmountFilled | takerAmountFilled | fee
            data = log_entry.get("data", "0x")
            raw  = bytes.fromhex(data[2:] if data.startswith("0x") else data)
            if len(raw) < 160:
                return None

            maker_asset  = int.from_bytes(raw[0:32],   "big")
            taker_asset  = int.from_bytes(raw[32:64],  "big")
            maker_amount = int.from_bytes(raw[64:96],  "big")   # 6-decimal units
            taker_amount = int.from_bytes(raw[96:128], "big")

            # Determine BUY vs SELL and extract the outcome token ID
            if maker_asset == USDC_ASSET_ID:
                # Maker is spending USDC → BUY
                side      = "BUY"
                token_id  = str(taker_asset)
                usdc_amt  = maker_amount / 1e6
                shares    = taker_amount / 1e6
            elif taker_asset == USDC_ASSET_ID:
                # Maker is receiving USDC → SELL
                side      = "SELL"
                token_id  = str(maker_asset)
                shares    = maker_amount / 1e6
                usdc_amt  = taker_amount / 1e6
            else:
                # Neither side is USDC — outcome↔outcome trade, skip
                return None

            if usdc_amt < 0.05:
                return None  # dust, skip

            price = round(usdc_amt / shares, 6) if shares > 0 else 0

            # Refresh cache if stale, then try fallback lookup for unknown tokens
            self._refresh_market_cache()
            meta = self._market_cache.get(token_id) or self._lookup_token(token_id)

            return {
                "type":        "copy_trade",
                "source":      self.whale,
                "source_name": "whale1",
                "market_id":   meta.get("market_id", ""),
                "asset":       token_id,
                "side":        side,
                "outcome":     meta.get("outcome", ""),
                "outcome_idx": meta.get("outcome_idx", -1),
                "size":        round(shares, 4),
                "usdc_size":   round(usdc_amt, 4),
                "price":       price,
                "title":       meta.get("title", f"Token …{token_id[-8:]}"),
                "tx_hash":     log_entry.get("transactionHash", ""),
                "raw_trade":   log_entry,
                "status":      "pending",
                "fired_at":    datetime.now(timezone.utc),
                "detection":   "mempool",
            }

        except Exception as e:
            log.debug(f"[MempoolWatcher] Decode error: {e}")
            return None

    # ── WebSocket subscription ─────────────────────────────────────────────────

    async def _subscribe_one(self, ws, contract: str, sub_id: int) -> str:
        """Subscribe to OrderFilled logs for a single contract address."""
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id":      sub_id,
            "method":  "eth_subscribe",
            "params":  [
                "logs",
                {
                    "address": contract.lower(),
                    "topics":  [ORDER_FILLED_TOPIC],
                },
            ],
        })
        await ws.send(payload)
        resp = json.loads(await ws.recv())
        sid  = resp.get("result")
        if not sid:
            log.error(f"[MempoolWatcher] Subscription failed for {contract[:14]}: {resp}")
            return None
        log.info(f"[MempoolWatcher] Subscribed to {contract[:14]} → id={sid}")
        return sid

    async def _subscribe(self):
        log.info(f"[MempoolWatcher] Connecting to Polygon WS …")
        async with websockets.connect(
            self.ws_url,
            ping_interval=20,
            ping_timeout=30,
            max_size=2**20,
        ) as ws:
            # Subscribe to each contract individually — Alchemy requires single address per sub
            sid1 = await self._subscribe_one(ws, CLOB_EXCHANGE,     sub_id=1)
            sid2 = await self._subscribe_one(ws, NEG_RISK_EXCHANGE,  sub_id=2)

            if not sid1 and not sid2:
                print("[Mempool] ✗ Both subscriptions failed — check Alchemy key/endpoint")
                return

            print(f"[Mempool] ✓ Subscribed — watching {self.whale[:14]}… on CLOB")

            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    msg = json.loads(raw)

                    log_entry = msg.get("params", {}).get("result")
                    if not log_entry:
                        continue

                    signal = self._decode_log(log_entry)
                    if signal:
                        print(
                            f"[Mempool] {signal['side']} {signal['outcome']} | "
                            f"{signal['title'][:50]} | "
                            f"${signal['usdc_size']:.2f} @ {signal['price']:.3f}"
                        )
                        self.on_trade(signal)

                except asyncio.TimeoutError:
                    await ws.ping()   # keepalive
                except websockets.ConnectionClosed:
                    log.warning("[MempoolWatcher] Connection closed — will reconnect")
                    break
                except Exception as e:
                    log.error(f"[MempoolWatcher] Error: {e}")
                    break

    async def _run_forever(self):
        self._running = True
        threading.Thread(target=self._refresh_market_cache, kwargs={"force": True}, daemon=True).start()
        backoff = 3
        while self._running:
            try:
                await self._subscribe()
                backoff = 3  # reset on clean disconnect
            except Exception as e:
                msg = str(e)
                if "429" in msg:
                    # Rate limited — back off hard, don't hammer
                    backoff = min(backoff * 2, 120)
                    log.warning(f"[MempoolWatcher] Rate limited (429). Waiting {backoff}s …")
                    print(f"[Mempool] Rate limited — backing off {backoff}s (REST fallback active)")
                else:
                    log.error(f"[MempoolWatcher] Connection failed: {e}")
                    backoff = min(backoff * 2, 60)
            if self._running:
                log.info(f"[MempoolWatcher] Reconnecting in {backoff}s …")
                await asyncio.sleep(backoff)

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self) -> threading.Thread:
        """Launch watcher in a background daemon thread. Returns the thread."""
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._run_forever())

        t = threading.Thread(target=_run, name="mempool-watcher", daemon=True)
        t.start()
        return t

    def stop(self):
        self._running = False


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    WHALE = "0xd0d6053c3c37e727402d84c14069780d360993aa"  # whale1

    def _print_signal(sig):
        print(f"\n  >>> SIGNAL: {sig['side']} {sig['outcome']} {sig['title'][:40]}")
        print(f"      ${sig['usdc_size']:.2f} @ {sig['price']:.3f}  tx={sig['tx_hash'][:14]}…\n")

    watcher = MempoolWatcher(whale_address=WHALE, on_trade=_print_signal)
    print("Starting standalone test … Ctrl+C to stop")
    t = watcher.start()
    try:
        t.join()
    except KeyboardInterrupt:
        watcher.stop()
        print("Stopped.")
