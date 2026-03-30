"""Reset paper trading accounts and signals for a fresh test."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from database.db import Database
from dotenv import load_dotenv
load_dotenv()

db = Database()
db.db["paper_accounts"].delete_many({})
db.db["paper_trades"].delete_many({})
db.db["signals"].delete_many({})
db.db["watched_traders"].delete_many({})
db.db["live_trades"].delete_many({})

whales = [
    ("0xd0d6053c3c37e727402d84c14069780d360993aa", "whale1"),
]
for addr, name in whales:
    db.add_watched_trader(addr, name)
    print(f"  Watching {name}: {addr[:12]}...")

print("\nDone. Fresh $100 paper balance per whale.")
print("Run: python strategies/copy_trader.py")
