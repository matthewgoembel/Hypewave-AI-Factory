"""
Paper trading engine.
Simulates trades when signals fire, tracks per-whale PnL against a $500 budget.
Fetches current market prices to calculate unrealized PnL.
When a market resolves (price → 1.0 or 0.0), closes the position and books realized PnL.
"""

import sys, os, logging
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime, timezone
from database.db import Database
from connectors.polymarket import PolymarketConnector

log = logging.getLogger("paper_trader")

# $500 per whale, $2500 total
BUDGET_PER_WHALE = 100.0
WALLETS = [
    "0xd0d6053c3c37e727402d84c14069780d360993aa",  # whale1
]
TOTAL_BUDGET = BUDGET_PER_WHALE * len(WALLETS)   # $100 total

# Cap individual trade size so one bad bet doesn't wipe the whale's budget
MAX_TRADE_PCT    = 0.10   # max 10% of whale's budget per trade ($10)
MIN_TRADE_USD    = 0.50   # ignore signals under $0.50 (noise)
RESOLVED_THRESH  = 0.97   # price ≥ 0.97 or ≤ 0.03 = treat as resolved


class PaperTrader:
    def __init__(self):
        self.db = Database()
        self.pm = PolymarketConnector()
        self._ensure_whale_accounts()

    # ------------------------------------------------------------------
    # Account setup
    # ------------------------------------------------------------------

    def _ensure_whale_accounts(self):
        """Create a paper account for each whale if it doesn't exist."""
        for addr in WALLETS:
            existing = self.db.db["paper_accounts"].find_one({"address": addr})
            if not existing:
                self.db.db["paper_accounts"].insert_one({
                    "address":      addr,
                    "budget":       BUDGET_PER_WHALE,
                    "balance":      BUDGET_PER_WHALE,   # available cash
                    "realized_pnl": 0.0,
                    "total_wagered": 0.0,
                    "wins":         0,
                    "losses":       0,
                    "created_at":   datetime.now(timezone.utc),
                    "updated_at":   datetime.now(timezone.utc),
                })
                log.info(f"Created paper account for {addr[:10]}... — ${BUDGET_PER_WHALE}")

    def get_account(self, address: str) -> dict:
        return self.db.db["paper_accounts"].find_one({"address": address})

    def get_all_accounts(self) -> list:
        return list(self.db.db["paper_accounts"].find())

    def _update_account(self, address: str, delta_balance: float, delta_pnl: float,
                        delta_wagered: float = 0, win: bool = None):
        update = {
            "$inc": {
                "balance":       delta_balance,
                "realized_pnl":  delta_pnl,
                "total_wagered": delta_wagered,
            },
            "$set": {"updated_at": datetime.now(timezone.utc)}
        }
        if win is True:
            update["$inc"]["wins"] = 1
        elif win is False:
            update["$inc"]["losses"] = 1
        self.db.db["paper_accounts"].update_one({"address": address}, update)

    # ------------------------------------------------------------------
    # Execute a paper trade from a signal
    # ------------------------------------------------------------------

    def execute_signal(self, signal: dict) -> bool:
        """
        Simulate placing a trade based on a copy signal.
        Returns True if trade was placed, False if skipped.
        """
        source   = signal.get("source", "")
        account  = self.get_account(source)
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
            return False   # skip degenerate prices

        # Scale whale's USD size proportionally to our budget
        whale_usdc = signal.get("usdc_size", 0)
        if whale_usdc < MIN_TRADE_USD:
            self.db.signals.update_one(
                {"_id": signal["_id"]},
                {"$set": {"status": "skipped", "skip_reason": "too small"}}
            )
            return False

        # Our trade = whale_usdc scaled to our budget ratio, capped at 10%
        max_trade    = account["balance"] * MAX_TRADE_PCT
        our_usdc     = min(whale_usdc, max_trade)
        our_usdc     = max(our_usdc, MIN_TRADE_USD)

        if our_usdc > account["balance"]:
            self.db.signals.update_one(
                {"_id": signal["_id"]},
                {"$set": {"status": "skipped", "skip_reason": "insufficient balance"}}
            )
            return False

        our_shares = our_usdc / price

        # Record the paper trade
        paper_trade = {
            "source":       source,
            "signal_id":    signal["_id"],
            "market_id":    market_id,
            "asset":        asset,
            "side":         side,
            "outcome":      outcome,
            "outcome_idx":  outcome_idx,
            "entry_price":  price,
            "shares":       our_shares,
            "cost_usdc":    our_usdc,
            "title":        title,
            "status":       "open",
            "current_price": price,
            "unrealized_pnl": 0.0,
            "realized_pnl":   None,
            "opened_at":    datetime.now(timezone.utc),
            "closed_at":    None,
        }
        self.db.db["paper_trades"].insert_one(paper_trade)

        # Deduct from balance
        self._update_account(source, delta_balance=-our_usdc, delta_pnl=0, delta_wagered=our_usdc)

        # Mark signal as executed
        self.db.signals.update_one(
            {"_id": signal["_id"]},
            {"$set": {"status": "executed", "paper_trade_usdc": our_usdc}}
        )

        log.info(f"[Paper] {source[:10]} {side} {outcome} {title[:40]} — ${our_usdc:.2f} @ {price:.3f}")
        return True

    # ------------------------------------------------------------------
    # Process pending signals
    # ------------------------------------------------------------------

    def process_pending_signals(self):
        """Pick up any unexecuted signals and paper-trade them."""
        pending = list(self.db.signals.find({"status": "pending"}).sort("fired_at", 1).limit(100))
        placed = 0
        for sig in pending:
            if self.execute_signal(sig):
                placed += 1
        if placed:
            log.info(f"[Paper] Placed {placed} paper trades from pending signals.")
        return placed

    # ------------------------------------------------------------------
    # Mark-to-market: update unrealized PnL on open positions
    # ------------------------------------------------------------------

    def update_open_positions(self):
        """
        Fetch current prices for all open paper trades and update unrealized PnL.
        Closes positions where the market has resolved.
        """
        open_trades = list(self.db.db["paper_trades"].find({"status": "open"}))
        if not open_trades:
            return

        # Group by asset token to minimize API calls
        asset_map: dict[str, list] = {}
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
                    # Market expired — resolve using the gamma API final price
                    self._resolve_expired_trades(trades)
                else:
                    log.warning(f"[Paper] Price update failed for asset {asset[:12]}: {e}")

    def _update_trade_price(self, trade: dict, current_price: float):
        entry  = trade["entry_price"]
        shares = trade["shares"]
        cost   = trade["cost_usdc"]

        unrealized = (current_price - entry) * shares

        # Check if market resolved
        if current_price >= RESOLVED_THRESH:       # outcome won
            self._close_trade(trade, current_price, won=True)
        elif current_price <= (1 - RESOLVED_THRESH):  # outcome lost
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

    def _resolve_expired_trades(self, trades: list):
        """
        Market orderbook is gone (404) — fetch final resolution from gamma API.
        Only close a trade if we get a definitive resolved price (0.0 or 1.0).
        If we can't confirm the outcome, leave it open — never guess.
        """
        import requests
        for trade in trades:
            condition_id = trade.get("market_id", "")
            outcome_idx  = trade.get("outcome_idx", -1)
            try:
                r = requests.get(
                    f"https://gamma-api.polymarket.com/markets/{condition_id}",
                    timeout=5
                )
                if r.status_code == 200:
                    data = r.json()
                    tokens = data.get("tokens") or []
                    if tokens and isinstance(tokens, list) and outcome_idx >= 0:
                        token = tokens[outcome_idx] if outcome_idx < len(tokens) else None
                        if token:
                            price = float(token.get("price", -1))
                            # Only close if definitively resolved
                            if price >= RESOLVED_THRESH or price <= (1 - RESOLVED_THRESH):
                                self._update_trade_price(trade, price)
                            # Otherwise leave open — don't guess
            except Exception:
                pass  # Leave open, try again next cycle

    def _close_trade(self, trade: dict, final_price: float, won: bool):
        shares   = trade["shares"]
        cost     = trade["cost_usdc"]
        proceeds = shares * final_price  # 1.0 if won, ~0 if lost
        pnl      = proceeds - cost
        source   = trade["source"]

        self.db.db["paper_trades"].update_one(
            {"_id": trade["_id"]},
            {"$set": {
                "status":        "closed",
                "current_price": final_price,
                "realized_pnl":  round(pnl, 4),
                "unrealized_pnl": 0.0,
                "closed_at":     datetime.now(timezone.utc),
            }}
        )

        # Return proceeds to balance, book PnL
        self._update_account(source, delta_balance=proceeds, delta_pnl=pnl, win=won)
        log.info(f"[Paper] Closed {'WIN' if won else 'LOSS'} {trade['title'][:40]} — PnL: ${pnl:+.2f}")

    # ------------------------------------------------------------------
    # Summary stats per whale
    # ------------------------------------------------------------------

    def get_whale_summary(self, address: str) -> dict:
        account = self.get_account(address)
        if not account:
            return {}

        open_trades   = list(self.db.db["paper_trades"].find({"source": address, "status": "open"}))
        closed_trades = list(self.db.db["paper_trades"].find({"source": address, "status": "closed"}))

        unrealized = sum(t.get("unrealized_pnl", 0) for t in open_trades)
        realized   = account.get("realized_pnl", 0)
        balance    = account.get("balance", 0)
        budget     = account.get("budget", BUDGET_PER_WHALE)
        wagered    = account.get("total_wagered", 0)
        wins       = account.get("wins", 0)
        losses     = account.get("losses", 0)
        total_closed = wins + losses
        winrate    = round(wins / total_closed * 100, 1) if total_closed else 0

        return {
            "address":       address,
            "balance":       round(balance, 2),
            "budget":        budget,
            "realized_pnl":  round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl":     round(realized + unrealized, 2),
            "total_wagered": round(wagered, 2),
            "open_trades":   len(open_trades),
            "wins":          wins,
            "losses":        losses,
            "winrate":       winrate,
            "roi_pct":       round((realized + unrealized) / budget * 100, 2) if budget else 0,
        }
