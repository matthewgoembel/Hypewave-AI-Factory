"""
Live trade executor for Polymarket.
Uses py-clob-client to place real orders on the CLOB.
Auto-withdraws profits to Phantom wallet when balance hits threshold.
"""

import os, logging
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

# Auto-withdrawal settings (Phase 1 → Phase 2)
WITHDRAW_TRIGGER  = 1000.0   # withdraw when balance hits $1000
WITHDRAW_KEEP     = 100.0    # keep $100 on Polymarket (Phase 1)
WITHDRAW_KEEP_P2  = 500.0    # keep $500 once Phase 2 starts (after first withdrawal)
PHASE2_THRESHOLD  = 500.0    # once we've withdrawn once, keep $500 base


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

        if not asset or price <= 0 or usdc_size <= 0:
            log.warning(f"[Executor] Skipping invalid signal: {title}")
            return None

        # Skip signals older than 10 minutes — market likely expired
        fired_at = signal.get("fired_at")
        if fired_at:
            if fired_at.tzinfo is None:
                fired_at = fired_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - fired_at > timedelta(minutes=10):
                log.warning(f"[Executor] Skipping stale signal ({title})")
                return None

        # Scale trade size to our real balance proportionally
        real_balance = self.get_balance()
        if real_balance < 1.0:
            log.warning(f"[Executor] Balance too low: ${real_balance:.2f}")
            return None

        # Use same 10% max per trade as paper trader
        max_trade = real_balance * 0.10
        our_size  = min(usdc_size, max_trade)
        our_size  = max(our_size, 1.00)  # Polymarket minimum order is $1
        shares = round(our_size / price, 2)
        # Polymarket requires $1 minimum AND 5 shares minimum
        if shares < 5:
            shares = 5.0
        if shares * price < 1.00:
            shares = round(1.01 / price, 2)

        try:
            order_args = OrderArgs(
                token_id=asset,
                price=round(price, 4),
                size=shares,
                side=side,
            )
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, OrderType.GTC)

            log.info(
                f"[Executor] ORDER PLACED — {side} {title} | "
                f"${our_size:.2f} @ {price:.3f} | resp: {resp}"
            )
            print(
                f"[LIVE] {side} {signal.get('outcome','')} | {title} | "
                f"${our_size:.2f} @ {price:.3f}"
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
