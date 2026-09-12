"""
M&A Target Screener — Streamlit analyst UI.

Framing: this ranks a mid-cap universe on *proxies* historically associated
with takeout chatter. It is not a prediction that any name will be acquired.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import database as db
import scoring
from scoring import DEFAULT_WEIGHTS, SIGNAL_KEYS, normalize_weights, weighted_composite

st.set_page_config(
    page_title="M&A Target Screener",
    page_icon="◎",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Light slate + teal — resume-screenshot palette (matches .streamlit/config.toml).
CSS = """
<style>
    .block-container { padding-top: 1.4rem; padding-bottom: 2rem; max-width: 1400px; }
    h1 { letter-spacing: -0.02em; color: #0f172a; }
    .disclaimer {
        color: #475569; font-size: 0.92rem; margin-top: -0.4rem; margin-bottom: 1.2rem;
    }
    .chip {
        display: inline-block; padding: 2px 8px; border-radius: 999px;
        font-size: 0.78rem; font-weight: 600; letter-spacing: 0.02em;
    }
    .chip-green { background: #ccfbf1; color: #0f766e; }
    .chip-yellow { background: #fef3c7; color: #b45309; }
    .chip-red { background: #fee2e2; color: #b91c1c; }
    .chip-muted { background: #e2e8f0; color: #64748b; }
    .why-box {
        background: #ffffff; border: 1px solid #dbe4ea; border-left: 4px solid #0f766e;
        padding: 0.75rem 1rem; margin-bottom: 0.6rem; border-radius: 4px;
    }
    .why-box h4 { margin: 0 0 0.25rem 0; color: #0f172a; font-size: 0.95rem; }
    .why-box p { margin: 0; color: #334155; font-size: 0.88rem; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

SIGNAL_LABELS = {
    "margin_compression": "Margin compression",
    "activist": "Activist (13D)",
    "industry_consolidation": "Industry consolidation",
    "valuation_discount": "Valuation discount",
    "leverage_stress": "Leverage stress",
}


def _badge_class(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "chip-muted"
    if value >= 67:
        return "chip-green"
    if value >= 33:
        return "chip-yellow"
    return "chip-red"


def _score_color_hex(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "#e2e8f0"
    if value >= 67:
        return "#99f6e4"
    if value >= 33:
        return "#fde68a"
    return "#fecaca"


def format_mcap(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    v = float(value)
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def init_weight_state() -> None:
    for key in SIGNAL_KEYS:
        widget_key = f"w_{key}"
        if widget_key not in st.session_state:
            st.session_state[widget_key] = int(round(DEFAULT_WEIGHTS[key] * 100))


def live_weights() -> dict[str, float]:
    return normalize_weights({k: float(st.session_state.get(f"w_{k}", 0)) for k in SIGNAL_KEYS})


def load_master() -> pd.DataFrame:
    companies = db.load_companies_df()
    scores = db.load_scores_df()
    if companies.empty:
        return pd.DataFrame()
    if scores.empty:
        out = companies.copy()
        for k in SIGNAL_KEYS:
            out[k] = None
        out["composite_score"] = None
        out["details"] = [{} for _ in range(len(out))]
        return out
    keep = [
        "ticker",
        "computed_at",
        "margin_compression",
        "activist",
        "industry_consolidation",
        "valuation_discount",
        "leverage_stress",
        "composite_score",
        "details",
    ]
    keep = [c for c in keep if c in scores.columns]
    return companies.merge(scores[keep], on="ticker", how="left")


def apply_live_composite(df: pd.DataFrame, weights: dict[str, float]) -> pd.DataFrame:
    """Percentiles are cache-stable; only the weighted average moves with sliders."""
    out = df.copy()

    def _comp(row: pd.Series) -> Optional[float]:
        pts = {k: (None if pd.isna(row.get(k)) else float(row.get(k))) for k in SIGNAL_KEYS}
        return weighted_composite(pts, weights)

    if out.empty:
        return out
    out["composite_score"] = out.apply(_comp, axis=1)
    return out


def why_blocks(details: dict[str, Any], percentiles: dict[str, Any]) -> list[tuple[str, str]]:
    blocks = []
    m = details.get("margin_compression") or {}
    slope = m.get("slope")
    n_years = m.get("n_years") or 0
    if slope is None:
        m_txt = (
            "Not enough annual operating-margin points (need ≥ 2 years of "
            "OperatingIncomeLoss / Revenues) to fit a trend."
        )
    else:
        direction = "compressed" if slope < 0 else "expanded"
        m_txt = (
            f"Operating margin {direction} at {slope * 100:.2f} percentage points per year "
            f"over {n_years} fiscal year(s). More negative slopes rank higher."
        )
    blocks.append((SIGNAL_LABELS["margin_compression"], m_txt))

    a = details.get("activist") or {}
    if a.get("latest_13d"):
        a_txt = (
            f"Latest Schedule 13D/A on {a.get('latest_13d')} "
            f"({a.get('days_ago')} days ago) — raw band {a.get('raw')}. "
            "13G (passive) filings are ignored."
        )
    else:
        a_txt = "No Schedule 13D/A in the SEC recent-filings window used for this name."
    blocks.append((SIGNAL_LABELS["activist"], a_txt))

    i = details.get("industry_consolidation") or {}
    i_txt = (
        f"2-digit SIC {i.get('sic2') or '—'}: {i.get('peers_with_deals', 0)} of "
        f"{i.get('peer_count', 0)} other universe peers filed an 8-K Item 2.01 "
        f"(completed acquisition/disposition) in the trailing 24 months."
    )
    if i.get("deal_peer_tickers"):
        i_txt += f" Deal peers: {', '.join(i['deal_peer_tickers'])}."
    blocks.append((SIGNAL_LABELS["industry_consolidation"], i_txt))

    v = details.get("valuation_discount") or {}
    if v.get("raw") is None:
        v_txt = (
            v.get("method_note")
            or "Could not compare a current multiple to a 3-year average (Yahoo fields missing)."
        )
    else:
        cur, avg = v.get("current_multiple"), v.get("avg_3y_multiple")
        cur_s = f"{cur:.1f}" if isinstance(cur, (int, float)) else "—"
        avg_s = f"{avg:.1f}" if isinstance(avg, (int, float)) else "—"
        v_txt = (
            f"Current {v.get('multiple_type') or 'multiple'} {cur_s} vs "
            f"own ~3y average {avg_s} → "
            f"{float(v['raw']) * 100:.1f}% discount (negative = premium). "
            f"{v.get('method_note') or ''}"
        )
    blocks.append((SIGNAL_LABELS["valuation_discount"], v_txt))

    l = details.get("leverage_stress") or {}
    if l.get("raw") is None:
        l_txt = (
            "Need two fiscal years with positive operating-income (EBITDA proxy) and debt. "
            "EBITDA is approximated as OperatingIncomeLoss — D&A is not reliably tagged."
        )
    else:
        l_txt = (
            f"Debt / operating-income rose from {l.get('prior_ratio'):.2f} "
            f"({l.get('prior_year')}) to {l.get('latest_ratio'):.2f} ({l.get('latest_year')}). "
            "Rising ratio ranks higher. EBITDA proxy = operating income, not true EBITDA."
        )
    blocks.append((SIGNAL_LABELS["leverage_stress"], l_txt))
    return blocks


def margin_chart(fin: pd.DataFrame, ticker: str) -> go.Figure:
    sub = fin[fin["ticker"].str.upper() == ticker.upper()].sort_values("fiscal_year")
    fig = go.Figure()
    if sub.empty or sub["operating_margin"].dropna().empty:
        fig.update_layout(title="Operating margin — no usable series", height=320, template="plotly_white")
        return fig
    fig.add_trace(
        go.Scatter(
            x=sub["fiscal_year"],
            y=sub["operating_margin"] * 100,
            mode="lines+markers",
            line=dict(color="#0f766e", width=3),
            marker=dict(size=9, color="#14b8a6"),
            name="Operating margin %",
        )
    )
    fig.update_layout(
        title=f"{ticker} operating margin (10-K)",
        xaxis_title="Fiscal year",
        yaxis_title="Operating margin (%)",
        height=340,
        template="plotly_white",
        margin=dict(l=40, r=20, t=50, b=40),
        hovermode="x unified",
    )
    return fig


def valuation_chart(details: dict[str, Any], ticker: str) -> go.Figure:
    yearly = (details.get("valuation_discount") or {}).get("yearly") or []
    fig = go.Figure()
    years = [p.get("year") for p in yearly]
    closes = [p.get("close") for p in yearly]
    multiples = [p.get("multiple") for p in yearly]
    if years and any(c is not None for c in closes):
        fig.add_trace(
            go.Scatter(
                x=years,
                y=closes,
                mode="lines+markers",
                name="Year-end close",
                line=dict(color="#0f766e", width=3),
            )
        )
    if years and any(m is not None for m in multiples):
        fig.add_trace(
            go.Scatter(
                x=years,
                y=multiples,
                mode="lines+markers",
                name="Approx. multiple",
                yaxis="y2",
                line=dict(color="#0369a1", width=2, dash="dot"),
            )
        )
    fig.update_layout(
        title=f"{ticker} price / approximate multiple (Yahoo — see methodology)",
        height=340,
        template="plotly_white",
        margin=dict(l=40, r=20, t=50, b=40),
        yaxis=dict(title="Price"),
        yaxis2=dict(title="Approx. P/E", overlaying="y", side="right"),
        legend=dict(orientation="h", y=1.12),
    )
    if not years:
        fig.update_layout(title="Valuation trend — no yearly points cached")
    return fig


def render_header() -> None:
    st.title("M&A Target Screener")
    st.markdown(
        '<p class="disclaimer">'
        "Ranks a mid-cap universe on five public-filing and market <em>proxies</em> "
        "associated with takeout chatter so you can short-list names for diligence. "
        "This is a screening tool for educational / portfolio use — "
        "<strong>not investment advice, not a prediction that any company will be acquired</strong>."
        "</p>",
        unsafe_allow_html=True,
    )


def render_sidebar(df: pd.DataFrame) -> dict[str, Any]:
    init_weight_state()
    st.sidebar.header("Data")
    st.sidebar.caption(
        "First load is from SQLite cache. Refresh re-hits SEC EDGAR and Yahoo Finance "
        "and takes a couple of minutes (~150ms between SEC calls)."
    )
    if st.sidebar.button("Refresh Data", type="primary", use_container_width=True):
        progress = st.sidebar.progress(0, text="Starting refresh…")
        status = st.sidebar.empty()

        def cb(i: int, n: int, ticker: str, message: str) -> None:
            pct = 0 if n == 0 else min(i / n, 1.0)
            progress.progress(pct, text=message or ticker)
            status.write(message)

        try:
            from fetch_pipeline import run_pipeline

            summary = run_pipeline(progress=cb, weights=live_weights())
            progress.progress(1.0, text="Done")
            status.success(
                f"Scored {summary['scored']}/{summary['companies']} names. "
                f"Issues: {len(summary['errors'])}"
            )
            if summary["errors"]:
                with st.sidebar.expander("Refresh warnings"):
                    for err in summary["errors"]:
                        st.write(err)
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            status.error(str(exc))
            st.sidebar.exception(exc)

    last = db.last_refresh_timestamp()
    st.sidebar.caption(f"Last cache write: {last or 'never'}")

    st.sidebar.header("Filters")
    sectors = sorted([s for s in df["sector"].dropna().unique().tolist()]) if not df.empty and "sector" in df.columns else []
    sic_opts = []
    if not df.empty and "sic" in df.columns:
        sic_opts = sorted({scoring.sic2(s) for s in df["sic"].tolist() if scoring.sic2(s)})
    picked_sectors = st.sidebar.multiselect("Sector", sectors)
    picked_sic = st.sidebar.multiselect("SIC (2-digit)", sic_opts)

    if df.empty or df["market_cap"].dropna().empty:
        cap_min, cap_max = 0.0, 50e9
    else:
        cap_min = float(df["market_cap"].min())
        cap_max = float(df["market_cap"].max())
        if cap_min == cap_max:
            cap_max = cap_min + 1e9
    cap_lo, cap_hi = st.sidebar.slider(
        "Market cap ($)",
        min_value=float(cap_min),
        max_value=float(cap_max),
        value=(float(cap_min), float(cap_max)),
        format="$%.0f",
    )
    min_score = st.sidebar.slider("Minimum composite score", 0, 100, 0)

    st.sidebar.header("Signal weights")
    st.sidebar.caption("Sliders are auto-normalized to 100% for ranking. Percentile columns do not change.")
    for key in SIGNAL_KEYS:
        st.sidebar.slider(
            SIGNAL_LABELS[key],
            min_value=0,
            max_value=100,
            key=f"w_{key}",
        )
    effective = live_weights()
    st.sidebar.caption(
        "Normalized: "
        + " · ".join(f"{SIGNAL_LABELS[k].split()[0]} {effective[k]*100:.0f}%" for k in SIGNAL_KEYS)
    )

    return {
        "sectors": picked_sectors,
        "sic": picked_sic,
        "cap_lo": cap_lo,
        "cap_hi": cap_hi,
        "min_score": min_score,
        "weights": effective,
    }


def filter_frame(df: pd.DataFrame, filt: dict[str, Any]) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out
    if filt["sectors"]:
        out = out[out["sector"].isin(filt["sectors"])]
    if filt["sic"]:
        out = out[out["sic"].map(scoring.sic2).isin(filt["sic"])]
    if "market_cap" in out.columns:
        cap = out["market_cap"]
        mask = cap.isna() | ((cap >= filt["cap_lo"]) & (cap <= filt["cap_hi"]))
        out = out[mask]
    if "composite_score" in out.columns and filt["min_score"] > 0:
        out = out[out["composite_score"].fillna(-1) >= filt["min_score"]]
    return out.sort_values("composite_score", ascending=False, na_position="last")


def render_table(view: pd.DataFrame) -> Optional[str]:
    if view.empty:
        st.info("No names match these filters — or the cache is empty. Use **Refresh Data**.")
        return None

    display = pd.DataFrame(
        {
            "Ticker": view["ticker"],
            "Name": view["name"],
            "Sector": view["sector"] if "sector" in view.columns else "—",
            "Mkt cap": view["market_cap"].map(format_mcap) if "market_cap" in view.columns else "—",
            "Composite": view["composite_score"],
            "Margin": view["margin_compression"],
            "Activist": view["activist"],
            "Industry": view["industry_consolidation"],
            "Valuation": view["valuation_discount"],
            "Leverage": view["leverage_stress"],
        }
    )
    # Map display names back to style columns that exist on `display`
    rename_style = {
        "Composite": "Composite",
        "Margin": "Margin",
        "Activist": "Activist",
        "Industry": "Industry",
        "Valuation": "Valuation",
        "Leverage": "Leverage",
    }
    score_cols = list(rename_style.keys())

    def _style(df_disp: pd.DataFrame) -> pd.io.formats.style.Styler:
        styler = df_disp.style
        for c in score_cols:
            styler = styler.map(
                lambda v: f"background-color: {_score_color_hex(v)}; color: #0f172a; font-weight: 600",
                subset=[c],
            )
        styler = styler.format({c: (lambda v: "—" if pd.isna(v) else f"{v:.0f}") for c in score_cols})
        return styler

    st.caption(
        "Green ≥ 67th percentile of this universe · yellow 33–66 · red < 33. "
        "Higher = more 'target-like' on that proxy, not a better company."
    )
    st.dataframe(_style(display.reset_index(drop=True)), use_container_width=True, height=440)

    tickers = view["ticker"].tolist()
    selected = st.selectbox(
        "Open a name for the diligence snapshot",
        options=tickers,
        format_func=lambda t: f"{t} — {view.loc[view['ticker']==t, 'name'].iloc[0]}",
    )
    csv = view.copy()
    csv_cols = [
        "ticker",
        "name",
        "sector",
        "sic",
        "market_cap",
        "trailing_pe",
        "composite_score",
        *SIGNAL_KEYS,
    ]
    csv_cols = [c for c in csv_cols if c in csv.columns]
    st.download_button(
        "Export to CSV",
        data=csv[csv_cols].to_csv(index=False).encode("utf-8"),
        file_name=f"ma_screener_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        use_container_width=False,
    )
    return selected


def render_detail(view: pd.DataFrame, ticker: str) -> None:
    row = view[view["ticker"] == ticker].iloc[0]
    details = row["details"] if isinstance(row.get("details"), dict) else {}
    st.markdown("---")
    left, right = st.columns([1.2, 1])
    with left:
        st.subheader(f"{ticker}  ·  {row.get('name') or ''}")
        st.caption(
            f"CIK {row.get('cik') or '—'}  ·  SIC {row.get('sic') or '—'} "
            f"{row.get('sic_description') or ''}  ·  {row.get('sector') or 'Sector n/a'}"
        )
    with right:
        c1, c2, c3 = st.columns(3)
        c1.metric("Composite", "—" if pd.isna(row.get("composite_score")) else f"{row['composite_score']:.0f}")
        c2.metric("Price", "—" if pd.isna(row.get("price")) else f"${row['price']:.2f}")
        c3.metric("Market cap", format_mcap(row.get("market_cap")))

    badges = []
    for key in SIGNAL_KEYS:
        val = row.get(key)
        label = SIGNAL_LABELS[key]
        cls = _badge_class(val)
        txt = "—" if val is None or (isinstance(val, float) and pd.isna(val)) else f"{val:.0f}"
        badges.append(f'<span class="chip {cls}">{label} {txt}</span>')
    st.markdown(" ".join(badges), unsafe_allow_html=True)

    st.markdown("##### Why this score")
    percentiles = {k: row.get(k) for k in SIGNAL_KEYS}
    for title, body in why_blocks(details, percentiles):
        st.markdown(f'<div class="why-box"><h4>{title}</h4><p>{body}</p></div>', unsafe_allow_html=True)

    fin = db.load_financials_df()
    c_chart, v_chart = st.columns(2)
    with c_chart:
        st.plotly_chart(margin_chart(fin, ticker), use_container_width=True)
    with v_chart:
        st.plotly_chart(valuation_chart(details, ticker), use_container_width=True)

    st.markdown("##### Filing history (SEC submissions, recent window)")
    filings = db.load_filings_df(ticker)
    if filings.empty:
        st.caption("No filings cached for this ticker.")
        return

    def _row_style(r: pd.Series) -> list[str]:
        ft = str(r.get("form_type") or "").upper().replace(" ", "")
        if "13G" not in ft and ("SC13D" in ft or ft.startswith("SC13D")):
            return ["background-color: #ccfbf1"] * len(r)
        if str(r.get("form_type") or "").upper().startswith("8-K") and "2.01" in str(r.get("items") or ""):
            return ["background-color: #e0f2fe"] * len(r)
        return [""] * len(r)

    show = filings[["form_type", "filing_date", "accession_number", "items"]].copy()
    st.caption("Teal = Schedule 13D/A · blue = 8-K Item 2.01")
    st.dataframe(show.style.apply(_row_style, axis=1), use_container_width=True, height=320)

    pe = row.get("trailing_pe")
    ev = row.get("ev_ebitda")
    lo = row.get("week52_low")
    hi = row.get("week52_high")
    st.caption(
        f"Yahoo snapshot — trailing P/E: {pe if pd.notna(pe) else '—'}  ·  "
        f"EV/EBITDA: {ev if pd.notna(ev) else '—'}  ·  "
        f"52-week: {lo if pd.notna(lo) else '—'} – {hi if pd.notna(hi) else '—'}"
    )


def main() -> None:
    db.init_db()
    render_header()
    raw = load_master()
    filt = render_sidebar(raw if not raw.empty else pd.DataFrame(columns=["sector", "sic", "market_cap"]))
    scored = apply_live_composite(raw, filt["weights"]) if not raw.empty else raw
    view = filter_frame(scored, filt)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Universe in cache", "0" if raw.empty else str(len(raw)))
    k2.metric("Passing filters", str(len(view)))
    med = view["composite_score"].median() if not view.empty else None
    k3.metric("Median composite", "—" if med is None or pd.isna(med) else f"{med:.0f}")
    k4.metric("Weights", "normalized 100%")

    selected = render_table(view)
    if selected:
        render_detail(view, selected)


if __name__ == "__main__":
    main()
