"""Unit tests for scoring — sample frames only, no network."""

from datetime import date

import pandas as pd

from scoring import (
    activist_raw,
    industry_consolidation_raw,
    leverage_stress_raw,
    linear_slope,
    margin_compression_raw,
    normalize_weights,
    percentile_ranks,
    score_universe,
    valuation_discount_raw,
    weighted_composite,
)


def test_linear_slope_negative():
    # Margins 20%, 18%, 16% over 2022–2024 → slope -0.02 per year.
    slope = linear_slope([2022.0, 2023.0, 2024.0], [0.20, 0.18, 0.16])
    assert slope is not None
    assert abs(slope - (-0.02)) < 1e-9


def test_margin_compression_inverts_slope():
    fin = pd.DataFrame(
        {
            "ticker": ["AAA", "AAA", "AAA"],
            "fiscal_year": [2022, 2023, 2024],
            "operating_margin": [0.20, 0.18, 0.16],
        }
    )
    out = margin_compression_raw(fin, "AAA")
    assert out["raw"] is not None
    assert out["raw"] > 0  # compression is a *positive* target-screen signal


def test_activist_windows():
    filings = pd.DataFrame(
        {
            "ticker": ["AAA", "BBB", "CCC"],
            "form_type": ["SC 13D", "SC 13D", "SC 13G"],
            "filing_date": ["2024-11-01", "2024-03-01", "2024-12-01"],
            "accession_number": ["a", "b", "c"],
            "items": ["", "", ""],
        }
    )
    as_of = date(2025, 1, 15)
    assert activist_raw(filings, "AAA", as_of=as_of)["raw"] == 100.0
    assert activist_raw(filings, "BBB", as_of=as_of)["raw"] == 60.0
    # 13G must not count
    assert activist_raw(filings, "CCC", as_of=as_of)["raw"] == 0.0


def test_industry_counts_other_issuers_only():
    companies = pd.DataFrame(
        {
            "ticker": ["AAA", "BBB", "CCC"],
            "sic": ["3711", "3714", "7372"],
        }
    )
    filings = pd.DataFrame(
        {
            "ticker": ["BBB", "AAA"],
            "form_type": ["8-K", "8-K"],
            "filing_date": ["2024-06-01", "2024-06-01"],
            "accession_number": ["x", "y"],
            "items": ["2.01,9.01", "2.01"],
        }
    )
    as_of = date(2025, 1, 1)
    aaa = industry_consolidation_raw(companies, filings, "AAA", as_of=as_of)
    assert aaa["sic2"] == "37"
    assert aaa["peers_with_deals"] == 1
    # Self 2.01 must not inflate AAA's own industry score.
    bbb = industry_consolidation_raw(companies, filings, "BBB", as_of=as_of)
    assert bbb["peers_with_deals"] == 1


def test_leverage_rising():
    fin = pd.DataFrame(
        {
            "ticker": ["AAA", "AAA"],
            "fiscal_year": [2023, 2024],
            "total_debt": [100.0, 180.0],
            "ebitda_proxy": [50.0, 50.0],
        }
    )
    out = leverage_stress_raw(fin, "AAA")
    assert out["prior_ratio"] == 2.0
    assert out["latest_ratio"] == 3.6
    assert abs(out["raw"] - 1.6) < 1e-9


def test_percentile_and_composite():
    ranks = percentile_ranks({"A": 1.0, "B": 2.0, "C": 3.0})
    assert ranks["A"] < ranks["B"] < ranks["C"]
    comp = weighted_composite(
        {
            "margin_compression": 100.0,
            "activist": 0.0,
            "industry_consolidation": None,
            "valuation_discount": None,
            "leverage_stress": None,
        },
        {"margin_compression": 25, "activist": 25, "industry_consolidation": 20, "valuation_discount": 15, "leverage_stress": 15},
    )
    # Only two live signals, equal weights after renormalize → 50
    assert comp is not None
    assert abs(comp - 50.0) < 1e-6


def test_normalize_weights_sum_to_one():
    w = normalize_weights(
        {
            "margin_compression": 25,
            "activist": 25,
            "industry_consolidation": 20,
            "valuation_discount": 15,
            "leverage_stress": 15,
        }
    )
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_valuation_discount_sign():
    cheap = valuation_discount_raw(
        {"discount_pct": 0.30, "current_multiple": 10, "avg_3y_multiple": 14, "multiple_type": "trailing_pe"}
    )
    rich = valuation_discount_raw(
        {"discount_pct": -0.20, "current_multiple": 18, "avg_3y_multiple": 15, "multiple_type": "trailing_pe"}
    )
    assert cheap["raw"] > 0
    assert rich["raw"] < 0


def test_score_universe_end_to_end():
    companies = pd.DataFrame(
        {"ticker": ["AAA", "BBB"], "sic": ["3711", "3714"], "name": ["A", "B"]}
    )
    financials = pd.DataFrame(
        {
            "ticker": ["AAA", "AAA", "BBB", "BBB"],
            "fiscal_year": [2023, 2024, 2023, 2024],
            "operating_margin": [0.2, 0.1, 0.1, 0.12],
            "total_debt": [10.0, 20.0, 10.0, 10.0],
            "ebitda_proxy": [5.0, 5.0, 5.0, 5.0],
        }
    )
    filings = pd.DataFrame(
        {
            "ticker": ["AAA"],
            "form_type": ["SC 13D"],
            "filing_date": ["2024-12-01"],
            "accession_number": ["z"],
            "items": [""],
        }
    )
    val = {
        "AAA": {"discount_pct": 0.2, "current_multiple": 8, "avg_3y_multiple": 10, "multiple_type": "trailing_pe"},
        "BBB": {"discount_pct": -0.1, "current_multiple": 12, "avg_3y_multiple": 11, "multiple_type": "trailing_pe"},
    }
    bundles = score_universe(companies, financials, filings, val, as_of=date(2025, 1, 15))
    by_t = {b.ticker: b for b in bundles}
    assert by_t["AAA"].composite is not None
    assert by_t["BBB"].composite is not None
    # AAA compresses, has a 13D, rising leverage, and a discount — should rank higher.
    assert by_t["AAA"].composite > by_t["BBB"].composite
