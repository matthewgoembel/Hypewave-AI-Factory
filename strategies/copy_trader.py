"""
Copy trading strategy.
Polls watched whale wallets every N seconds, detects new trades,
logs them to MongoDB, and queues copy orders.

Signal aggregation: buffers all legs of a multi-leg trade for 2 seconds,
then copies only the DOMINANT side (highest whale USDC = their real conviction).
This mirrors the whale's actual strategy instead of randomly copying hedges.
"""

import sys, os, time, logging, threading
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import re
from datetime import datetime, timezone, timedelta
from database.db import Database
from connectors.polymarket import PolymarketConnector
from connectors.mempool_watcher import MempoolWatcher
from engine.paper_trader import PaperTrader
from engine.executor import Executor


def _get_timeframe(title: str) -> str:
    """Detect timeframe from market title. Returns '5m', '15m', '30m', '1h', or 'unknown'."""
    pattern = r'(\d{1,2}):(\d{2})\s*([AP]M)\s*[-–]\s*(\d{1,2}):(\d{2})\s*([AP]M)'
    m = re.search(pattern, title, re.IGNORECASE)
    if m:
        def to_minutes(h, mi, ampm):
            h, mi = int(h), int(mi)
            if ampm.upper() == 'PM' and h != 12:
                h += 12
            if ampm.upper() == 'AM' and h == 12:
                h = 0
            return h * 60 + mi
        start = to_minutes(m.group(1), m.group(2), m.group(3))
        end   = to_minutes(m.group(4), m.group(5), m.group(6))
        diff  = (end - start) % (24 * 60)
        if diff == 5:   return '5m'
        if diff == 15:  return '15m'
        if diff == 30:  return '30m'
        if diff == 60:  return '1h'
    return 'unknown'


logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/copy_trader.log", encoding="utf-8")
    ]
)
log = logging.getLogger("copy_trader")


