"""
Paper trading engine — single wallet, simulates live account behaviour.
Winning positions sit as "pending_claim" for 60s before cash returns,
matching the real-world delay of claiming resolved positions.
"""

import sys, os, logging, requests
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime, timezone, timedelta
from database.db import Database
from connectors.polymarket import PolymarketConnector

log = logging.getLogger("paper_trader")

# ── Config — change WALLET to test a different whale ──────────────────────────
WALLET          = "0xd0d6053c3c37e727402d84c14069780d360993aa"  # whale1
BUDGET          = 100.0   # starting paper balance
MIN_TRADE_USD   = 0.50
CLOB_FEE        = 0.005   # 0.5% per trade
RESOLVED_THRESH = 0.97
CLAIM_DELAY_S   = 60      # seconds before a won position returns cash


class PaperTrader:
    def __init__(self):
        self.db = Database()
        self.pm = PolymarketConnector()
        self._whale_portfolio = self._fetch_whale_portfolio()
        self._ensure_account()

    # ── Whale portfolio for proportional scaling ──────────────────────────────

    def _fetch_whale_portfolio(self) -> float:
        try:
            r = requests.get(
                f"https://data-api.polymarket.com/value?user={WALLET}",
                timeout=5
            )
            data = r.json()
            if isinstance(data, list) and data:
                val = float(data[0].get("value", 0))
                if val > 0:
                    return val
        except Exception:
            pass
        return 35895.0  # fallback

    # ── Account setup ─────────────────────────────────────────────────────────

    def _ensure_account(self):
        existing = self.db.db["paper_accounts"].find_one({"address": WALLET})
        if not existing:
            self.db.db["paper_accounts"].insert_one({
                "address":       WALLET,
                "budget":        BUDGET,
                "balance":       BUDGET,
                "realized_pnl":  0.0,
                "total_wagered": 0.0,
                "wins":          0,
                "losses":        0,
                "created_at":    datetime.now(timezone.utc),
                "updated_at":    datetime.now(timezone.utc),
            })
            log.info(f"Created paper account for {WALLET[:10]} — ${BUDGET}")

    def get_account(self) -> dict:
        return self.db.db["paper_accounts"].find_one({"address": WALLET})

    def _update_account(self, delta_balance: float, delta_pnl: float,
                        delta_wagered: float = 0, win: bool = None):
        update = {
            "$inc": {
                "balance":       delta_balance,
                "realized_pnl":  delta_pnl,
                "total_wagered": delta_wagered,
            },
            "$set": {"updated_at": datetime.now(timezone.utc)}
        }
        if win is True:  update["$inc"]["wins"]   = 1
        if win is False: update["$inc"]["losses"] = 1
        self.db.db["paper_accounts"].update_one({"address": WALLET}, update)

    # ── Execute a signal ──────────────────────────────────────────────────────

    def execute_signal(self, signal: dict) -> bool:
        source = signal.get("source", "")
        if source != WALLET:
            return False

        account = self.get_account()
        if not account:
            return False

        market_id   = signal.get("market_id", "")
        asset       = signal.get("asset", "")
        side        = signal.get("side", "BUY")
        outcome     = signal.get("outcome", "")
        outcome_idx = signal.get("outcome_idx", -1)
        price       = signal.get("price", 0)
        title       = signal.get("title", "")

        if price <= 0 or price >= 1:
            return False

        # SELL — close open position
        if side == "SELL":
            open_pos = self.db.db["paper_trades"].find_one({
                "asset":  asset,
                "status": "open",
            })
            if not open_pos:
                self.db.signals.update_one(
                    {"_id": signal["_id"]},
                    {"$set": {"status": "skipped", "skip_reason": "no open position"}}
                )
                return False
            won = price > open_pos["entry_price"]
            self._close_trade(open_pos, price, won=won)
            self.db.signals.update_one({"_id": signal["_id"]}, {"$set": {"status": "executed"}})
            return True

        # BUY
        whale_usdc = signal.get("usdc_size", 0)
        if whale_usdc < MIN_TRADE_USD:
            self.db.signals.update_one(
                {"_id": signal["_id"]},
                {"$set": {"status": "skipped", "skip_reason": "too small"}}
            )
            return False

        balance = account["balance"]

        if balance <= 0:
            print(f"[Paper] Balance too low: ${balance:.2f}")
            self.db.signals.update_one(
                {"_id": signal["_id"]},
                {"$set": {"status": "skipped", "skip_reason": "balance too low"}}
            )
            return False

        scale    = balance / self._whale_portfolio
        our_usdc = max(whale_usdc * scale, MIN_TRADE_USD)

        if our_usdc > balance:
            print(f"[Paper] Balance too low: ${balance:.2f}")
            self.db.signals.update_one(
                {"_id": signal["_id"]},
                {"$set": {"status": "skipped", "skip_reason": "insufficient balance"}}
            )
            return False

        # Deduct fee
        our_usdc   = our_usdc * (1 - CLOB_FEE)
        our_shares = our_usdc / price

        trade = {
            "source":        WALLET,
            "signal_id":     signal["_id"],
            "market_id":     market_id,
            "asset":         asset,
            "side":          side,
            "outcome":       outcome,
            "outcome_idx":   outcome_idx,
            "entry_price":   price,
            "shares":        our_shares,
            "cost_usdc":     our_usdc,
            "title":         title,
            "status":        "open",
            "current_price": price,
            "unrealized_pnl": 0.0,
            "realized_pnl":  None,
            "opened_at":     datetime.now(timezone.utc),
            "resolved_at":   None,
            "closed_at":     None,
        }
        self.db.db["paper_trades"].insert_one(trade)
        self._update_account(delta_balance=-our_usdc, delta_pnl=0, delta_wagered=our_usdc)
        self.db.signals.update_one(
            {"_id": signal["_id"]},
            {"$set": {"status": "executed", "paper_trade_usdc": our_usdc}}
        )
        print(f"[Paper] BUY {outcome} | {title[:50]} | ${our_usdc:.2f} @ {price:.3f}")
        return True

    # ── Process pending signals ───────────────────────────────────────────────

    def process_pending_signals(self):
        pending = list(self.db.signals.find({"status": "pending"}).sort("fired_at", 1).limit(100))
        placed = 0
        for sig in pending:
            if self.execute_signal(sig):
                placed += 1
        return placed

    # ── Mark-to-market ────────────────────────────────────────────────────────

    def update_open_positions(self):
        # Auto-claim won positions after CLAIM_DELAY_S
        self._process_pending_claims()

        open_trades = list(self.db.db["paper_trades"].find({"status": "open"}))
        if not open_trades:
            return

        asset_map: dict = {}
        for t in open_trades:
            a = t.get("asset", "")
            if a:
                asset_map.setdefault(a, []).append(t)

        for asset, trades in asset_map.items():
            try:
                book = self.pm.get_market_orderbook(asset)
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids and asks:
                    mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
                elif bids:
                    mid = float(bids[0]["price"])
                elif asks:
                    mid = float(asks[0]["price"])
                else:
                    continue
                for trade in trades:
                    self._update_trade_price(trade, mid)
            except Exception as e:
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                if status_code == 404:
                    self._resolve_expired_trades(trades)
                else:
                    log.warning(f"Price update failed for {asset[:12]}: {e}")

    def _update_trade_price(self, trade: dict, current_price: float):
        shares = trade["shares"]
        cost   = trade["cost_usdc"]
        unrealized = (current_price - trade["entry_price"]) * shares

        if current_price >= RESOLVED_THRESH:
            # Won — mark pending_claim, cash returns after delay
            self.db.db["paper_trades"].update_one(
                {"_id": trade["_id"]},
                {"$set": {
                    "status":        "pending_claim",
                    "current_price": current_price,
                    "resolved_at":   datetime.now(timezone.utc),
                    "unrealized_pnl": round(unrealized, 4),
                }}
            )
            print(f"[Paper] WON (pending claim) | {trade['title'][:50]}")
        elif current_price <= (1 - RESOLVED_THRESH):
            # Lost — close immediately
            self._close_trade(trade, current_price, won=False)
        else:
            self.db.db["paper_trades"].update_one(
                {"_id": trade["_id"]},
                {"$set": {
                    "current_price":  current_price,
                    "unrealized_pnl": round(unrealized, 4),
                    "updated_at":     datetime.now(timezone.utc),
                }}
            )

    def _process_pending_claims(self):
        """Close won positions that have waited CLAIM_DELAY_S seconds."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_DELAY_S)
        claimable = list(self.db.db["paper_trades"].find({
            "status":      "pending_claim",
            "resolved_at": {"$lte": cutoff},
        }))
        for trade in claimable:
            self._close_trade(trade, trade["current_price"], won=True)
            print(f"[Paper] CLAIMED | {trade['title'][:50]} | PnL: ${trade['shares'] * trade['current_price'] - trade['cost_usdc']:+.2f}")

    def _resolve_expired_trades(self, trades: list):
        for trade in trades:
            condition_id = trade.get("market_id", "")
            outcome_idx  = trade.get("outcome_idx", -1)
            try:
                r = requests.get(
                    f"https://gamma-api.polymarket.com/markets/{condition_id}",
                    timeout=5
                )
                if r.status_code == 200:
                    data   = r.json()
                    tokens = data.get("tokens") or []
                    if tokens and outcome_idx >= 0 and outcome_idx < len(tokens):
                        price = float(tokens[outcome_idx].get("price", -1))
                        if price >= RESOLVED_THRESH or price <= (1 - RESOLVED_THRESH):
                            self._update_trade_price(trade, price)
            except Exception:
                pass

    def _close_trade(self, trade: dict, final_price: float, won: bool):
        shares   = trade["shares"]
        cost     = trade["cost_usdc"]
        proceeds = shares * final_price
        pnl      = proceeds - cost

        self.db.db["paper_trades"].update_one(
            {"_id": trade["_id"]},
            {"$set": {
                "status":         "closed",
                "current_price":  final_price,
                "realized_pnl":   round(pnl, 4),
                "unrealized_pnl": 0.0,
                "closed_at":      datetime.now(timezone.utc),
            }}
        )
        self._update_account(delta_balance=proceeds, delta_pnl=pnl, win=won)
        if not won:
            print(f"[Paper] LOSS | {trade['title'][:50]} | PnL: ${pnl:+.2f}")

    # ── Summary ───────────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        account = self.get_account()
        if not account:
            return {}

        open_trades   = list(self.db.db["paper_trades"].find({"status": "open"}))
        pending       = list(self.db.db["paper_trades"].find({"status": "pending_claim"}))
        closed_trades = list(self.db.db["paper_trades"].find({"status": "closed"}))

        unrealized  = sum(t.get("unrealized_pnl", 0) for t in open_trades + pending)
        realized    = account.get("realized_pnl", 0)
        balance     = account.get("balance", 0)
        budget      = account.get("budget", BUDGET)
        wins        = account.get("wins", 0)
        losses      = account.get("losses", 0)
        total_closed = wins + losses
        winrate     = round(wins / total_closed * 100, 1) if total_closed else 0

        open_value  = sum(t.get("current_price", 0) * t.get("shares", 0) for t in open_trades + pending)
        portfolio   = balance + open_value

        return {
            "balance":       round(balance, 2),
            "portfolio":     round(portfolio, 2),
            "open_value":    round(open_value, 2),
            "budget":        budget,
            "realized_pnl":  round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl":     round(realized + unrealized, 2),
            "open_trades":   len(open_trades),
            "pending_claims": len(pending),
            "wins":          wins,
            "losses":        losses,
            "winrate":       winrate,
            "roi_pct":       round((realized + unrealized) / budget * 100, 2) if budget else 0,
        }
