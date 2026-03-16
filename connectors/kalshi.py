"""
Kalshi connector — authenticated with API Key + RSA private key.
Kalshi uses RSA-PS256 signing for all requests.
"""

import os
import time
import base64
import requests
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from dotenv import load_dotenv

load_dotenv()

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiConnector:
    def __init__(self):
        self.api_key     = os.getenv("KALSHI_API_KEY")
        raw_key          = os.getenv("KALSHI_PRIVATE_KEY", "")
        self.private_key = self._load_private_key(raw_key)
        self.session     = requests.Session()

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _load_private_key(self, raw: str):
        """
        Handles RSA key stored as a flat base64 string in .env.
        Tries PKCS#8 header first, falls back to PKCS#1.
        """
        raw = raw.strip().replace("\\n", "\n")
        # Strip any existing headers so we can re-wrap cleanly
        raw = raw.replace("-----BEGIN RSA PRIVATE KEY-----", "")
        raw = raw.replace("-----END RSA PRIVATE KEY-----", "")
        raw = raw.replace("-----BEGIN PRIVATE KEY-----", "")
        raw = raw.replace("-----END PRIVATE KEY-----", "")
        raw = raw.replace("\n", "").strip()

        for header, footer in [
            ("-----BEGIN PRIVATE KEY-----",     "-----END PRIVATE KEY-----"),
            ("-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----"),
        ]:
            pem = f"{header}\n{raw}\n{footer}\n"
            try:
                key = serialization.load_pem_private_key(
                    pem.encode(), password=None, backend=default_backend()
                )
                print("[Kalshi] Private key loaded successfully.")
                return key
            except Exception:
                continue

        print("[Kalshi] ERROR: Could not load private key with any PEM format.")
        return None

    def _sign(self, timestamp: str, method: str, path: str) -> str:
        """Generate RSA-PS256 signature for Kalshi auth header."""
        message = f"{timestamp}{method}{path}"
        signature = self.private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        """Build authenticated headers for a request."""
        ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        sig = self._sign(ts, method.upper(), path)
        return {
            "Content-Type":    "application/json",
            "KALSHI-ACCESS-KEY":       self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
        }

    def _get(self, path: str, params: dict = None):
        url = f"{KALSHI_BASE}{path}"
        full_path = f"/trade-api/v2{path}"
        headers = self._headers("GET", full_path)
        r = self.session.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_balance(self):
        """Get account balance in cents."""
        data = self._get("/portfolio/balance")
        balance_cents = data.get("balance", 0)
        return {
            "balance_cents": balance_cents,
            "balance_usd":   round(balance_cents / 100, 2)
        }

    def get_positions(self):
        """Get all open positions."""
        return self._get("/portfolio/positions")

    def get_fills(self, limit=100):
        """Get recent trade fills."""
        return self._get("/portfolio/fills", params={"limit": limit})

    # ------------------------------------------------------------------
    # Markets
    # ------------------------------------------------------------------

    def get_markets(self, limit=100, status="open", category=None):
        """Fetch open markets. category e.g. 'crypto'"""
        params = {"limit": limit, "status": status}
        if category:
            params["category"] = category
        return self._get("/markets", params=params)

    def get_market(self, ticker: str):
        """Fetch a single market by ticker."""
        return self._get(f"/markets/{ticker}")

    def get_market_orderbook(self, ticker: str, depth: int = 10):
        """Fetch live orderbook."""
        return self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    def get_market_history(self, ticker: str, limit=100):
        """Get price history for a market."""
        return self._get(f"/markets/{ticker}/history", params={"limit": limit})

    def get_crypto_markets(self):
        """Shortcut — fetch all open crypto markets."""
        return self.get_markets(limit=200, status="open", category="crypto")

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def ping(self):
        """Verify auth works by fetching balance."""
        try:
            bal = self.get_balance()
            print(f"[Kalshi] Connected. Balance: ${bal['balance_usd']}")
            return True
        except Exception as e:
            print(f"[Kalshi] Connection failed: {e}")
            return False


if __name__ == "__main__":
    k = KalshiConnector()
    k.ping()

    print("\nFetching crypto markets...")
    markets = k.get_crypto_markets()
    market_list = markets.get("markets", [])
    print(f"Found {len(market_list)} crypto markets")
    for m in market_list[:5]:
        print(f"  {m.get('ticker')} — {m.get('title')}")
