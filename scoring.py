"""
M&A-target screening signals.

This is a *ranking* model, not a classifier. Each signal is converted to a
percentile (0–100) versus the current universe so a '70' means 'higher than
70% of names we screen today', not '70% probability of a takeout'.

Missing inputs yield None for that signal. The composite then renormalizes
the live UI weights across signals that actually exist — we do not impute 50
and pretend we measured something.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

import pandas as pd

DEFAULT_WEIGHTS = {
    "margin_compression": 0.25,
    "activist": 0.25,
    "industry_consolidation": 0.20,
    "valuation_discount": 0.15,
    "leverage_stress": 0.15,
}

SIGNAL_KEYS = tuple(DEFAULT_WEIGHTS.keys())


def _parse_date(value: Any) -> Optional[date]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def linear_slope(xs: list[float], ys: list[float]) -> Optional[float]:
    """Ordinary least squares slope. None if fewer than 2 points or no x-variance."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    n = float(len(xs))
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    var_x = sum((x - x_mean) ** 2 for x in xs)
    if var_x == 0:
        return None
    cov = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    return cov / var_x


def margin_compression_raw(financials: pd.DataFrame, ticker: str) -> dict[str, Any]:
    """
    Operating margin = OperatingIncomeLoss / Revenues, last 5 fiscal years.

    More *negative* slope (compression) → higher raw score (we store -slope).
    Why: sustained margin pressure is a classic reason a board explores a sale
    (scale buyer, sponsor take-private, or strategic tuck-in).
    """
    out: dict[str, Any] = {"raw": None, "slope": None, "n_years": 0, "series": []}
    if financials is None or financials.empty:
        return out
    sub = financials[financials["ticker"].str.upper() == ticker.upper()].copy()
    sub = sub.dropna(subset=["operating_margin", "fiscal_year"])
    sub = sub.sort_values("fiscal_year").tail(5)
    out["n_years"] = int(len(sub))
    out["series"] = [
        {"fiscal_year": int(r.fiscal_year), "operating_margin": float(r.operating_margin)}
        for r in sub.itertuples()
    ]
    if len(sub) < 2:
        return out
    xs = [float(y) for y in sub["fiscal_year"].tolist()]
    ys = [float(m) for m in sub["operating_margin"].tolist()]
    slope = linear_slope(xs, ys)
    out["slope"] = slope
    if slope is None:
        return out
    out["raw"] = float(-slope)
    return out


def activist_raw(filings: pd.DataFrame, ticker: str, as_of: Optional[date] = None) -> dict[str, Any]:
    """
    Schedule 13D in the trailing 12 months (NOT 13G).

    Scoring bands (before percentile):
      100 — most recent 13D/A in the last 6 months
       60 — most recent 13D/A between 6 and 12 months ago
        0 — none in the window

    Why 13D vs 13G: 13D is the 'I may influence control' form; 13G is passive.
    """
    as_of = as_of or date.today()
    out: dict[str, Any] = {"raw": 0.0, "latest_13d": None, "days_ago": None}
    if filings is None or filings.empty:
        return out

    sub = filings[filings["ticker"].str.upper() == ticker.upper()].copy()
    if sub.empty:
        return out

    def _is_13d(form: Any) -> bool:
        ft = str(form or "").upper().replace(" ", "")
        if "13G" in ft:
            return False
        return ft in {"SC13D", "SC13D/A"} or ft.startswith("SC13D")

    sub = sub[sub["form_type"].map(_is_13d)]
    dated = []
    for rec in sub.itertuples():
        d = _parse_date(getattr(rec, "filing_date", None))
        if d is not None:
            dated.append((d, rec.form_type, getattr(rec, "accession_number", "")))
    if not dated:
        return out
    dated.sort(reverse=True)
    latest, form, acc = dated[0]
    delta = (as_of - latest).days
    out["latest_13d"] = latest.isoformat()
    out["days_ago"] = delta
    out["form_type"] = form
    out["accession_number"] = acc
    if delta <= 182:
        out["raw"] = 100.0
    elif delta <= 365:
        out["raw"] = 60.0
    else:
        out["raw"] = 0.0
    return out


def sic2(sic: Any) -> Optional[str]:
    if sic is None or (isinstance(sic, float) and pd.isna(sic)):
        return None
    digits = "".join(ch for ch in str(sic) if ch.isdigit())
    if len(digits) < 2:
        return None
    return digits.zfill(4)[:2]


def _item_201(items: Any) -> bool:
    if items is None or (isinstance(items, float) and pd.isna(items)):
        return False
    parts = [p.strip() for p in str(items).replace(";", ",").split(",")]
    return any(p == "2.01" or p.startswith("2.01") for p in parts)


