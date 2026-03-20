"""
M.A.R.T.Y — Trade Comparison Dashboard
Shows our fills vs whale's fills side-by-side.
Key metrics: entry slippage, time delay, PnL comparison.
"""

import sys, os, time
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from database.db import Database
from engine.paper_trader import WALLET
from dotenv import load_dotenv
load_dotenv()

st.set_page_config(
    page_title="M.A.R.T.Y — Comparison",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.html("""
<style>
  .stApp, [data-testid="stAppViewContainer"] { background-color: #0c0e12 !important; }
  [data-testid="stHeader"] { background-color: #0c0e12 !important; }
  [data-testid="metric-container"] {
    background: #151820; border: 1px solid #1e2230;
    border-radius: 12px; padding: 16px 20px;
  }
  [data-testid="stMetricValue"] { color: #fff; font-size: 1.4rem !important; font-weight: 700; }
  [data-testid="stMetricLabel"] { color: #8b949e; font-size: 0.7rem !important; text-transform: uppercase; }
  hr { border-color: #1e2230 !important; }
</style>
""")


@st.cache_resource
def get_db():
    return Database()


def ts_to_dt(ts):
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def fmt_delay(seconds):
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds/60:.1f}m"


def build_comparison(db) -> pd.DataFrame:
    """
    Join paper_trades → signals to build per-trade comparison rows.
    """
    trades = list(db.db["paper_trades"].find({"source": WALLET}).sort("opened_at", -1).limit(500))
    if not trades:
        return pd.DataFrame()

    # Build signal lookup by _id
    signal_ids = [t.get("signal_id") for t in trades if t.get("signal_id")]
    signals    = {s["_id"]: s for s in db.signals.find({"_id": {"$in": signal_ids}})}

    rows = []
    for t in trades:
        sig = signals.get(t.get("signal_id"), {})

        # ── Times ──────────────────────────────────────────────────────────
        whale_time = ts_to_dt(sig.get("fired_at"))
        our_time   = ts_to_dt(t.get("opened_at"))
        delay_s    = None
        if whale_time and our_time:
            delay_s = (our_time - whale_time).total_seconds()
            # Negative delay means mempool fired before REST indexed — cap at 0 for display
            delay_s = max(delay_s, 0)

        # ── Prices ─────────────────────────────────────────────────────────
        whale_entry = sig.get("price", 0)
        our_entry   = t.get("entry_price", 0)
        slip_c      = (our_entry - whale_entry) * 100          # cents difference
        slip_pct    = (slip_c / (whale_entry * 100) * 100) if whale_entry else 0  # % of whale price

        # ── PnL ────────────────────────────────────────────────────────────
        status      = t.get("status", "")
        our_pnl     = t.get("realized_pnl")   if status == "closed" else None
        our_upnl    = t.get("unrealized_pnl", 0) if status == "open" else None
        cost        = t.get("cost_usdc", 0)

        # Estimate whale PnL on same market using same close price (if resolved)
        whale_pnl = None
        if status == "closed" and whale_entry and our_entry:
            close_price   = t.get("current_price", our_entry)
            whale_usdc    = sig.get("usdc_size", 0)
            whale_shares  = (whale_usdc / whale_entry) if whale_entry else 0
            whale_pnl     = (close_price - whale_entry) * whale_shares

        # ── Row ────────────────────────────────────────────────────────────
        pnl_display = f"${our_pnl:+.2f}" if our_pnl is not None else (
                      f"${our_upnl:+.2f} (open)" if our_upnl is not None else "—")
        whale_pnl_display = f"${whale_pnl:+.2f}" if whale_pnl is not None else "—"
        pnl_pct     = f"{our_pnl/cost*100:+.1f}%" if our_pnl is not None and cost else "—"

        result = ""
        if status == "closed":
            result = "✅ WIN" if (our_pnl or 0) > 0 else "❌ LOSS"
        elif status == "pending_claim":
            result = "🕐 Claiming"
        elif status == "open":
            result = "🟡 Open"

        rows.append({
            "_opened_at":      our_time,
            "Time":            our_time.strftime("%I:%M:%S %p") if our_time else "—",
            "Market":          t.get("title", "")[:48],
            "Outcome":         t.get("outcome", ""),
            "Side":            t.get("side", ""),
            "Whale Entry":     f"{whale_entry*100:.1f}¢" if whale_entry else "—",
            "Our Entry":       f"{our_entry*100:.1f}¢"   if our_entry  else "—",
            "Slip (¢)":        f"{slip_c:+.1f}¢"         if whale_entry else "—",
            "Slip (%)":        f"{slip_pct:+.1f}%"        if whale_entry else "—",
            "Delay":           fmt_delay(delay_s),
            "Whale $":         f"${sig.get('usdc_size', 0):.2f}",
            "Our $":           f"${cost:.2f}",
            "Result":          result,
            "Our PnL":         pnl_display,
            "Our PnL %":       pnl_pct,
            "Whale Est. PnL":  whale_pnl_display,
            "_delay_s":        delay_s,
            "_slip_c":         slip_c,
            "_our_pnl":        our_pnl,
            "_whale_pnl":      whale_pnl,
            "_status":         status,
        })

    return pd.DataFrame(rows)


