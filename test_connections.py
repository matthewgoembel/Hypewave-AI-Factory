"""
Run this first to verify all API connections work before building anything else.
  python test_connections.py
"""

from database.db import Database
from connectors.polymarket import PolymarketConnector
from connectors.kalshi import KalshiConnector

print("=" * 50)
print("MARTY — Connection Test")
print("=" * 50)

print("\n[1] MongoDB...")
db = Database()
db.ping()

print("\n[2] Polymarket...")
pm = PolymarketConnector()
if pm.ping():
    print("  Fetching top 5 traders...")
    try:
        leaders = pm.get_leaderboard(limit=5)
        if isinstance(leaders, list):
            for i, t in enumerate(leaders, 1):
                print(f"    {i}. {t.get('name', 'anon')} — ${t.get('profit', 0):,.0f}")
        else:
            print("  Response:", leaders)
    except Exception as e:
        print(f"  Leaderboard error: {e}")

print("\n[3] Kalshi...")
k = KalshiConnector()
k.ping()

print("\n" + "=" * 50)
print("Done.")