class CopyTrader:
    def __init__(self, poll_interval: int = 3, live: bool = False):
        self.poll_interval = poll_interval
        self.live   = live
        self.db     = Database()
        self.pm     = PolymarketConnector()
        self.paper  = PaperTrader()
        self.executor = Executor() if live else None
        if live:
            print("[LIVE MODE] Real money trading enabled.")
            self.executor.ping()
        else:
            print("[PAPER MODE] Simulated trading.")

        if live:
            self.watched = [
                "0xd0d6053c3c37e727402d84c14069780d360993aa",  # whale1
            ]
        else:
            self.watched = [
                "0xd0d6053c3c37e727402d84c14069780d360993aa",  # whale1
            ]

        self._last_seen: dict[str, str] = {}
        self._cycle_count = 0
        self._session_stop_time = None

        # ------------------------------------------------------------------
        # Signal aggregation: buffer trades by conditionId for 2 seconds,
        # then emit ONE signal for the dominant side (most USDC).
        # This is how the whale actually makes money — bet heavy on one side,
        # small hedge on the other. We copy only the heavy side.
        # ------------------------------------------------------------------
        self._trade_buffer: dict = {}          # conditionId → buffer dict
        self._buffer_lock = threading.Lock()
        self._buffer_window = 2.0              # seconds to collect both legs
        self._buffered_tx_hashes: set = set()  # global dedup within buffer window

        # Mempool watcher — fires signals before REST API indexes them
        self._mempool_watcher: MempoolWatcher = None
        try:
            self._mempool_watcher = MempoolWatcher(
                whale_address=self.watched[0],
                on_trade=self._on_mempool_trade,
            )
        except ValueError as e:
            print(f"[Mempool] Disabled — {e}")
            log.warning(f"MempoolWatcher not started: {e}")

    # ------------------------------------------------------------------
    # Mempool callback (primary fast path)
    # ------------------------------------------------------------------

    def _on_mempool_trade(self, signal: dict):
        """Called by MempoolWatcher when whale trade detected on-chain."""
        tx_hash = signal.get("tx_hash", "")
        side    = signal.get("side", "BUY").upper()

        # SELLs: execute immediately — whale exiting = we exit, no need to aggregate
        if side == "SELL":
            if tx_hash and self.db.signals.find_one({"tx_hash": tx_hash}):
                return
            signal.setdefault("status", "pending")
            signal.setdefault("conviction", 1)
            self.db.signals.insert_one(signal)
            log.info(f"[Mempool] SELL queued: {signal.get('title','')[:40]}")
            return

        # BUYs: buffer by market to pick dominant side
        condition_id = signal.get("market_id", "")
        if not condition_id:
            # No market ID from cache miss — queue direct as fallback
            if not (tx_hash and self.db.signals.find_one({"tx_hash": tx_hash})):
                signal.setdefault("status", "pending")
                signal.setdefault("conviction", 1)
                self.db.signals.insert_one(signal)
            return

        self._buffer_trade(condition_id, signal, tx_hash)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def load_last_seen(self):
        """Load last-seen trade IDs from DB so we don't re-copy on restart."""
        for trader in [t for t in self.db.get_watched_traders() if t["address"] in self.watched]:
            addr = trader["address"]
            latest = self.db.get_latest_activity(addr, limit=1)
            if latest:
                self._last_seen[addr] = latest[0].get("transactionHash", "")
        log.info(f"Loaded last-seen for {len(self._last_seen)} wallets.")

    # ------------------------------------------------------------------
    # Core polling loop
    # ------------------------------------------------------------------

    def run(self):
        log.info("Copy trader started.")
        self.load_last_seen()

        if self._mempool_watcher:
            self._mempool_watcher.start()
            print("[Mempool] Watcher started — primary detection active")

        if self.live:
            cleared = self.db.signals.update_many(
                {"status": "pending"},
                {"$set": {"status": "skipped", "skip_reason": "cleared_on_startup"}}
            )
            if cleared.modified_count:
                print(f"[LIVE] Cleared {cleared.modified_count} stale pending signals.")

        while True:
            try:
                traders = [t for t in self.db.get_watched_traders() if t["address"] in self.watched]
            except Exception as e:
                log.error(f"DB error loading traders: {e}")
                time.sleep(self.poll_interval)
                continue

            for trader in traders:
                try:
                    self._check_wallet(trader)
                except Exception as e:
                    log.error(f"Error checking {trader.get('name','?')}: {e}")

            # Flush aggregation buffers that have been waiting >= buffer_window.
            # This is where we decide which side the whale is actually betting on.
            try:
                self._flush_ready_buffers()
            except Exception as e:
                log.error(f"Buffer flush error: {e}")

            if self.live:
                bal = self.executor.get_balance()
                self.executor._cached_balance = bal
                self.executor._balance_cache_time = datetime.now(timezone.utc)

                if bal < self.executor._session_start_bal * (1 - 0.40):
                    if self._session_stop_time is None:
                        self._session_stop_time = datetime.now(timezone.utc)
                        print(f"[LIVE] Session stop — balance ${bal:.2f} (start ${self.executor._session_start_bal:.2f}). Will resume if balance recovers.")
                    else:
                        now = datetime.now(timezone.utc)
                        mins = now.minute
                        on_boundary = mins % 15 == 0 and now.second < 5
                        past_stop   = (now - self._session_stop_time).total_seconds() >= 60
                        if on_boundary and past_stop:
                            self.executor._session_start_bal = bal
                            self.executor._ordered_assets.clear()
                            self._session_stop_time = None
                            print(f"[LIVE] Session reset at :{mins:02d} boundary — new start balance ${bal:.2f}")
                    cleared = self.db.signals.update_many(
                        {"status": "pending"},
                        {"$set": {"status": "skipped", "skip_reason": "session_stop"}}
                    )
                    if cleared.modified_count:
                        print(f"[LIVE] Session stop — cleared {cleared.modified_count} pending signals.")
                    time.sleep(self.poll_interval)
                    continue

                if self._session_stop_time is not None:
                    print(f"[LIVE] Balance recovered to ${bal:.2f} — resuming trading.")
                    self.executor._ordered_assets.clear()
                    self._session_stop_time = None

                try:
                    pending = list(self.db.signals.find({"status": "pending"}).sort("fired_at", 1).limit(50))
                except Exception as e:
                    log.error(f"DB read error: {e}")
                    pending = []

                for sig in pending:
                    try:
                        resp = self.executor.place_order(sig)
                        status = "executed" if resp else "failed"
                        self.db.signals.update_one(
                            {"_id": sig["_id"]},
                            {"$set": {"status": status, "order_resp": str(resp)}}
                        )
                        if resp and sig.get("side", "BUY") == "BUY":
                            conviction = min(sig.get("conviction", 1), 3)
                            our_usdc   = 1.50 * conviction
                            our_usdc   = min(our_usdc, self.executor._cached_balance * 0.05)
                            price      = sig.get("price", 0)
                            our_shares = round(our_usdc / price, 4) if price else 0
                            self.db.db["live_trades"].insert_one({
                                "signal_id":     sig["_id"],
                                "asset":         sig.get("asset", ""),
                                "market_id":     sig.get("market_id", ""),
                                "title":         sig.get("title", ""),
                                "outcome":       sig.get("outcome", ""),
                                "side":          "BUY",
                                "entry_price":   price,
                                "cost_usdc":     round(our_usdc, 4),
                                "shares":        our_shares,
                                "current_price": price,
                                "status":        "open",
                                "opened_at":     datetime.now(timezone.utc),
                                "whale_dominant_usdc": sig.get("whale_dominant_usdc", 0),
                                "whale_total_usdc":    sig.get("whale_total_usdc", 0),
                                "dominant_ratio":      sig.get("dominant_ratio", 1.0),
                            })
                    except Exception as e:
                        log.error(f"DB update error for signal {sig.get('_id')}: {e}")

                if self._cycle_count % 30 == 0:
                    try:
                        self._update_live_positions()
                    except Exception as e:
                        log.error(f"Live position update error: {e}")

                self._cycle_count += 1
                if self._cycle_count % 180 == 0:
                    try:
                        self.executor.check_and_withdraw(self.db)
                    except Exception as e:
                        log.error(f"Withdrawal check error: {e}")
            else:
                placed = self.paper.process_pending_signals()
                if placed:
                    log.info(f"Paper traded {placed} new signal(s).")
                self.paper.update_open_positions()

            log.debug(f"Cycle complete. Sleeping {self.poll_interval}s...")
            time.sleep(self.poll_interval)

    # ------------------------------------------------------------------
    # Signal aggregation — the core fix
    # ------------------------------------------------------------------

    def _buffer_trade(self, condition_id: str, trade: dict, tx_hash: str, trader: dict = None):
        """
        Add a BUY trade to the aggregation buffer for this market.
        All legs of a multi-leg trade (e.g. whale's Up hedge + Down main bet)
        accumulate here for buffer_window seconds, then _create_aggregated_signal
        picks the dominant side.
        """
        with self._buffer_lock:
            # Global tx dedup: prevents mempool + REST double-counting the same tx
            if tx_hash and tx_hash in self._buffered_tx_hashes:
                return
            if tx_hash:
                self._buffered_tx_hashes.add(tx_hash)

            outcome = trade.get("outcome", "")
            usdc    = float(trade.get("usdcSize", 0) or trade.get("usdc_size", 0) or 0)

            if condition_id not in self._trade_buffer:
                self._trade_buffer[condition_id] = {
                    "usdc_by_outcome": {},   # outcome → total usdc on that side
                    "best_by_outcome": {},   # outcome → the single largest trade (for template)
                    "tx_hashes":       set(),
                    "first_seen":      datetime.now(timezone.utc),
                    "trader":          trader,
                }

            buf = self._trade_buffer[condition_id]
            buf["usdc_by_outcome"][outcome] = buf["usdc_by_outcome"].get(outcome, 0.0) + usdc
            if tx_hash:
                buf["tx_hashes"].add(tx_hash)

            # Keep the largest single trade per outcome as the signal template
            existing     = buf["best_by_outcome"].get(outcome)
            existing_usdc = float(existing.get("usdcSize", 0) or existing.get("usdc_size", 0) or 0) if existing else 0
            if usdc >= existing_usdc:
                buf["best_by_outcome"][outcome] = trade

            if trader:
                buf["trader"] = trader

    def _flush_ready_buffers(self):
        """
        Flush buffers that have been accumulating for >= buffer_window seconds.
        For each market, create ONE signal targeting the dominant (most-USDC) side.
        """
        now   = datetime.now(timezone.utc)
        ready = {}

        with self._buffer_lock:
            for cid in list(self._trade_buffer.keys()):
                buf = self._trade_buffer[cid]
                if (now - buf["first_seen"]).total_seconds() >= self._buffer_window:
                    ready[cid] = self._trade_buffer.pop(cid)
            # Release tx_hashes so DB-based dedup handles future REST re-detections
            for buf in ready.values():
                self._buffered_tx_hashes -= buf["tx_hashes"]

        for cid, buf in ready.items():
            try:
                self._create_aggregated_signal(cid, buf)
            except Exception as e:
                log.error(f"[Agg] Failed to create signal for {cid[:12]}: {e}")

    def _create_aggregated_signal(self, condition_id: str, buf: dict):
        """
        Emit one BUY signal for the side the whale bet the most money on.
        Conviction is derived from the dominant/total ratio — same logic the
        whale uses (more money on one side = higher confidence).
        """
        usdc_by_outcome = {k: v for k, v in buf.get("usdc_by_outcome", {}).items() if k and v > 0}
        if not usdc_by_outcome:
            return

        dominant_outcome = max(usdc_by_outcome, key=usdc_by_outcome.get)
        dominant_usdc    = usdc_by_outcome[dominant_outcome]
        total_usdc       = sum(usdc_by_outcome.values())
        hedge_usdc       = total_usdc - dominant_usdc

        # Conviction from size ratio:
        #   ≥85% on one side → very high conviction (3x bet)
        #   ≥65% on one side → high conviction (2x bet)
        #   <65%             → balanced / uncertain (1x bet)
        ratio = dominant_usdc / total_usdc if total_usdc > 0 else 1.0
        if ratio >= 0.85:
            conviction = 3
        elif ratio >= 0.65:
            conviction = 2
        else:
            conviction = 1

        template = buf["best_by_outcome"].get(dominant_outcome)
        if not template:
            return

        # Dedup: skip if we already queued this market+outcome in the last 10 seconds
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=10)
        if self.db.signals.find_one({
            "market_id": condition_id,
            "outcome":   dominant_outcome,
            "side":      "BUY",
            "status":    "pending",
            "fired_at":  {"$gte": cutoff},
        }):
            log.debug(f"[Agg] Already pending: {condition_id[:12]}/{dominant_outcome}")
            return

        # Also check if any of the buffered tx_hashes are already in a queued signal
        for tx in buf.get("tx_hashes", set()):
            if tx and self.db.signals.find_one({"buffered_tx_hashes": tx}):
                log.debug(f"[Agg] tx already processed in prior signal: {tx[:14]}")
                return

        trader = buf.get("trader") or {}
        addr   = trader.get("address", self.watched[0] if self.watched else "")
        name   = trader.get("name", "whale1")
        title  = template.get("title", condition_id[:40])
        price  = float(template.get("price", 0))
        asset  = template.get("asset", "")

        print(
            f"[Signal] BUY {dominant_outcome} ({ratio:.0%} confidence) | {title[:45]} | "
            f"whale: ${dominant_usdc:.2f} main / ${hedge_usdc:.2f} hedge | {conviction}x bet"
        )
        log.info(
            f"[Agg] {dominant_outcome} ({ratio:.0%}) | {title[:40]} | "
            f"${dominant_usdc:.2f} vs ${hedge_usdc:.2f}"
        )

        signal = {
            "type":                "copy_trade",
            "source":              addr,
            "source_name":         name,
            "market_id":           condition_id,
            "asset":               asset,
            "side":                "BUY",
            "outcome":             dominant_outcome,
            "outcome_idx":         template.get("outcomeIndex", template.get("outcome_idx", -1)),
            "size":                float(template.get("size", 0)),
            "usdc_size":           float(template.get("usdcSize", 0) or template.get("usdc_size", 0) or 0),
            "price":               price,
            "title":               title,
            "tx_hash":             template.get("transactionHash", template.get("tx_hash", "")),
            "buffered_tx_hashes":  list(buf.get("tx_hashes", set())),  # all legs for dedup
            "raw_trade":           template,
            "conviction":          conviction,
            "dominant_ratio":      round(ratio, 3),
            "whale_dominant_usdc": round(dominant_usdc, 4),
            "whale_total_usdc":    round(total_usdc, 4),
            "status":              "pending",
            "fired_at":            datetime.now(timezone.utc),
        }
        self.db.signals.insert_one(signal)
        log.info(f"  Signal queued: {dominant_outcome} {conviction}x @ {price:.3f}")

    def _queue_sell(self, trader: dict, trade: dict):
        """Queue a SELL signal immediately — no buffering, follow whale exits fast."""
        addr         = trader["address"]
        name         = trader.get("name", addr[:8])
        condition_id = trade.get("conditionId", "unknown")
        tx_hash      = trade.get("transactionHash", "")

        if tx_hash and self.db.signals.find_one({"tx_hash": tx_hash}):
            return

        signal = {
            "type":         "copy_trade",
            "source":       addr,
            "source_name":  name,
            "market_id":    condition_id,
            "asset":        trade.get("asset", ""),
            "side":         "SELL",
            "outcome":      trade.get("outcome", ""),
            "outcome_idx":  trade.get("outcomeIndex", -1),
            "size":         float(trade.get("size", 0)),
            "usdc_size":    float(trade.get("usdcSize", 0)),
            "price":        float(trade.get("price", 0)),
            "title":        trade.get("title", condition_id[:40]),
            "tx_hash":      tx_hash,
            "raw_trade":    trade,
            "conviction":   1,
            "status":       "pending",
            "fired_at":     datetime.now(timezone.utc),
        }
        self.db.signals.insert_one(signal)
        log.info(f"[SELL] Signal queued: {signal['outcome']} {signal['title'][:40]}")

    # ------------------------------------------------------------------
    # Live position tracking
    # ------------------------------------------------------------------

    def _update_live_positions(self):
        """Update current prices on open live trades and close resolved ones."""
        import requests
        open_trades = list(self.db.db["live_trades"].find({"status": "open"}))
        for trade in open_trades:
            asset = trade.get("asset", "")
            if not asset:
                continue
            try:
                r = requests.get(
                    f"https://clob.polymarket.com/midpoint?token_id={asset}",
                    timeout=3
                )
                mid = float(r.json().get("mid", 0))
                if mid <= 0:
                    continue
                entry  = trade.get("entry_price", 0)
                shares = trade.get("shares", 0)
                upnl   = (mid - entry) * shares
                update = {"current_price": mid, "unrealized_pnl": round(upnl, 4)}
                if mid >= 0.97:
                    pnl = (mid - entry) * shares
                    update.update({
                        "status": "closed", "result": "win",
                        "close_price": mid, "realized_pnl": round(pnl, 4),
                        "closed_at": datetime.now(timezone.utc),
                    })
                elif mid <= 0.03:
                    pnl = (mid - entry) * shares
                    update.update({
                        "status": "closed", "result": "loss",
                        "close_price": mid, "realized_pnl": round(pnl, 4),
                        "closed_at": datetime.now(timezone.utc),
                    })
                self.db.db["live_trades"].update_one({"_id": trade["_id"]}, {"$set": update})
            except Exception:
                continue

    # ------------------------------------------------------------------
    # REST wallet polling
    # ------------------------------------------------------------------

    def _check_wallet(self, trader: dict):
        addr = trader["address"]
        name = trader.get("name", addr[:8])

        activity = self.pm.get_wallet_trades(addr, limit=20)
        trades   = activity if isinstance(activity, list) else activity.get("data", [])

        if not trades:
            return

        last_seen_id = self._last_seen.get(addr, "")
        new_trades   = []

        for trade in trades:
            trade_id = trade.get("transactionHash", "")
            if trade_id == last_seen_id:
                break
            new_trades.append(trade)

        if not new_trades:
            return

        log.info(f"[{name}] {len(new_trades)} new trade(s) detected.")

        for trade in new_trades:
            self._process_trade(trader, trade)

        self._last_seen[addr] = new_trades[0].get("transactionHash", "")

    # ------------------------------------------------------------------
    # Trade processing (REST path)
    # ------------------------------------------------------------------

    def _process_trade(self, trader: dict, trade: dict):
        addr = trader["address"]
        name = trader.get("name", addr[:8])

        tx_hash = trade.get("transactionHash", "")

        # Skip if already queued in DB (mempool may have already created a signal)
        if tx_hash and self.db.signals.find_one(
            {"$or": [{"tx_hash": tx_hash}, {"buffered_tx_hashes": tx_hash}]}
        ):
            log.debug(f"[REST] Skipping duplicate (already in DB): {tx_hash[:14]}")
            return

        # Log raw activity
        self.db.log_trader_activity(addr, trade)

        side         = trade.get("side", "BUY").upper()
        condition_id = trade.get("conditionId", "unknown")
        outcome      = trade.get("outcome", "")
        price        = float(trade.get("price", 0))
        size         = float(trade.get("size", 0))

        print(
            f"[{name}] {side} {outcome} | {trade.get('title','')[:50]} | "
            f"size={size:.2f} @ {price:.3f}"
        )

        if side == "SELL":
            # SELLs: queue immediately, don't aggregate
            self._queue_sell(trader, trade)
            return

        # BUYs: add to aggregation buffer keyed by market
        self._buffer_trade(condition_id, trade, tx_hash, trader=trader)


if __name__ == "__main__":
    import argparse
    os.makedirs("logs", exist_ok=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Enable live trading with real money")
    args = parser.parse_args()

    if args.live:
        print("=" * 50)
        print("  LIVE MODE — REAL MONEY")
        print("=" * 50)

    ct = CopyTrader(poll_interval=1, live=args.live)
    ct.run()
