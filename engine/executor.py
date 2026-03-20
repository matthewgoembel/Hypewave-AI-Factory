"""
Live trade executor for Polymarket.
Uses py-clob-client to place real orders on the CLOB.
Auto-withdraws profits to Phantom wallet when balance hits threshold.
"""

import os, logging, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, AssetType, BalanceAllowanceParams
from py_clob_client.constants import POLYGON

load_dotenv()
log = logging.getLogger("executor")

CLOB_HOST       = "https://clob.polymarket.com"
PRIVATE_KEY     = os.getenv("POLYMARKET_WALLET_PRIVATE_KEY")   # signer key → 0x32e16b
ACCOUNT_ADDRESS = os.getenv("POLYMARKET_ACCOUNT_ADRESS")          # proxy wallet → 0xa71d97b7 (holds funds)
CHAIN_ID        = POLYGON  # 137
WHALE_ADDRESS   = "0xd0d6053c3c37e727402d84c14069780d360993aa"  # whale1 ~$2.7k portfolio

# Auto-withdrawal settings (Phase 1 → Phase 2)
WITHDRAW_TRIGGER  = 1000.0   # withdraw when balance hits $1000
WITHDRAW_KEEP     = 100.0    # keep $100 on Polymarket (Phase 1)
WITHDRAW_KEEP_P2  = 500.0    # keep $500 once Phase 2 starts (after first withdrawal)
PHASE2_THRESHOLD  = 500.0    # once we've withdrawn once, keep $500 base
FLAT_BET          = 1.50     # flat bet per trade
SESSION_LOSS_PCT  = 0.25     # halt new buys if down 25% from session start


