"""
MongoDB connector — single shared client for the whole app.
Database: Polymarket_Bot
Collections:
  - trades         : executed trades (ours)
  - positions      : current open positions
  - watched_traders: top trader wallets we're tracking
  - trader_activity: raw trade feed from watched wallets
  - markets        : cached market metadata
  - signals        : fired signals with outcome tracking
  - pnl_snapshots  : daily P&L history
"""

import os
from datetime import datetime, timezone
from pymongo import MongoClient, DESCENDING
from pymongo.collection import Collection
from dotenv import load_dotenv

load_dotenv()


class Database:
    def __init__(self):
        uri       = os.getenv("MONGO_DB_URI")
        db_name   = os.getenv("MONGO_DB_DATABASE_NAME", "Polymarket_Bot").strip('"')
        self.client = MongoClient(uri)
        self.db     = self.client[db_name]

        # Collections
        self.trades          : Collection = self.db["trades"]
        self.positions       : Collection = self.db["positions"]
        self.watched_traders : Collection = self.db["watched_traders"]
        self.trader_activity : Collection = self.db["trader_activity"]
        self.markets         : Collection = self.db["markets"]
        self.signals         : Collection = self.db["signals"]
        self.pnl_snapshots   : Collection = self.db["pnl_snapshots"]

        self._ensure_indexes()

    def _ensure_indexes(self):
        """Create indexes for fast lookups."""
        self.trades.create_index([("timestamp", DESCENDING)])
        self.trades.create_index("market_id")
        self.positions.create_index("market_id", unique=True)
        self.watched_traders.create_index("address", unique=True)
        self.trader_activity.create_index([("timestamp", DESCENDING)])
        self.trader_activity.create_index("trader_address")
        self.markets.create_index("market_id", unique=True)
        self.signals.create_index([("fired_at", DESCENDING)])
        self.pnl_snapshots.create_index([("date", DESCENDING)])

    # ------------------------------------------------------------------
    # Watched traders
    # ------------------------------------------------------------------

    def add_watched_trader(self, address: str, name: str = None, source: str = "leaderboard"):
        """Add a trader wallet to track."""
        self.watched_traders.update_one(
            {"address": address},
            {"$set": {
                "address":    address,
                "name":       name,
                "source":     source,
                "added_at":   datetime.now(timezone.utc),
                "active":     True
            }},
            upsert=True
        )

    def get_watched_traders(self) -> list:
        """Return all active watched trader addresses."""
        return list(self.watched_traders.find({"active": True}))

    # ------------------------------------------------------------------
    # Trader activity
    # ------------------------------------------------------------------

    def log_trader_activity(self, trader_address: str, activity: dict):
        """Store a raw trade event from a watched trader."""
        activity["trader_address"] = trader_address
        activity["recorded_at"]    = datetime.now(timezone.utc)
        self.trader_activity.insert_one(activity)

    def get_latest_activity(self, trader_address: str, limit=10) -> list:
        return list(
            self.trader_activity
            .find({"trader_address": trader_address})
            .sort("timestamp", DESCENDING)
            .limit(limit)
        )

    # ------------------------------------------------------------------
    # Trades (our own)
    # ------------------------------------------------------------------

    def log_trade(self, trade: dict):
        """Record a trade we executed."""
        trade["timestamp"] = datetime.now(timezone.utc)
        return self.trades.insert_one(trade)

    def get_recent_trades(self, limit=50) -> list:
        return list(self.trades.find().sort("timestamp", DESCENDING).limit(limit))

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def upsert_position(self, market_id: str, position: dict):
        """Update or create a position."""
        position["market_id"]   = market_id
        position["updated_at"]  = datetime.now(timezone.utc)
        self.positions.update_one(
            {"market_id": market_id},
            {"$set": position},
            upsert=True
        )

    def close_position(self, market_id: str):
        """Mark position as closed."""
        self.positions.update_one(
            {"market_id": market_id},
            {"$set": {"status": "closed", "closed_at": datetime.now(timezone.utc)}}
        )

    def get_open_positions(self) -> list:
        return list(self.positions.find({"status": {"$ne": "closed"}}))

    # ------------------------------------------------------------------
    # P&L
    # ------------------------------------------------------------------

    def snapshot_pnl(self, pnl_usd: float, balance_usd: float):
        """Save daily P&L snapshot."""
        self.pnl_snapshots.insert_one({
            "date":        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "timestamp":   datetime.now(timezone.utc),
            "pnl_usd":     pnl_usd,
            "balance_usd": balance_usd
        })

    def get_pnl_history(self, days=30) -> list:
        return list(
            self.pnl_snapshots.find().sort("timestamp", DESCENDING).limit(days)
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def ping(self):
        try:
            self.client.admin.command("ping")
            print(f"[MongoDB] Connected to database: {self.db.name}")
            return True
        except Exception as e:
            print(f"[MongoDB] Connection failed: {e}")
            return False


if __name__ == "__main__":
    db = Database()
    db.ping()
    print("Collections:", db.db.list_collection_names())
