"""
M.A.R.T.Y — Live Dashboard
Clean real-time view of YOUR positions and P&L.
"""

import sys, os, requests
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

ACCOUNT_ADDRESS = os.getenv("POLYMARKET_ACCOUNT_ADRESS", "")
DATA_API        = "https://data-api.polymarket.com"

st.set_page_config(
    page_title="M.A.R.T.Y",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ── Data fetchers ─────────────────────────────────────────────────────────────

def fetch_portfolio() -> float:
    try:
        r = requests.get(f"{DATA_API}/value?user={ACCOUNT_ADDRESS}", timeout=5)
        d = r.json()
        return float(d[0].get("value", 0)) if isinstance(d, list) and d else 0.0
    except Exception:
        return 0.0


def fetch_positions() -> list:
    try:
        r = requests.get(f"{DATA_API}/positions?user={ACCOUNT_ADDRESS}&sizeThreshold=0", timeout=5)
        d = r.json()
        return d if isinstance(d, list) else []
    except Exception:
        return []


def fetch_activity(limit=100) -> list:
    try:
        r = requests.get(f"{DATA_API}/activity?user={ACCOUNT_ADDRESS}&limit={limit}", timeout=5)
        d = r.json()
        return d if isinstance(d, list) else []
    except Exception:
        return []


def fmt_time(ts) -> str:
    if not ts:
        return "—"
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = ts
        return dt.strftime("%I:%M:%S %p")
    except Exception:
        return "—"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # ── Data ─────────────────────────────────────────────────────────────────
    portfolio = fetch_portfolio()
    positions = fetch_positions()
    activity  = fetch_activity()

    open_pos  = [p for p in positions if 0.03 < float(p.get("curPrice", 0.5)) < 0.97]
    resolved  = [p for p in positions if float(p.get("curPrice", 0.5)) >= 0.97 or float(p.get("curPrice", 0.5)) <= 0.03]

    # Unrealized P&L
    unrealized = sum(
        float(p.get("curPrice", 0)) * float(p.get("size", 0)) - float(p.get("initialValue", 0) or 0)
        for p in open_pos
    )

    # Realized P&L from resolved positions
    realized = sum(
        float(p.get("curPrice", 0)) * float(p.get("size", 0)) - float(p.get("initialValue", 0) or 0)
        for p in resolved
    )

    # Cash = portfolio - value of open positions
    open_value = sum(float(p.get("curPrice", 0)) * float(p.get("size", 0)) for p in open_pos)
    cash       = portfolio - open_value

    wins   = sum(1 for p in resolved if float(p.get("curPrice", 0)) >= 0.97)
    losses = sum(1 for p in resolved if float(p.get("curPrice", 0)) <= 0.03)
    total  = wins + losses
    wr     = round(wins / total * 100, 1) if total else 0.0

    # ── Header ───────────────────────────────────────────────────────────────
    st.markdown(f"## ⚡ M.A.R.T.Y &nbsp;&nbsp; <sub style='color:gray'>Live · {datetime.now().strftime('%I:%M:%S %p')}</sub>", unsafe_allow_html=True)
    st.divider()

    # ── Balance bar ──────────────────────────────────────────────────────────
    b1, b2, b3, b4, b5, b6 = st.columns(6)
    b1.metric("Portfolio",       f"${portfolio:,.2f}")
    b2.metric("Cash Available",  f"${cash:,.2f}")
    b3.metric("In Positions",    f"${open_value:,.2f}", f"{len(open_pos)} open")
    b4.metric("Unrealized PnL",  f"${unrealized:+.2f}")
    b5.metric("Realized PnL",    f"${realized:+.2f}", f"{wins}W / {losses}L")
    b6.metric("Win Rate",        f"{wr}%", f"{total} resolved")

    st.divider()

    # ── Open Positions ────────────────────────────────────────────────────────
    st.markdown(f"### 🟡 Open Positions &nbsp; `{len(open_pos)}`")

    if not open_pos:
        st.info("No open positions right now — bot is watching for next entry.")
    else:
        rows = []
        for p in open_pos:
            title     = p.get("title", p.get("market", ""))
            outcome   = p.get("outcome", "")
            size      = float(p.get("size", 0))
            cur_price = float(p.get("curPrice", 0))
            init_val  = float(p.get("initialValue", 0) or 0)
            cur_val   = cur_price * size
            upnl      = cur_val - init_val
            upnl_pct  = (upnl / init_val * 100) if init_val else 0
            rows.append({
                "Market":        title,
                "Outcome":       outcome,
                "Shares":        round(size, 2),
                "Entry Cost":    f"${init_val:.2f}",
                "Current Value": f"${cur_val:.2f}",
                "Price":         round(cur_price, 3),
                "PnL":           f"${upnl:+.2f}",
                "PnL %":         f"{upnl_pct:+.1f}%",
            })

        df = pd.DataFrame(rows)
        st.dataframe(df, width="stretch", height=min(50 + len(rows) * 38, 400))

    st.divider()

    # ── Closed / Resolved Positions ───────────────────────────────────────────
    st.markdown(f"### ✅ Closed Positions &nbsp; `{total}` &nbsp; ({wins} wins · {losses} losses)")

    if not resolved:
        st.info("No closed positions yet today.")
    else:
        rows = []
        for p in sorted(resolved, key=lambda x: float(x.get("curPrice", 0)), reverse=True):
            cur_price = float(p.get("curPrice", 0))
            init_val  = float(p.get("initialValue", 0) or 0)
            size      = float(p.get("size", 0))
            pnl       = cur_price * size - init_val
            result    = "✅ WIN" if cur_price >= 0.97 else "❌ LOSS"
            rows.append({
                "Result":   result,
                "Market":   p.get("title", p.get("market", "")),
                "Outcome":  p.get("outcome", ""),
                "Cost":     f"${init_val:.2f}",
                "PnL":      f"${pnl:+.2f}",
            })

        df = pd.DataFrame(rows)
        st.dataframe(df, width="stretch", height=min(50 + len(rows) * 38, 500))

    st.divider()

    # ── Recent On-Chain Activity ───────────────────────────────────────────────
    st.markdown("### 📋 Recent Trades Placed")

    if not activity:
        st.info("No on-chain activity found.")
    else:
        rows = []
        for a in activity[:40]:
            ts    = a.get("timestamp") or a.get("createdAt", 0)
            price = float(a.get("price", 0))
            size  = float(a.get("usdcSize", 0))
            rows.append({
                "Time":    fmt_time(ts),
                "Side":    a.get("side", ""),
                "Outcome": a.get("outcome", ""),
                "Market":  a.get("title", "")[:60],
                "Price":   round(price, 3),
                "USDC":    f"${size:.2f}",
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", height=400)

    # ── Auto-refresh ──────────────────────────────────────────────────────────
    st.caption(f"Auto-refreshing every 3s · Last update: {datetime.now().strftime('%I:%M:%S %p')}")
    st.html('<meta http-equiv="refresh" content="10">')


if __name__ == "__main__":
    main()
