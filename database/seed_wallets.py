"""
Seeds the watched_traders collection with our target whale wallets.
Run once: python database/seed_wallets.py
"""

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database.db import Database

WALLETS = [
    {"name": "whale1", "address": "0xd0d6053c3c37e727402d84c14069780d360993aa"},
    {"name": "whale2", "address": "0x1f0ebc543b2d411f66947041625c0aa1ce61cf86"},
    {"name": "whale3", "address": "0x63ce342161250d705dc0b16df89036c8e5f9ba9a"},
    {"name": "whale4", "address": "0xa45fe11dd1420fca906ceac2c067844379a42429"},
    {"name": "whale5", "address": "0x2d8b401d2f0e6937afebf18e19e11ca568a5260a"},
]

if __name__ == "__main__":
    db = Database()
    db.ping()

    for w in WALLETS:
        db.add_watched_trader(
            address=w["address"],
            name=w["name"],
            source="manual_seed"
        )
        print(f"  Added: {w['name']} — {w['address']}")

    total = len(db.get_watched_traders())
    print(f"\nDone. {total} wallets in watched_traders collection.")
