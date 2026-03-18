"""
Copy trading strategy.
Polls watched whale wallets every N seconds, detects new trades,
logs them to MongoDB, and queues copy orders.
"""

import sys, os, time, logging
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import re
from datetime import datetime, timezone
from database.db import Database
from connectors.polymarket import PolymarketConnector
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
                    except Exception as e:
                        log.error(f"DB update error for signal {sig.get('_id')}: {e}")
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
