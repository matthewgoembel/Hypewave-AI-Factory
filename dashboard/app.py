"""
M.A.R.T.Y Dashboard — Live Trading
"""

import sys, os, time
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from database.db import Database
from connectors.polymarket import PolymarketConnector

st.set_page_config(
    page_title="M.A.R.T.Y — Live Trading",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
  .stApp { background-color: #0d1117; color: #e6edf3; }
  [data-testid="metric-container"] {
    background-color: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 12px 16px;
  }
  [data-testid="stMetricValue"] { color: #58a6ff; font-size: 1.6rem !important; font-weight: 700; }
  [data-testid="stMetricLabel"] { color: #8b949e; font-size: 0.75rem !important; text-transform: uppercase; }
  [data-testid="stMetricDelta"] { font-size: 0.85rem !important; }
  .section-header {
    color: #58a6ff; font-size: 0.7rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.1em;
    border-bottom: 1px solid #21262d; padding-bottom: 6px; margin-bottom: 10px;
  }
  .signal-card {
    background-color: #161b22; border: 1px solid #30363d;
    border-radius: 8px; padding: 12px 14px; margin-bottom: 8px; font-size: 0.82rem;
  }
  .signal-card.buy  { border-left: 3px solid #3fb950; }
  .signal-card.sell { border-left: 3px solid #f85149; }
  .buy-tag  { color: #3fb950; font-weight: 700; }
  .sell-tag { color: #f85149; font-weight: 700; }
  .up-tag   { color: #3fb950; }
  .dn-tag   { color: #f85149; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.7rem; font-weight: 700; }
  .badge-exec    { background: #1f4d2e; color: #3fb950; }
  .badge-pending { background: #3d2e00; color: #d29922; }
  .badge-failed  { background: #4d1f1f; color: #f85149; }
  .badge-skipped { background: #2d2d2d; color: #8b949e; }
  .whale-card { background-color: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; margin-bottom: 10px; }
  .trade-row { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; border-bottom: 1px solid #21262d; font-size: 0.78rem; }
  hr { border-color: #21262d; }
  .refresh-note { color: #8b949e; font-size: 0.7rem; }
</style>
""", unsafe_allow_html=True)


WHALE_NAMES = {
    "0xd0d6053c3c37e727402d84c14069780d360993aa": "WHALE1",
}
ACCOUNT_ADDRESS = os.getenv("POLYMARKET_ACCOUNT_ADRESS", "")


@st.cache_resource
def get_db():
    from dotenv import load_dotenv
    load_dotenv()
    return Database()

@st.cache_resource
def get_pm():
    return PolymarketConnector()


def fmt_ts(ts):
    if ts is None:
        return "—"
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    elif isinstance(ts, datetime):
        dt = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    else:
        return str(ts)
    return dt.strftime("%m/%d %H:%M:%S")


def get_live_balance():
    """Fetch live USDC balance from Polymarket CLOB."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        from py_clob_client.constants import POLYGON
        import os
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=os.getenv("POLYMARKET_WALLET_PRIVATE_KEY"),
            chain_id=POLYGON,
            signature_type=1,
            funder=os.getenv("POLYMARKET_ACCOUNT_ADRESS"),
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        resp = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return int(resp.get("balance", 0)) / 1e6
    except Exception:
        return None


def load_live_stats(db):
    executed = db.signals.count_documents({"status": "executed"})
    failed   = db.signals.count_documents({"status": "failed"})
    pending  = db.signals.count_documents({"status": "pending"})
    skipped  = db.signals.count_documents({"status": "skipped"})
    return executed, failed, pending, skipped


def load_executed_trades(db, limit=100):
    return list(db.signals.find({"status": "executed"}).sort("fired_at", -1).limit(limit))


def load_recent_signals(db, limit=30):
    return list(db.signals.find(
        {"status": {"$in": ["executed", "pending", "failed"]}}
    ).sort("fired_at", -1).limit(limit))


def load_whale_feed(db, address, limit=10):
    return list(
        db.trader_activity
        .find({"trader_address": address})
        .sort("timestamp", -1)
        .limit(limit)
    )


def main():
    from dotenv import load_dotenv
    load_dotenv()

    db = get_db()

    # ── Header ──────────────────────────────────────────────────────────
    col_title, col_badge, col_time = st.columns([4, 1, 1])
    with col_title:
        st.markdown("## 🤖 M.A.R.T.Y &nbsp; <span style='color:#8b949e;font-size:1rem'>Live Copy Trader</span>", unsafe_allow_html=True)
    with col_badge:
        st.markdown('<span class="badge badge-exec" style="font-size:0.85rem;padding:4px 12px">LIVE</span>', unsafe_allow_html=True)
    with col_time:
        st.markdown(f'<div class="refresh-note">Updated<br>{datetime.now().strftime("%H:%M:%S")}</div>', unsafe_allow_html=True)

    st.markdown("---")

    # ── Key metrics ─────────────────────────────────────────────────────
    balance = get_live_balance()
    executed, failed, pending, skipped = load_live_stats(db)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Live Balance",    f"${balance:.2f}" if balance is not None else "N/A")
    c2.metric("Orders Executed", executed)
    c3.metric("Orders Failed",   failed)
    c4.metric("Pending",         pending)
    c5.metric("Skipped/Stale",   skipped)

    st.markdown("---")

    # ── Tabs ─────────────────────────────────────────────────────────────
    tab_feed, tab_trades, tab_whale = st.tabs(["Live Feed", "Executed Trades", "WHALE1 Activity"])

    # ── Live Feed ────────────────────────────────────────────────────────
    with tab_feed:
        col_signals, col_whale = st.columns([1.1, 0.9])

        with col_signals:
            st.markdown('<div class="section-header">Recent Signals</div>', unsafe_allow_html=True)
            signals = load_recent_signals(db, limit=30)
            if not signals:
                st.markdown('<div style="color:#8b949e">No signals yet.</div>', unsafe_allow_html=True)
            for s in signals:
                side    = s.get("side", "BUY")
                outcome = s.get("outcome", "")
                title   = s.get("title", "Unknown")[:55]
                price   = s.get("price", 0)
                usdc    = s.get("usdc_size", 0)
                status  = s.get("status", "pending")
                ts      = fmt_ts(s.get("fired_at"))
                resp    = s.get("order_resp", "")

                card_cls  = "buy" if side == "BUY" else "sell"
                side_html = f'<span class="buy-tag">{side}</span>' if side == "BUY" else f'<span class="sell-tag">{side}</span>'
                out_html  = f'<span class="up-tag">{outcome}</span>' if outcome in ("Up","YES") else f'<span class="dn-tag">{outcome}</span>'
                badge_cls = {"executed":"badge-exec","pending":"badge-pending","failed":"badge-failed"}.get(status,"badge-skipped")
                badge_html = f'<span class="badge {badge_cls}">{status}</span>'

                order_note = ""
                if status == "executed" and resp:
                    order_note = f'<span style="color:#3fb950;font-size:0.7rem">✓ order placed</span>'
                elif status == "failed" and resp:
                    err = str(resp)[:60]
                    order_note = f'<span style="color:#f85149;font-size:0.7rem">{err}</span>'

                st.markdown(f"""
                <div class="signal-card {card_cls}">
                  <div style="display:flex;justify-content:space-between;margin-bottom:4px">
                    <span>{side_html} {out_html} &nbsp;<strong style="color:#e6edf3">{title}</strong></span>
                    <span style="color:#8b949e;font-size:0.7rem">{ts}</span>
                  </div>
                  <div style="display:flex;gap:16px;color:#8b949e;align-items:center">
                    <span>Price: <strong style="color:#e6edf3">{price:.3f}</strong></span>
                    <span>USDC: <strong style="color:#e6edf3">${usdc:.2f}</strong></span>
                    {badge_html}&nbsp;{order_note}
                  </div>
                </div>
                """, unsafe_allow_html=True)

        with col_whale:
            st.markdown('<div class="section-header">WHALE1 Raw Activity</div>', unsafe_allow_html=True)
            whale_addr = "0xd0d6053c3c37e727402d84c14069780d360993aa"
            feed = load_whale_feed(db, whale_addr, limit=15)
            if not feed:
                st.markdown('<div style="color:#8b949e">No activity yet.</div>', unsafe_allow_html=True)
            for t in feed:
                side    = t.get("side", "")
                outcome = t.get("outcome", "")
                title   = t.get("title", "")[:40]
                price   = t.get("price", 0)
                size    = t.get("size", 0)
                ts      = fmt_ts(t.get("timestamp"))
                side_html = f'<span class="buy-tag">BUY</span>' if side == "BUY" else f'<span class="sell-tag">SELL</span>'
                out_html  = f'<span class="up-tag">{outcome}</span>' if outcome in ("Up","YES") else f'<span class="dn-tag">{outcome}</span>'
                st.markdown(f"""
                <div class="trade-row">
                  <span>{side_html} {out_html} <span style="color:#e6edf3">{title}</span></span>
                  <span style="color:#8b949e">{size:.1f}@ {price:.3f} {ts}</span>
                </div>
                """, unsafe_allow_html=True)

    # ── Executed Trades ──────────────────────────────────────────────────
    with tab_trades:
        st.markdown('<div class="section-header">All Executed Orders</div>', unsafe_allow_html=True)
        trades = load_executed_trades(db, limit=200)
        if not trades:
            st.markdown('<div style="color:#8b949e">No executed trades yet.</div>', unsafe_allow_html=True)
        else:
            rows = []
            for t in trades:
                rows.append({
                    "Time":    fmt_ts(t.get("fired_at")),
                    "Market":  t.get("title","")[:50],
                    "Side":    t.get("side",""),
                    "Outcome": t.get("outcome",""),
                    "Price":   round(t.get("price",0), 3),
                    "USDC":    f"${t.get('usdc_size',0):.2f}",
                    "Whale":   t.get("source_name",""),
                    "TX":      str(t.get("order_resp",""))[:40],
                })
            df = pd.DataFrame(rows)
            st.dataframe(df, width='stretch', height=600)
            st.markdown(f'<div class="refresh-note">{len(trades)} executed orders</div>', unsafe_allow_html=True)

    # ── Whale Activity ───────────────────────────────────────────────────
    with tab_whale:
        whale_addr = "0xd0d6053c3c37e727402d84c14069780d360993aa"
        feed = load_whale_feed(db, whale_addr, limit=50)
        if not feed:
            st.markdown('<div style="color:#8b949e">No activity yet.</div>', unsafe_allow_html=True)
        else:
            rows = []
            for t in feed:
                rows.append({
                    "Time":    fmt_ts(t.get("timestamp")),
                    "Market":  t.get("title","")[:50],
                    "Side":    t.get("side",""),
                    "Outcome": t.get("outcome",""),
                    "Price":   round(t.get("price",0), 3),
                    "Size":    round(t.get("size",0), 2),
                    "USDC":    f"${t.get('usdcSize', t.get('usdc_size',0)):.2f}",
                })
            df = pd.DataFrame(rows)
            st.dataframe(df, width='stretch', height=600)

    # Auto-refresh every 15 seconds
    st.markdown("---")
    st.markdown('<div class="refresh-note">Auto-refreshes every 15 seconds</div>', unsafe_allow_html=True)
    time.sleep(15)
    st.rerun()


if __name__ == "__main__":
    main()