def render(slot, db):
    df = build_comparison(db)

    with slot.container():
        st.markdown("## 📊 M.A.R.T.Y &nbsp; `Trade Comparison`")
        st.caption(f"Whale: `{WALLET}` · {datetime.now().strftime('%I:%M:%S %p')}")
        st.divider()

        if df.empty:
            st.info("No trades yet — run the copy trader first.")
            return

        closed = df[df["_status"] == "closed"]

        # ── Summary metrics ────────────────────────────────────────────────
        avg_delay  = df["_delay_s"].dropna().mean()
        avg_slip   = df["_slip_c"].dropna().mean()
        our_total  = closed["_our_pnl"].dropna().sum()
        whale_total= closed["_whale_pnl"].dropna().sum()
        n_closed   = len(closed)
        n_wins     = (closed["_our_pnl"] > 0).sum()
        wr         = round(n_wins / n_closed * 100, 1) if n_closed else 0

        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("Avg Fill Delay",    fmt_delay(avg_delay) if avg_delay else "—")
        m2.metric("Avg Entry Slip",    f"{avg_slip:+.2f}¢"  if not pd.isna(avg_slip) else "—",
                  help="How many cents worse our entry is vs whale's")
        m3.metric("Our Total PnL",     f"${our_total:+.2f}")
        m4.metric("Whale Est. PnL",    f"${whale_total:+.2f}",
                  help="Estimated whale PnL on same markets using same close prices")
        m5.metric("PnL Gap",           f"${our_total - whale_total:+.2f}",
                  help="Difference between our PnL and whale's — slippage cost")
        m6.metric("Win Rate",          f"{wr}%", f"{n_wins}W / {n_closed - n_wins}L")

        st.divider()

        # ── Slippage analysis ──────────────────────────────────────────────
        if not closed.empty:
            good  = (closed["_slip_c"] <= 0.5).sum()
            ok    = ((closed["_slip_c"] > 0.5) & (closed["_slip_c"] <= 2.0)).sum()
            bad   = (closed["_slip_c"] > 2.0).sum()
            c1, c2, c3 = st.columns(3)
            c1.markdown(f"""
            <div style="background:#151820;border:1px solid #1e2230;border-radius:10px;padding:14px;text-align:center">
              <div style="color:#8b949e;font-size:0.7rem;text-transform:uppercase">≤0.5¢ slip (same fill)</div>
              <div style="color:#00c853;font-size:1.8rem;font-weight:700">{good}</div>
              <div style="color:#8b949e;font-size:0.72rem">trades</div>
            </div>""", unsafe_allow_html=True)
            c2.markdown(f"""
            <div style="background:#151820;border:1px solid #1e2230;border-radius:10px;padding:14px;text-align:center">
              <div style="color:#8b949e;font-size:0.7rem;text-transform:uppercase">0.5–2¢ slip</div>
              <div style="color:#f5a623;font-size:1.8rem;font-weight:700">{ok}</div>
              <div style="color:#8b949e;font-size:0.72rem">trades</div>
            </div>""", unsafe_allow_html=True)
            c3.markdown(f"""
            <div style="background:#151820;border:1px solid #1e2230;border-radius:10px;padding:14px;text-align:center">
              <div style="color:#8b949e;font-size:0.7rem;text-transform:uppercase">>2¢ slip (bad fill)</div>
              <div style="color:#ff3d3d;font-size:1.8rem;font-weight:700">{bad}</div>
              <div style="color:#8b949e;font-size:0.72rem">trades</div>
            </div>""", unsafe_allow_html=True)
            st.divider()

        # ── Trade table ────────────────────────────────────────────────────
        display_cols = [
            "Time", "Market", "Outcome", "Side",
            "Whale Entry", "Our Entry", "Slip (¢)", "Slip (%)",
            "Delay", "Whale $", "Our $",
            "Result", "Our PnL", "Our PnL %", "Whale Est. PnL",
        ]
        st.markdown("**All Trades**")

        # Tab split: closed vs open
        t1, t2 = st.tabs([f"Closed ({len(closed)})", f"Open / Pending ({len(df) - len(closed)})"])

        with t1:
            if closed.empty:
                st.caption("No closed trades yet.")
            else:
                st.dataframe(
                    closed[display_cols].reset_index(drop=True),
                    use_container_width=True,
                    height=min(60 + len(closed) * 35, 600),
                )

        with t2:
            open_df = df[df["_status"] != "closed"]
            if open_df.empty:
                st.caption("No open positions.")
            else:
                st.dataframe(
                    open_df[display_cols].reset_index(drop=True),
                    use_container_width=True,
                    height=min(60 + len(open_df) * 35, 400),
                )


def main():
    db   = get_db()
    slot = st.empty()
    while True:
        render(slot, db)
        time.sleep(5)


if __name__ == "__main__":
    main()
