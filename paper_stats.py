"""Show current paper trading performance."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from engine.paper_trader import PaperTrader
from dotenv import load_dotenv
load_dotenv()

pt = PaperTrader()
addr = "0xd0d6053c3c37e727402d84c14069780d360993aa"
s = pt.get_whale_summary(addr)

if not s:
    print("No paper trades yet.")
else:
    print(f"\n{'='*40}")
    print(f"  PAPER TRADER — WHALE1")
    print(f"{'='*40}")
    print(f"  Balance:        ${s['balance']:.2f}  (started $100)")
    print(f"  Realized PnL:   ${s['realized_pnl']:+.2f}")
    print(f"  Unrealized PnL: ${s['unrealized_pnl']:+.2f}")
    print(f"  Total PnL:      ${s['total_pnl']:+.2f}")
    print(f"  ROI:            {s['roi_pct']:+.1f}%")
    print(f"  Wins:           {s['wins']}")
    print(f"  Losses:         {s['losses']}")
    print(f"  Win Rate:       {s['winrate']}%")
    print(f"  Open trades:    {s['open_trades']}")
    print(f"{'='*40}\n")
