"""
Copy trading strategy.
Polls watched whale wallets every N seconds, detects new trades,
logs them to MongoDB, and queues copy orders.
"""

import sys, os, time, logging
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
    # Match patterns like "12:00PM-12:15PM" or "12:00 PM - 12:15 PM"
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
        """
        poll_interval: seconds between wallet checks (default 30s).
        Lower = faster reaction but more API calls.
        """
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
                "0xd0d6053c3c37e727402d84c14069780d360993aa",  # whale1 ~$2.7k portfolio
            ]
        else:
            # Paper: same single wallet as paper_trader.py WALLET constant
            self.watched = [
                "0xd0d6053c3c37e727402d84c14069780d360993aa",  # whale1
            ]
        # Track the latest seen trade ID per wallet to detect new ones
        # Loaded from DB on startup so it survives restarts
        self._last_seen: dict[str, str] = {}
        self._cycle_count = 0
        self._session_stop_time = None  # when session stop first fired

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
        """Called by MempoolWatcher when whale1 trade detected on-chain."""
        tx_hash = signal.get("tx_hash", "")

        # Dedup — REST poller might also see this trade
        if tx_hash and self.db.signals.find_one({"tx_hash": tx_hash}):
            return

        self.db.signals.insert_one(signal)
        log.info(f"[Mempool] Signal queued: {signal['side']} {signal.get('title','')[:40]}")

        # Prevent REST poller from re-queuing the same tx
        if tx_hash:
            self._last_seen[signal["source"]] = tx_hash

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

        # Start mempool watcher in background thread (primary fast-path)
        if self._mempool_watcher:
            self._mempool_watcher.start()
            print("[Mempool] Watcher started — primary detection active")

        if self.live:
            # Wipe any leftover pending signals from previous sessions
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

            if self.live:
                # Live mode — place real orders
                # Always use fresh balance for session stop decision
                bal = self.executor.get_balance()
                self.executor._cached_balance = bal
                self.executor._balance_cache_time = datetime.now(timezone.utc)

                if bal < self.executor._session_start_bal * (1 - 0.40):
                    if self._session_stop_time is None:
                        self._session_stop_time = datetime.now(timezone.utc)
                        print(f"[LIVE] Session stop — balance ${bal:.2f} (start ${self.executor._session_start_bal:.2f}). Will resume if balance recovers.")
                    # After 15 min cooldown, reset session start to current balance
                    elif (datetime.now(timezone.utc) - self._session_stop_time).total_seconds() >= 900:
                        self.executor._session_start_bal = bal
                        self.executor._ordered_assets.clear()
                        self._session_stop_time = None
                        print(f"[LIVE] Session reset after cooldown — new start balance ${bal:.2f}")
                    cleared = self.db.signals.update_many(
                        {"status": "pending"},
                        {"$set": {"status": "skipped", "skip_reason": "session_stop"}}
                    )
                    if cleared.modified_count:
                        print(f"[LIVE] Session stop — cleared {cleared.modified_count} pending signals.")
                    time.sleep(self.poll_interval)
                    continue

                # Balance recovered or never hit stop — resume/continue trading
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
                        # Store live trade for dashboard tracking
                        if resp and sig.get("side", "BUY") == "BUY":
                            # Use same flat bet + conviction formula as executor
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
                            })
                    except Exception as e:
                        log.error(f"DB update error for signal {sig.get('_id')}: {e}")
                # Update live trade prices every 30 cycles
                if self._cycle_count % 30 == 0:
                    try:
                        self._update_live_positions()
                    except Exception as e:
                        log.error(f"Live position update error: {e}")
                # Check auto-withdrawal every 180 cycles (~3 min at 1s poll) to avoid excess API calls
                self._cycle_count += 1
                if self._cycle_count % 180 == 0:
                    try:
                        self.executor.check_and_withdraw(self.db)
                    except Exception as e:
                        log.error(f"Withdrawal check error: {e}")
            else:
                # Paper mode
                placed = self.paper.process_pending_signals()
                if placed:
                    log.info(f"Paper traded {placed} new signal(s).")
                self.paper.update_open_positions()

            log.debug(f"Cycle complete. Sleeping {self.poll_interval}s...")
            time.sleep(self.poll_interval)

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
                entry = trade.get("entry_price", 0)
                cost  = trade.get("cost_usdc", 0)
                shares = trade.get("shares", 0)
                upnl  = (mid - entry) * shares
                update = {"current_price": mid, "unrealized_pnl": round(upnl, 4)}
                if mid >= 0.97:
                    # Won — close it
                    pnl = (mid - entry) * shares
                    update.update({
                        "status": "closed", "result": "win",
                        "close_price": mid, "realized_pnl": round(pnl, 4),
                        "closed_at": datetime.now(timezone.utc),
                    })
                elif mid <= 0.03:
                    # Lost
                    pnl = (mid - entry) * shares
                    update.update({
                        "status": "closed", "result": "loss",
                        "close_price": mid, "realized_pnl": round(pnl, 4),
                        "closed_at": datetime.now(timezone.utc),
                    })
                self.db.db["live_trades"].update_one({"_id": trade["_id"]}, {"$set": update})
            except Exception:
                continue

    def _check_wallet(self, trader: dict):
        addr = trader["address"]
        name = trader.get("name", addr[:8])

        activity = self.pm.get_wallet_trades(addr, limit=20)

        # API returns a list directly or wrapped — handle both
        trades = activity if isinstance(activity, list) else activity.get("data", [])

        if not trades:
            return

        last_seen_id = self._last_seen.get(addr, "")
        new_trades = []

        for trade in trades:
            trade_id = trade.get("transactionHash", "")
            if trade_id == last_seen_id:
                break  # Everything after this is already processed
            new_trades.append(trade)

        if not new_trades:
            return

        log.info(f"[{name}] {len(new_trades)} new trade(s) detected.")

        for trade in new_trades:
            self._process_trade(trader, trade)

        # Update last seen to the most recent trade
        self._last_seen[addr] = new_trades[0].get("transactionHash", "")

    # ------------------------------------------------------------------
    # Trade processing
    # ------------------------------------------------------------------

    def _process_trade(self, trader: dict, trade: dict):
        addr = trader["address"]
        name = trader.get("name", addr[:8])

        # Skip if mempool watcher already queued this trade
        tx_hash = trade.get("transactionHash", "")
        if tx_hash and self.db.signals.find_one({"tx_hash": tx_hash}):
            log.debug(f"[REST] Skipping duplicate (mempool already caught it): {tx_hash[:14]}")
            return

        # Log raw activity to DB
        self.db.log_trader_activity(addr, trade)

        # Parse the trade
        market_id    = trade.get("conditionId", "unknown")
        title        = trade.get("title", market_id[:40])

        side         = trade.get("side", "").upper()         # BUY or SELL
        outcome      = trade.get("outcome", "")              # YES / NO / Up / Down
        outcome_idx  = trade.get("outcomeIndex", -1)
        size         = float(trade.get("size", 0))           # shares
        usdc_size    = float(trade.get("usdcSize", 0))       # USD value
        price        = float(trade.get("price", 0))          # 0.0–1.0
        asset        = trade.get("asset", "")

        print(
            f"[{name}] {side} {outcome} | {title[:50]} | "
            f"size={size:.2f} @ {price:.3f}"
        )

        # Conviction: how many times has whale bet same direction on this asset in last 60s?
        # More rapid same-direction bets = higher whale conviction = we size up.
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
        conviction = self.db.signals.count_documents({
            "asset":    asset,
            "side":     side,
            "source":   addr,
            "fired_at": {"$gte": cutoff},
        })
        conviction = min(conviction + 1, 3)  # +1 for this signal, cap at 3x

        # Queue copy signal
        signal = {
            "type":         "copy_trade",
            "source":       addr,
            "source_name":  name,
            "market_id":    market_id,
            "asset":        asset,
            "side":         side,
            "outcome":      outcome,
            "outcome_idx":  outcome_idx,
            "size":         size,
            "usdc_size":    usdc_size,
            "price":        price,
            "title":        title,
            "tx_hash":      trade.get("transactionHash", ""),
            "raw_trade":    trade,
            "conviction":   conviction,
            "status":       "pending",  # pending → executed / skipped
            "fired_at":     datetime.now(timezone.utc),
        }
        self.db.signals.insert_one(signal)
        log.info(f"  Signal queued for {title[:40]}")

        # TODO: Auto-execute — wired in engine/executor.py (next step)


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
