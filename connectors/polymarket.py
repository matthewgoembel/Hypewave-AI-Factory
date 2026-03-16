"""
Polymarket connector — fetches markets, positions, and top trader activity.
Uses the Polymarket CLOB API + Gamma API for market data.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE  = "https://data-api.polymarket.com"


class PolymarketConnector:
    def __init__(self):
        self.api_key    = os.getenv("POLYMARKET_API_KEY")
        self.api_secret = os.getenv("POLYMARKET_API_SECRET")
        self.passphrase = os.getenv("POLYMARKET_PASSPHRASE")
        self.wallet     = os.getenv("POLYMARKET_WALLET_ADDRESS")
        self.session    = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    # ------------------------------------------------------------------
    # Markets
    # ------------------------------------------------------------------

    def get_markets(self, limit=50, active_only=True):
        """Fetch open prediction markets."""
        params = {"limit": limit}
        if active_only:
            params["active"] = "true"
        r = self.session.get(f"{GAMMA_BASE}/markets", params=params)
        r.raise_for_status()
        return r.json()

    def get_market(self, condition_id: str):
        """Fetch a single market by condition ID."""
        r = self.session.get(f"{CLOB_BASE}/markets/{condition_id}")
        r.raise_for_status()
        return r.json()

    def get_market_orderbook(self, token_id: str):
        """Fetch live orderbook for a market token."""
        r = self.session.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Top traders / copy trading targets
    # ------------------------------------------------------------------

    def get_leaderboard(self, limit=50):
        """
        Fetch top traders by profit on Polymarket.
        Returns list of {name, address, pnl, volume}.
        """
        r = self.session.get(
            f"{DATA_BASE}/leaderboard",
            params={"limit": limit}
        )
        r.raise_for_status()
        return r.json()

    def get_wallet_positions(self, wallet_address: str):
        """
        Get all open positions for a wallet address.
        This is how we see what a top trader currently holds.
        """
        r = self.session.get(
            f"{DATA_BASE}/positions",
            params={"user": wallet_address, "sizeThreshold": 0}
        )
        r.raise_for_status()
        return r.json()

    def get_wallet_trades(self, wallet_address: str, limit=100):
        """
        Get recent trade history for a wallet.
        Used to detect when a top trader makes a new move.
        """
        r = self.session.get(
            f"{DATA_BASE}/activity",
            params={"user": wallet_address, "limit": limit}
        )
        r.raise_for_status()
        return r.json()

    def get_wallet_pnl(self, wallet_address: str):
        """Get profit/loss stats for a wallet."""
        r = self.session.get(
            f"{DATA_BASE}/profiles/{wallet_address}"
        )
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Price data
    # ------------------------------------------------------------------

    def get_price_history(self, market_id: str, interval: str = "1d"):
        """Fetch historical prices for a market. interval: 1m, 5m, 1h, 1d"""
        r = self.session.get(
            f"{CLOB_BASE}/prices-history",
            params={"market": market_id, "interval": interval, "fidelity": 60}
        )
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def ping(self):
        """Verify API connectivity."""
        try:
            r = self.session.get(f"{CLOB_BASE}/ok")
            return r.status_code == 200
        except Exception as e:
            print(f"Polymarket ping failed: {e}")
            return False


if __name__ == "__main__":
    pm = PolymarketConnector()
    print("Polymarket connected:", pm.ping())

    print("\nFetching leaderboard...")
    leaders = pm.get_leaderboard(limit=10)
    if isinstance(leaders, list):
        for i, trader in enumerate(leaders[:5], 1):
            print(f"  {i}. {trader.get('name', 'anon')} — ${trader.get('profit', 0):,.0f} profit")
    else:
        print(leaders)