class Executor:
    def __init__(self):
        self.client = ClobClient(
            host=CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=1,  # Polymarket proxy wallet
            funder=ACCOUNT_ADDRESS,
        )
        # Derive L2 API credentials from private key
        try:
            self.creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(self.creds)
            log.info("[Executor] API credentials derived.")
        except Exception as e:
            log.error(f"[Executor] Failed to derive API creds: {e}")
            self.creds = None

        # Fetch whale cash for proportional scaling (cash only, not total portfolio)
        self._whale_cash        = self._fetch_whale_cash()
        self._whale_fetch_cycle = 0
        print(f"[Executor] Whale cash: ${self._whale_cash:,.2f}")

        # Cached balance — refreshed every 30 seconds instead of per-trade
        self._cached_balance      = self.get_balance()
        self._balance_cache_time  = datetime.now(timezone.utc)
        self._session_start_bal   = self._cached_balance
        print(f"[Executor] Session start balance: ${self._session_start_bal:.2f}")

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    def get_balance(self) -> float:
        """Get USDC balance on Polymarket in dollars."""
        try:
            resp = self.client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            raw = int(resp.get("balance", 0))
            return raw / 1e6  # USDC has 6 decimals
        except Exception as e:
            log.error(f"[Executor] Balance fetch failed: {e}")
            return 0.0

    # ------------------------------------------------------------------
    # Whale portfolio (for proportional scaling)
    # ------------------------------------------------------------------

    def _fetch_whale_cash(self) -> float:
        """Fetch whale's available cash (total portfolio minus open positions)."""
        try:
            r = requests.get(
                f"https://data-api.polymarket.com/value?user={WHALE_ADDRESS}",
                timeout=5,
            )
            data = r.json()
            total = float(data[0].get("value", 0)) if isinstance(data, list) and data else 0

            r2 = requests.get(
                f"https://data-api.polymarket.com/positions?user={WHALE_ADDRESS}&sizeThreshold=0",
                timeout=5,
            )
            positions = r2.json()
            pos_value = 0.0
            if isinstance(positions, list):
                pos_value = sum(
                    float(p.get("curPrice", 0)) * float(p.get("size", 0))
                    for p in positions
                )
            cash = total - pos_value
            if cash > 100:
                log.info(f"[Executor] Whale cash: ${cash:,.0f} (total ${total:,.0f} - positions ${pos_value:,.0f})")
                return cash
        except Exception as e:
            log.warning(f"[Executor] Could not fetch whale cash: {e}")
        return 2000.0  # fallback

    # ------------------------------------------------------------------
    # Position lookup
    # ------------------------------------------------------------------

    def _get_position_size(self, asset: str) -> float:
        """Return how many shares of `asset` we currently hold on Polymarket."""
        try:
            r = requests.get(
                f"https://data-api.polymarket.com/positions?user={ACCOUNT_ADDRESS}",
                timeout=5,
            )
            positions = r.json()
            if not isinstance(positions, list):
                return 0.0
            for p in positions:
                if str(p.get("asset", "")) == str(asset):
                    return float(p.get("size", 0))
            return 0.0
        except Exception as e:
            log.warning(f"[Executor] Position lookup failed: {e}")
            return 0.0

    # ------------------------------------------------------------------
    # Place order
    # ------------------------------------------------------------------

    def place_order(self, signal: dict) -> dict:
        """
        Place a real limit order on Polymarket CLOB.
        signal: dict from copy_trader with asset, side, outcome_idx, price, usdc_size
        Returns order response or None on failure.
        """
        asset       = signal.get("asset", "")
        side        = signal.get("side", "BUY")
        price       = signal.get("price", 0)
        usdc_size   = signal.get("usdc_size", 0)
        title       = signal.get("title", "")[:50]

        if not asset or price <= 0:
            log.warning(f"[Executor] Skipping invalid signal: {title}")
            return None
        if side == "BUY" and usdc_size <= 0:
            log.warning(f"[Executor] Skipping BUY with no usdc_size: {title}")
            return None

        # Skip only true dust — cheap tokens are the high-leverage edge, not a problem
        if side == "BUY" and price < 0.03:
            log.debug(f"[Executor] Skipping dust trade ({price:.2f}): {title}")
            return None

        # Session stop — halt new BUYs if down 25% from session start balance
        if side == "BUY":
            now = datetime.now(timezone.utc)
            if (now - self._balance_cache_time).total_seconds() > 30:
                self._cached_balance     = self.get_balance()
                self._balance_cache_time = now
            if self._cached_balance < self._session_start_bal * (1 - SESSION_LOSS_PCT):
                print(f"[LIVE] SESSION STOP — down 25% from start (${self._session_start_bal:.2f} → ${self._cached_balance:.2f})")
                log.warning(f"[Executor] Session stop triggered at ${self._cached_balance:.2f}")
                return None

        # Position dedup — don't buy the same asset again if already open
        if side == "BUY" and self._get_position_size(asset) >= 5:
            log.debug(f"[Executor] Already holding {asset[:12]}… — skipping duplicate BUY")
            return None

        # Skip signals older than 10 minutes — market likely expired
        fired_at = signal.get("fired_at")
        if fired_at:
            if fired_at.tzinfo is None:
                fired_at = fired_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - fired_at > timedelta(minutes=10):
                log.warning(f"[Executor] Skipping stale signal ({title})")
                return None

        whale_shares = float(signal.get("size", 0))

        if side == "SELL":
            # Follow whale exits immediately — any position, no minimum
            shares = self._get_position_size(asset)
            if shares <= 0:
                log.debug(f"[Executor] SELL skipped — no position for {title}")
                return None
            log.info(f"[Executor] SELL {title} — closing {shares:.2f} shares")
        else:
            # BUY — flat bet with conviction multiplier
            # Balance was already refreshed in session stop check above
            real_balance = self._cached_balance
            if real_balance < 1.0:
                log.warning(f"[Executor] Balance too low: ${real_balance:.2f}")
                return None

            # Conviction = how many same-direction bets whale fired in last 60s
            # 1 → $1.50, 2 → $3.00, 3+ → $4.50, always capped at 5% of balance
            conviction = min(signal.get("conviction", 1), 3)
            our_size   = FLAT_BET * conviction
            our_size   = min(our_size, real_balance * 0.05)

            if our_size < 0.50:
                log.debug(f"[Executor] Bet too small after cap (${our_size:.2f}): {title}")
                return None

            shares = round(our_size / price, 2)
            if shares < 5:
                # Polymarket minimum order is 5 shares — skip if we can't meet it
                log.debug(f"[Executor] Below min shares ({shares:.2f}) at price {price:.3f}: {title}")
                return None

        try:
            order_args = OrderArgs(
                token_id=asset,
                price=round(price, 4),
                size=shares,
                side=side,
            )
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, OrderType.GTC)

            display_size = shares * price if side == "SELL" else our_size  # noqa: F821
            log.info(
                f"[Executor] ORDER PLACED — {side} {title} | "
                f"${display_size:.2f} @ {price:.3f} | resp: {resp}"
            )
            print(
                f"[LIVE] {side} {signal.get('outcome','')} | {title} | "
                f"${display_size:.2f} @ {price:.3f}"
            )
            return resp

        except Exception as e:
            log.error(f"[Executor] Order failed for {title}: {e}")
            return None

    # ------------------------------------------------------------------
    # Auto-withdrawal
    # ------------------------------------------------------------------

    def check_and_withdraw(self, db) -> bool:
        """
        Check balance and withdraw profits to Phantom if threshold hit.
        Phase 1: $100 base — withdraw when balance >= $1000, keep $100
        Phase 2: $500 base — withdraw 50% when balance >= $1000
        Returns True if withdrawal was triggered.
        """
        balance = self.get_balance()
        log.info(f"[Executor] Balance check: ${balance:.2f}")

        if balance < WITHDRAW_TRIGGER:
            return False

        # Determine how much to keep based on how many withdrawals we've done
        withdrawals_done = db.db.get("withdrawals", {})
        total_withdrawn  = db.db["withdrawals"].count_documents({}) if "withdrawals" in db.db.list_collection_names() else 0

        keep    = WITHDRAW_KEEP_P2 if total_withdrawn > 0 else WITHDRAW_KEEP
        to_send = balance - keep

        if to_send < 10:
            return False

        log.info(f"[Executor] Withdrawing ${to_send:.2f} to Phantom...")
        print(f"[WITHDRAW] ${to_send:.2f} → Phantom wallet")

        success = self._withdraw_to_phantom(to_send)
        if success:
            db.db["withdrawals"].insert_one({
                "amount_usd":    to_send,
                "balance_before": balance,
                "balance_after":  keep,
                "phantom":       os.getenv("PHANTOM_WALLET_ADDRESS"),
            })
            log.info(f"[Executor] Withdrawal complete: ${to_send:.2f}")
        return success

    def _withdraw_to_phantom(self, amount_usd: float) -> bool:
        """Withdraw USDC from Polymarket to Phantom wallet."""
        phantom_address = os.getenv("PHANTOM_WALLET_ADDRESS")
        if not phantom_address:
            log.error("[Executor] No PHANTOM_WALLET_ADDRESS set.")
            return False
        try:
            amount_usdc = int(amount_usd * 1e6)  # convert to USDC units
            resp = self.client.withdraw(
                amount=amount_usdc,
                address=phantom_address,
            )
            log.info(f"[Executor] Withdrawal response: {resp}")
            return True
        except Exception as e:
            log.error(f"[Executor] Withdrawal failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            balance = self.get_balance()
            print(f"[Executor] Connected. Balance: ${balance:.2f}")
            return True
        except Exception as e:
            print(f"[Executor] Connection failed: {e}")
            return False


if __name__ == "__main__":
    ex = Executor()
    ex.ping()
