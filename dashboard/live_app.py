"""
M.A.R.T.Y — Live Trade Dashboard
Same as comparison dashboard but for real money trades.
"""

import sys, os, time
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from database.db import Database
from dotenv import load_dotenv
load_dotenv()

WHALE   = "0xd0d6053c3c37e727402d84c14069780d360993aa"
ACCOUNT = os.getenv("POLYMARKET_ACCOUNT_ADRESS", "")

st.set_page_config(
    page_title="M.A.R.T.Y — Live",
    page_icon="⚡",
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


def build_df(db) -> pd.DataFrame:
    trades = list(db.db["live_trades"].find().sort("opened_at", -1).limit(500))
    if not trades:
        return pd.DataFrame()

    signal_ids = [t.get("signal_id") for t in trades if t.get("signal_id")]
    signals    = {s["_id"]: s for s in db.signals.find({"_id": {"$in": signal_ids}})}

    rows = []
    for t in trades:
        sig = signals.get(t.get("signal_id"), {})

        whale_time = ts_to_dt(sig.get("fired_at"))
        our_time   = ts_to_dt(t.get("opened_at"))
        delay_s    = None
        if whale_time and our_time:
            delay_s = max((our_time - whale_time).total_seconds(), 0)

        whale_entry = sig.get("price", 0)
        our_entry   = t.get("entry_price", 0)
        slip_c      = (our_entry - whale_entry) * 100
        slip_pct    = (slip_c / (whale_entry * 100) * 100) if whale_entry else 0

        status   = t.get("status", "open")
        our_pnl  = t.get("realized_pnl")    if status == "closed" else None
        our_upnl = t.get("unrealized_pnl", 0) if status == "open"   else None
        cost     = t.get("cost_usdc", 0)

        whale_pnl = None
        if status == "closed" and whale_entry:
            close_price  = t.get("close_price", our_entry)
            whale_usdc   = sig.get("usdc_size", 0)
            whale_shares = (whale_usdc / whale_entry) if whale_entry else 0
            whale_pnl    = (close_price - whale_entry) * whale_shares

        pnl_display       = f"${our_pnl:+.2f}" if our_pnl is not None else (
                            f"${our_upnl:+.2f} (open)" if our_upnl is not None else "—")
        whale_pnl_display = f"${whale_pnl:+.2f}" if whale_pnl is not None else "—"
        pnl_pct           = f"{our_pnl/cost*100:+.1f}%" if our_pnl is not None and cost else "—"

        if status == "closed":
            result = "✅ WIN" if (our_pnl or 0) > 0 else "❌ LOSS"
        elif status == "open":
            result = "🟡 Open"
        else:
            result = "—"

        rows.append({
            "_opened_at":     our_time,
            "Time":           our_time.strftime("%I:%M:%S %p") if our_time else "—",
            "Market":         t.get("title", "")[:48],
            "Outcome":        t.get("outcome", ""),
            "Side":           t.get("side", "BUY"),
            "Whale Entry":    f"{whale_entry*100:.1f}¢" if whale_entry else "—",
            "Our Entry":      f"{our_entry*100:.1f}¢"  if our_entry  else "—",
            "Slip (¢)":       f"{slip_c:+.1f}¢"        if whale_entry else "—",
            "Slip (%)":       f"{slip_pct:+.1f}%"       if whale_entry else "—",
            "Delay":          fmt_delay(delay_s),
            "Whale $":        f"${sig.get('usdc_size', 0):.2f}",
            "Our $":          f"${cost:.2f}",
            "Result":         result,
            "Our PnL":        pnl_display,
            "Our PnL %":      pnl_pct,
            "Whale Est. PnL": whale_pnl_display,
            "_delay_s":       delay_s,
            "_slip_c":        slip_c,
            "_our_pnl":       our_pnl,
            "_whale_pnl":     whale_pnl,
            "_status":        status,
            "_cost":          cost,
        })

    return pd.DataFrame(rows)


def render(slot, db):
    df = build_df(db)

    with slot.container():
        st.markdown("## ⚡ M.A.R.T.Y &nbsp; `Live Trading`")
        st.caption(f"Wallet: `{ACCOUNT or 'not set'}` · Whale: `{WHALE[:20]}…` · {datetime.now().strftime('%I:%M:%S %p')}")
        st.divider()

        if df.empty:
            st.info("No live trades yet — run: `python strategies/copy_trader.py --live`")
            return

        closed = df[df["_status"] == "closed"]
        open_  = df[df["_status"] == "open"]

        # ── Summary metrics ────────────────────────────────────────────────
        avg_delay   = df["_delay_s"].dropna().mean()
        avg_slip    = df["_slip_c"].dropna().mean()
        our_total   = closed["_our_pnl"].dropna().sum()
        whale_total = closed["_whale_pnl"].dropna().sum()
        open_value  = open_["_cost"].sum()
        n_closed    = len(closed)
        n_wins      = int((closed["_our_pnl"] > 0).sum()) if not closed.empty else 0
        wr          = round(n_wins / n_closed * 100, 1) if n_closed else 0
        roi         = our_total / 100 * 100

        m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
        m1.metric("Avg Fill Delay",  fmt_delay(avg_delay) if avg_delay else "—")
        m2.metric("Avg Entry Slip",  f"{avg_slip:+.2f}¢" if not pd.isna(avg_slip) else "—",
                  help="How many cents worse our entry is vs whale's")
        m3.metric("Realized PnL",    f"${our_total:+.2f}")
        m4.metric("Open Exposure",   f"${open_value:.2f}", f"{len(open_)} positions")
        m5.metric("Whale Est. PnL",  f"${whale_total:+.2f}",
                  help="Estimated whale PnL on same markets")
        m6.metric("Win Rate",        f"{wr}%", f"{n_wins}W / {n_closed - n_wins}L")
        m7.metric("ROI",             f"{roi:+.1f}%", "from $100")

        st.divider()

        # ── Slippage breakdown ─────────────────────────────────────────────
        if not closed.empty:
            good = int((closed["_slip_c"] <= 0.5).sum())
            ok   = int(((closed["_slip_c"] > 0.5) & (closed["_slip_c"] <= 2.0)).sum())
            bad  = int((closed["_slip_c"] > 2.0).sum())
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
        t1, t2 = st.tabs([f"Closed ({len(closed)})", f"Open ({len(open_)})"])

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
            if open_.empty:
                st.caption("No open positions.")
            else:
                st.dataframe(
                    open_[display_cols].reset_index(drop=True),
                    use_container_width=True,
                    height=min(60 + len(open_) * 35, 400),
                )


def main():
    db   = get_db()
    slot = st.empty()
    while True:
        render(slot, db)
        time.sleep(5)


if __name__ == "__main__":
    main()