def industry_consolidation_raw(
    companies: pd.DataFrame,
    filings: pd.DataFrame,
    ticker: str,
    as_of: Optional[date] = None,
) -> dict[str, Any]:
    """
    Count *other* universe names with the same 2-digit SIC that filed an 8-K
    Item 2.01 (completed acquisition/disposition) in the trailing 24 months.

    Why 2-digit SIC: 4-digit is too sparse in a 50-name book; 2-digit is the
    usual 'industry group' cut for a first-pass consolidation screen.
    """
    as_of = as_of or date.today()
    cutoff = as_of - timedelta(days=730)
    out: dict[str, Any] = {
        "raw": 0.0,
        "sic2": None,
        "peer_count": 0,
        "peers_with_deals": 0,
        "peer_tickers": [],
    }
    if companies is None or companies.empty:
        return out

    me = companies[companies["ticker"].str.upper() == ticker.upper()]
    if me.empty:
        return out
    code = sic2(me.iloc[0]["sic"])
    out["sic2"] = code
    if not code:
        out["raw"] = None
        return out

    peers = companies[companies["sic"].map(sic2) == code]
    peers = peers[peers["ticker"].str.upper() != ticker.upper()]
    peer_tickers = [t.upper() for t in peers["ticker"].tolist()]
    out["peer_count"] = len(peer_tickers)
    out["peer_tickers"] = peer_tickers
    if not peer_tickers or filings is None or filings.empty:
        return out

    f = filings.copy()
    f["ticker"] = f["ticker"].str.upper()
    f = f[f["ticker"].isin(peer_tickers)]
    f = f[f["form_type"].astype(str).str.upper().str.startswith("8-K")]
    deal_names = set()
    for rec in f.itertuples():
        if not _item_201(getattr(rec, "items", "")):
            continue
        d = _parse_date(getattr(rec, "filing_date", None))
        if d is None or d < cutoff or d > as_of:
            continue
        deal_names.add(rec.ticker.upper())
    out["peers_with_deals"] = len(deal_names)
    out["deal_peer_tickers"] = sorted(deal_names)
    out["raw"] = float(len(deal_names))
    return out


def valuation_discount_raw(hist: dict[str, Any]) -> dict[str, Any]:
    """
    Bigger discount vs the name's own 3-year average multiple → higher raw score.

    raw = discount_pct = (avg - current) / abs(avg)
    A name trading at a premium scores negative before percentile ranking.
    """
    out = {
        "raw": None,
        "current_multiple": hist.get("current_multiple") if hist else None,
        "avg_3y_multiple": hist.get("avg_3y_multiple") if hist else None,
        "multiple_type": hist.get("multiple_type") if hist else None,
        "method_note": (hist or {}).get("method_note"),
        "yearly": (hist or {}).get("yearly") or [],
    }
    if not hist:
        return out
    disc = hist.get("discount_pct")
    if disc is None:
        return out
    try:
        out["raw"] = float(disc)
    except (TypeError, ValueError):
        out["raw"] = None
    return out


def leverage_stress_raw(financials: pd.DataFrame, ticker: str) -> dict[str, Any]:
    """
    Debt / EBITDA-proxy for the two most recent fiscal years with both inputs.

    EBITDA is *proxied by operating income* because DepreciationAndAmortization
    is unreliably tagged in companyfacts for a mixed mid-cap universe. That
    proxy overstates leverage when D&A is large (capital-intensive names).

    raw = latest_ratio - prior_ratio  (rising leverage → higher score)
    """
    out: dict[str, Any] = {
        "raw": None,
        "latest_year": None,
        "prior_year": None,
        "latest_ratio": None,
        "prior_ratio": None,
    }
    if financials is None or financials.empty:
        return out
    sub = financials[financials["ticker"].str.upper() == ticker.upper()].copy()
    sub = sub.dropna(subset=["total_debt", "ebitda_proxy", "fiscal_year"])
    # Negative or zero 'EBITDA' makes the ratio uninterpretable for screening.
    sub = sub[sub["ebitda_proxy"] > 0]
    sub = sub.sort_values("fiscal_year")
    if len(sub) < 2:
        return out
    prior = sub.iloc[-2]
    latest = sub.iloc[-1]
    prior_ratio = float(prior.total_debt) / float(prior.ebitda_proxy)
    latest_ratio = float(latest.total_debt) / float(latest.ebitda_proxy)
    out.update(
        {
            "raw": latest_ratio - prior_ratio,
            "latest_year": int(latest.fiscal_year),
            "prior_year": int(prior.fiscal_year),
            "latest_ratio": latest_ratio,
            "prior_ratio": prior_ratio,
        }
    )
    return out


