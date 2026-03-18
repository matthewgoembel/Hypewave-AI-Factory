"""
M.A.R.T.Y — Paper Trading Dashboard
Single-wallet, Polymarket-style real-time view.
"""

import sys, os, time
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from database.db import Database
from engine.paper_trader import PaperTrader, WALLET, BUDGET
from dotenv import load_dotenv
load_dotenv()

st.set_page_config(
    page_title="M.A.R.T.Y — Paper",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.html("""
<style>
  .stApp, [data-testid="stAppViewContainer"] { background-color: #0c0e12 !important; }
  [data-testid="stHeader"] { background-color: #0c0e12 !important; }
  [data-testid="metric-container"] {
    background: #151820;
    border: 1px solid #1e2230;
    border-radius: 12px;
    padding: 16px 20px;
  }
  [data-testid="stMetricValue"] { color: #ffffff; font-size: 1.4rem !important; font-weight: 700; }
  [data-testid="stMetricLabel"] { color: #8b949e; font-size: 0.7rem !important; text-transform: uppercase; letter-spacing: 0.08em; }
  .stTabs [data-baseweb="tab-list"] { background-color: #151820; border-radius: 8px; padding: 4px; gap: 4px; }
  .stTabs [data-baseweb="tab"] { border-radius: 6px; color: #8b949e; font-weight: 600; font-size: 0.85rem; padding: 6px 16px; }
  .stTabs [aria-selected="true"] { background-color: #1e2230 !important; color: #ffffff !important; }
  hr { border-color: #1e2230 !important; }
  .stDataFrame { background: #151820; }
</style>
""")


def fmt_ts(ts):
    if ts is None:
        return "—"
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        return dt.strftime("%I:%M:%S %p")
    except Exception:
        return "—"


def pnl_color(val: float) -> str:
    return "#00c853" if val >= 0 else "#ff3d3d"


@st.cache_resource
def get_db():
    return Database()

@st.cache_resource
def get_pt():
    return PaperTrader()


def render_dashboard(slot, db, pt):
    s = pt.get_summary()
    if not s:
        slot.info("No account data yet. Run: python reset_paper.py")
        return

    balance   = s["balance"]
    portfolio = s["portfolio"]
    open_val  = s["open_value"]
    realized  = s["realized_pnl"]
    unreal    = s["unrealized_pnl"]
    total_pnl = s["total_pnl"]
    wins      = s["wins"]
    losses    = s["losses"]
    wr        = s["winrate"]
    roi       = s["roi_pct"]
    n_open    = s["open_trades"]
    n_pending = s["pending_claims"]
    pc        = pnl_color(total_pnl)
    now       = datetime.now().strftime("%I:%M:%S %p")

    with slot.container():
        # ── Header ────────────────────────────────────────────────────────────
        st.markdown(f"""
        <div style="display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:4px">
          <div>
            <div style="color:#8b949e;font-size:0.72rem;text-transform:uppercase;letter-spacing:0.1em">Portfolio Value</div>
            <div style="font-size:2.8rem;font-weight:700;color:#ffffff;line-height:1.1">${portfolio:,.2f}</div>
            <div style="font-size:0.88rem;color:{pc};font-weight:600;margin-top:2px">
              {'+' if total_pnl >= 0 else ''}{total_pnl:.2f} ({roi:+.1f}%) all time
            </div>
          </div>
          <div style="color:#8b949e;font-size:0.72rem">{now}</div>
        </div>
        """, unsafe_allow_html=True)

        st.divider()

        # ── Metric bar ────────────────────────────────────────────────────────
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("Cash Available",  f"${balance:,.2f}")
        m2.metric("In Positions",    f"${open_val:,.2f}", f"{n_open} open{f' + {n_pending} claiming' if n_pending else ''}")
        m3.metric("Realized PnL",    f"${realized:+.2f}")
        m4.metric("Unrealized PnL",  f"${unreal:+.2f}")
        m5.metric("Win Rate",        f"{wr}%",  f"{wins}W / {losses}L")
        m6.metric("ROI",             f"{roi:+.1f}%", f"from ${BUDGET:.0f}")

        st.divider()

        # ── Tabs ──────────────────────────────────────────────────────────────
        t1, t2, t3 = st.tabs([
            f"Positions  ({n_open + n_pending})",
            f"Closed  ({wins + losses})",
            "Activity Feed",
        ])

        # ── Open Positions ────────────────────────────────────────────────────
        with t1:
            open_trades = list(
                db.db["paper_trades"].find(
                    {"source": WALLET, "status": {"$in": ["open", "pending_claim"]}}
                ).sort("opened_at", -1)
            )
            if not open_trades:
                st.caption("No open positions.")
            else:
                rows = []
                for t in open_trades:
                    cost  = t.get("cost_usdc", 0)
                    upnl  = t.get("unrealized_pnl", 0)
                    stat  = "🕐 Claiming" if t["status"] == "pending_claim" else "🟡 Open"
                    rows.append({
                        "Status":  stat,
                        "Market":  t.get("title", "")[:55],
                        "Outcome": t.get("outcome", ""),
                        "Entry":   f"{t.get('entry_price', 0)*100:.1f}¢",
                        "Now":     f"{t.get('current_price', t.get('entry_price', 0))*100:.1f}¢",
                        "Cost":    f"${cost:.2f}",
                        "PnL":     f"${upnl:+.2f}",
                        "PnL %":   f"{(upnl/cost*100):+.1f}%" if cost else "—",
                        "Opened":  fmt_ts(t.get("opened_at")),
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, height=min(60 + len(rows) * 35, 450))

        # ── Closed Positions ──────────────────────────────────────────────────
        with t2:
            closed = list(
                db.db["paper_trades"].find({"source": WALLET, "status": "closed"})
                .sort("closed_at", -1).limit(200)
            )
            if not closed:
                st.caption("No closed trades yet.")
            else:
                rows = []
                for t in closed:
                    p = t.get("realized_pnl", 0)
                    rows.append({
                        "Result":  "✅ WIN" if p > 0 else "❌ LOSS",
                        "Time":    fmt_ts(t.get("closed_at")),
                        "Market":  t.get("title", "")[:55],
                        "Outcome": t.get("outcome", ""),
                        "Entry":   f"{t.get('entry_price', 0)*100:.1f}¢",
                        "Cost":    f"${t.get('cost_usdc', 0):.2f}",
                        "PnL":     f"${p:+.2f}",
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, height=min(60 + len(rows) * 35, 500))

        # ── Activity Feed ─────────────────────────────────────────────────────
        with t3:
            all_trades = list(
                db.db["paper_trades"].find({"source": WALLET})
                .sort("opened_at", -1).limit(200)
            )
            sigs = list(
                db.signals.find({"source": WALLET})
                .sort("fired_at", -1).limit(200)
            )

            events = []

            for t in all_trades:
                status = t.get("status", "")
                if status == "closed":
                    p    = t.get("realized_pnl", 0)
                    won  = p > 0
                    events.append({
                        "_sort": t.get("closed_at") or t.get("opened_at"),
                        "Time":   fmt_ts(t.get("closed_at")),
                        "Event":  "🏆 REDEEM" if won else "❌ LOSS",
                        "Market": t.get("title", "")[:55],
                        "Outcome": t.get("outcome", ""),
                        "Amount": f"${abs(p):+.2f}",
                        "Detail": f"PnL {'+' if p>=0 else ''}{p:.2f}",
                    })
                elif status in ("open", "pending_claim"):
                    events.append({
                        "_sort": t.get("opened_at"),
                        "Time":   fmt_ts(t.get("opened_at")),
                        "Event":  "🕐 CLAIMING" if status == "pending_claim" else "🟢 BUY",
                        "Market": t.get("title", "")[:55],
                        "Outcome": t.get("outcome", ""),
                        "Amount": f"${t.get('cost_usdc', 0):.2f}",
                        "Detail": f"@ {t.get('entry_price', 0)*100:.1f}¢",
                    })

            for s in sigs:
                if s.get("status") == "skipped":
                    events.append({
                        "_sort": s.get("fired_at"),
                        "Time":   fmt_ts(s.get("fired_at")),
                        "Event":  "⏭ SKIPPED",
                        "Market": s.get("title", "")[:55],
                        "Outcome": s.get("outcome", ""),
                        "Amount": f"${s.get('usdc_size', 0):.2f}",
                        "Detail": s.get("skip_reason", ""),
                    })

            if not events:
                st.caption("No activity yet.")
            else:
                events.sort(key=lambda x: x["_sort"] if x["_sort"] else datetime.min.replace(tzinfo=timezone.utc), reverse=True)
                rows = [{k: v for k, v in e.items() if k != "_sort"} for e in events[:200]]
                st.dataframe(pd.DataFrame(rows), use_container_width=True, height=500)


def main():
    db = get_db()
    pt = get_pt()

    st.markdown("## 📄 M.A.R.T.Y &nbsp; `Paper Trader`")
    st.caption(f"Wallet: `{WALLET}`")
    st.divider()

    slot = st.empty()

    while True:
        render_dashboard(slot, db, pt)
        time.sleep(3)


if __name__ == "__main__":
    main()
