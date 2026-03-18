"""
Claim Monitor — checks for claimable Polymarket winnings every 5 minutes.
Prints a loud alert when there's money to claim so you can go to the browser and hit Claim.
Run: python engine/claim_monitor.py
"""

import sys, os, time, requests
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from dotenv import load_dotenv
load_dotenv()

ACCOUNT = os.getenv("POLYMARKET_ACCOUNT_ADRESS", "")
CHECK_INTERVAL = 300  # 5 minutes
MIN_ALERT_USD  = 1.0  # only alert if > $1 to claim

def get_claimable():
    """Returns list of redeemable positions with value > 0."""
    try:
        r = requests.get(
            f"https://data-api.polymarket.com/positions?user={ACCOUNT}&redeemable=true",
            timeout=10
        )
        positions = r.json()
        claimable = [p for p in positions if p.get("curPrice", 0) >= 0.99]
        return claimable, len(positions)
    except Exception as e:
        print(f"[MONITOR] API error: {e}")
        return [], 0

def get_balance():
    """Returns available cash balance from Polymarket."""
    try:
        r = requests.get(
            f"https://data-api.polymarket.com/value?user={ACCOUNT}",
            timeout=10
        )
        data = r.json()
        if isinstance(data, list) and data:
            return data[0].get("value", 0)
        return 0
    except Exception:
        return 0

def print_alert(claimable, total_value):
    print("\n" + "=" * 60)
    print("  💰  CLAIM YOUR WINNINGS ON POLYMARKET!")
    print("=" * 60)
    print(f"  Total claimable: ${total_value:.2f}")
    print(f"  Positions ready: {len(claimable)}")
    for p in claimable:
        title = p.get("title", "Unknown")[:50]
        val   = p.get("currentValue", p.get("size", 0))
        print(f"    • {title}: ${val:.2f}")
    print("=" * 60)
    print("  → Go to polymarket.com and click CLAIM")
    print("=" * 60 + "\n")

def main():
    print(f"[MONITOR] Watching {ACCOUNT[:10]}... for claimable winnings")
    print(f"[MONITOR] Checking every {CHECK_INTERVAL // 60} minutes. Press Ctrl+C to stop.\n")

    last_alert_value = 0

    while True:
        claimable, total_redeemable = get_claimable()

        if claimable:
            total_value = sum(
                p.get("currentValue", p.get("size", 0)) for p in claimable
            )
            if total_value >= MIN_ALERT_USD:
                print_alert(claimable, total_value)
                last_alert_value = total_value
            else:
                print(f"[MONITOR] {len(claimable)} claimable (${total_value:.2f}) — below threshold, skipping alert")
        else:
            # Just show status
            bal = get_balance()
            print(f"[MONITOR] No claimable winnings right now. "
                  f"Total redeemable positions: {total_redeemable} | "
                  f"Portfolio value: ${bal:.2f}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