def percentile_ranks(raw_by_ticker: dict[str, Optional[float]]) -> dict[str, Optional[float]]:
    """
    Percentile rank in [0, 100] among tickers with a non-null raw value.
    Ties use the average rank (pandas default method='average').
    """
    s = pd.Series(raw_by_ticker, dtype="float64")
    ranked = s.rank(pct=True, method="average") * 100.0
    out: dict[str, Optional[float]] = {}
    for k, v in ranked.items():
        out[str(k)] = None if pd.isna(v) else float(v)
    return out


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """Force non-negative weights to sum to 1.0 (UI sliders always 'sum to 100%')."""
    cleaned = {k: max(0.0, float(weights.get(k, 0.0))) for k in SIGNAL_KEYS}
    total = sum(cleaned.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in cleaned.items()}


def weighted_composite(
    percentiles: dict[str, Optional[float]],
    weights: dict[str, float],
) -> Optional[float]:
    """Renormalize weights over signals that are present so missing data is not a 0."""
    w = normalize_weights(weights)
    usable = {
        k: (percentiles.get(k), w[k])
        for k in SIGNAL_KEYS
        if percentiles.get(k) is not None and w[k] > 0
    }
    if not usable:
        return None
    w_sum = sum(weight for _p, weight in usable.values())
    if w_sum <= 0:
        return None
    return float(sum(p * weight for p, weight in usable.values()) / w_sum)


@dataclass
class ScoreBundle:
    ticker: str
    percentiles: dict[str, Optional[float]]
    composite: Optional[float]
    details: dict[str, Any] = field(default_factory=dict)


def score_universe(
    companies: pd.DataFrame,
    financials: pd.DataFrame,
    filings: pd.DataFrame,
    valuation_by_ticker: dict[str, dict[str, Any]],
    weights: Optional[dict[str, float]] = None,
    as_of: Optional[date] = None,
) -> list[ScoreBundle]:
    """Compute raw signals, convert to percentiles, then apply weights."""
    weights = normalize_weights(weights or DEFAULT_WEIGHTS)
    as_of = as_of or date.today()
    tickers = [str(t).upper() for t in companies["ticker"].tolist()] if not companies.empty else []

    raw_margin: dict[str, Optional[float]] = {}
    raw_act: dict[str, Optional[float]] = {}
    raw_ind: dict[str, Optional[float]] = {}
    raw_val: dict[str, Optional[float]] = {}
    raw_lev: dict[str, Optional[float]] = {}
    details: dict[str, dict[str, Any]] = {}

    for tkr in tickers:
        m = margin_compression_raw(financials, tkr)
        a = activist_raw(filings, tkr, as_of=as_of)
        i = industry_consolidation_raw(companies, filings, tkr, as_of=as_of)
        v = valuation_discount_raw(valuation_by_ticker.get(tkr) or valuation_by_ticker.get(tkr.lower()) or {})
        l = leverage_stress_raw(financials, tkr)
        raw_margin[tkr] = m.get("raw")
        raw_act[tkr] = a.get("raw")
        raw_ind[tkr] = i.get("raw")
        raw_val[tkr] = v.get("raw")
        raw_lev[tkr] = l.get("raw")
        details[tkr] = {
            "margin_compression": m,
            "activist": a,
            "industry_consolidation": i,
            "valuation_discount": v,
            "leverage_stress": l,
        }

    p_margin = percentile_ranks(raw_margin)
    p_act = percentile_ranks(raw_act)
    p_ind = percentile_ranks(raw_ind)
    p_val = percentile_ranks(raw_val)
    p_lev = percentile_ranks(raw_lev)

    bundles: list[ScoreBundle] = []
    for tkr in tickers:
        percentiles = {
            "margin_compression": p_margin.get(tkr),
            "activist": p_act.get(tkr),
            "industry_consolidation": p_ind.get(tkr),
            "valuation_discount": p_val.get(tkr),
            "leverage_stress": p_lev.get(tkr),
        }
        d = details[tkr]
        for key, p in percentiles.items():
            d[key]["percentile"] = p
        bundles.append(
            ScoreBundle(
                ticker=tkr,
                percentiles=percentiles,
                composite=weighted_composite(percentiles, weights),
                details=d,
            )
        )
    return bundles


def bundles_to_score_rows(
    bundles: list[ScoreBundle],
    weights: Optional[dict[str, float]] = None,
) -> list[dict[str, Any]]:
    w = normalize_weights(weights or DEFAULT_WEIGHTS)
    rows = []
    for b in bundles:
        rows.append(
            {
                "ticker": b.ticker,
                "margin_compression": b.percentiles.get("margin_compression"),
                "activist": b.percentiles.get("activist"),
                "industry_consolidation": b.percentiles.get("industry_consolidation"),
                "valuation_discount": b.percentiles.get("valuation_discount"),
                "leverage_stress": b.percentiles.get("leverage_stress"),
                "composite_score": b.composite,
                "details": {**b.details, "weights_used": w},
            }
        )
    return rows
