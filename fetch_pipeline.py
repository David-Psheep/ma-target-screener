"""
Orchestrate universe fetch → SQLite → scores.

Design rules:
  * One company failing (404 facts, Yahoo timeout, missing SIC) must never
    abort the run. We log and continue.
  * Cache-first: Streamlit startup reads SQLite. This module is only invoked
    by Refresh Data or `python fetch_pipeline.py`.
  * Progress is a callback (i, n, ticker, message) so the UI can drive a bar
    without importing Streamlit here.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

import database as db
import market_data
import scoring
import sec_client

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
UNIVERSE_PATH = PROJECT_ROOT / "data" / "universe.csv"

ProgressCb = Callable[[int, int, str, str], None]


def load_universe(path: Path = UNIVERSE_PATH) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "ticker" not in df.columns:
        raise ValueError("universe.csv must have a ticker column")
    df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
    if "company_name" not in df.columns:
        df["company_name"] = df["ticker"]
    return df.drop_duplicates(subset=["ticker"]).reset_index(drop=True)


def _cik_from_efts_hit(hit: dict[str, Any]) -> Optional[str]:
    """Defensive parse of an undocumented EFTS hit. Never assume keys exist."""
    if not isinstance(hit, dict):
        return None
    src = hit.get("_source")
    if not isinstance(src, dict):
        src = hit
    ciks = src.get("ciks") or src.get("cik") or src.get("entity_id")
    if isinstance(ciks, list) and ciks:
        try:
            return sec_client.pad_cik(ciks[0])
        except (TypeError, ValueError):
            return None
    if isinstance(ciks, (str, int)):
        try:
            return sec_client.pad_cik(ciks)
        except (TypeError, ValueError):
            return None
    names = src.get("display_names")
    if isinstance(names, list) and names:
        m = re.search(r"CIK\s*0*(\d+)", str(names[0]), re.I)
        if m:
            return sec_client.pad_cik(m.group(1))
    return None


def _supplement_item_201_from_efts(
    companies_cik: dict[str, str],
    filings_by_ticker: dict[str, list[dict[str, Any]]],
) -> None:
    """
    Cross-check 8-K Item 2.01 via full-text search.

    Submissions JSON sometimes has an empty `items` array. EFTS is unofficial,
    so a failure here is logged and ignored — industry scores then rely on
    whatever Item 2.01 strings we already stored from submissions.
    """
    end = date.today()
    start = end - timedelta(days=730)
    try:
        hits = sec_client.fetch_full_text_search(
            query='"2.01"',
            forms="8-K",
            start=start,
            end=end,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("EFTS 8-K Item 2.01 supplement skipped: %s", exc)
        return

    cik_to_ticker = {cik: tkr for tkr, cik in companies_cik.items()}
    matched = 0
    for hit in hits:
        cik = _cik_from_efts_hit(hit)
        if not cik or cik not in cik_to_ticker:
            continue
        ticker = cik_to_ticker[cik]
        src = hit.get("_source") if isinstance(hit.get("_source"), dict) else hit
        file_date = str(src.get("file_date") or src.get("period_of_report") or "")[:10]
        acc = str(src.get("adsh") or src.get("accession_number") or f"efts-{cik}-{file_date}")
        existing = filings_by_ticker.setdefault(ticker, [])
        if any(r.get("accession_number") == acc for r in existing):
            # Ensure items includes 2.01 even if submissions omitted it.
            for r in existing:
                if r.get("accession_number") == acc and "2.01" not in str(r.get("items") or ""):
                    r["items"] = (str(r.get("items") or "").strip(",") + ",2.01").strip(",")
            continue
        existing.append(
            {
                "form_type": "8-K",
                "filing_date": file_date or start.isoformat(),
                "accession_number": acc,
                "items": "2.01",
            }
        )
        matched += 1
    logger.info("EFTS supplement attached Item 2.01 to %s universe filings", matched)


def fetch_one_company(
    ticker: str,
    universe_name: str,
    ticker_map: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Pull SEC + Yahoo for a single ticker. Returns None on hard mapping failure."""
    rec = ticker_map.get(ticker.upper())
    if not rec:
        logger.warning("Skipping %s — not found in SEC company_tickers.json", ticker)
        return None

    cik = rec["cik"]
    result: dict[str, Any] = {
        "ticker": ticker.upper(),
        "cik": cik,
        "name": universe_name or rec.get("title") or ticker,
        "sic": None,
        "sic_description": None,
        "filings": [],
        "financials": [],
        "quote": {},
        "valuation": {},
    }

    try:
        submissions = sec_client.fetch_submissions(cik)
        meta = sec_client.extract_issuer_meta(submissions)
        result["name"] = meta.get("name") or result["name"]
        result["sic"] = meta.get("sic")
        result["sic_description"] = meta.get("sic_description")
        result["filings"] = sec_client.parse_recent_filings(submissions)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Submissions failed for %s (%s): %s", ticker, cik, exc)

    try:
        facts = sec_client.fetch_companyfacts(cik)
        result["financials"] = sec_client.extract_financial_rows(facts)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Companyfacts failed for %s (%s): %s", ticker, cik, exc)

    try:
        result["quote"] = market_data.get_quote(ticker)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Yahoo quote failed for %s: %s", ticker, exc)
        result["quote"] = {}

    try:
        result["valuation"] = market_data.get_historical_multiples(ticker)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Yahoo multiples failed for %s: %s", ticker, exc)
        result["valuation"] = {}

    return result


def persist_company(row: dict[str, Any]) -> None:
    quote = row.get("quote") or {}
    db.upsert_company(
        {
            "ticker": row["ticker"],
            "cik": row.get("cik"),
            "name": row.get("name"),
            "sic": row.get("sic"),
            "sic_description": row.get("sic_description"),
            "sector": quote.get("sector"),
            "industry": quote.get("industry"),
            "price": quote.get("price"),
            "market_cap": quote.get("market_cap"),
            "trailing_pe": quote.get("trailing_pe"),
            "ev_ebitda": quote.get("ev_ebitda"),
            "week52_low": quote.get("week52_low"),
            "week52_high": quote.get("week52_high"),
        }
    )
    db.replace_financials(row["ticker"], row.get("financials") or [])
    db.replace_filings(row["ticker"], row.get("filings") or [])


def recompute_scores(
    weights: Optional[dict[str, float]] = None,
    valuation_by_ticker: Optional[dict[str, dict[str, Any]]] = None,
) -> list[scoring.ScoreBundle]:
    companies = db.load_companies_df()
    financials = db.load_financials_df()
    filings = db.load_filings_df()
    val_map = valuation_by_ticker or {}
    # If we did not just fetch, rebuild a thin valuation dict from cached quotes
    # so the UI can re-weight without hitting Yahoo. Discount vs 3y avg is only
    # as good as the last refresh (details_json still has the last narrative).
    if not val_map and not companies.empty:
        for rec in companies.itertuples():
            val_map[str(rec.ticker).upper()] = {
                "current_multiple": getattr(rec, "trailing_pe", None) or getattr(rec, "ev_ebitda", None),
                "multiple_type": "trailing_pe" if getattr(rec, "trailing_pe", None) else "ev_ebitda",
                "avg_3y_multiple": None,
                "discount_pct": None,
                "method_note": "Re-score without a live Yahoo pull; valuation percentile uses last Refresh Data.",
            }
        # Prefer details from last score run if present.
        prev = db.load_scores_df()
        if not prev.empty and "details" in prev.columns:
            for rec in prev.itertuples():
                det = rec.details if isinstance(rec.details, dict) else {}
                vd = det.get("valuation_discount") or {}
                if vd.get("raw") is not None:
                    val_map[str(rec.ticker).upper()] = {
                        "discount_pct": vd.get("raw"),
                        "current_multiple": vd.get("current_multiple"),
                        "avg_3y_multiple": vd.get("avg_3y_multiple"),
                        "multiple_type": vd.get("multiple_type"),
                        "yearly": vd.get("yearly") or [],
                        "method_note": vd.get("method_note"),
                    }

    bundles = scoring.score_universe(
        companies, financials, filings, val_map, weights=weights
    )
    db.replace_scores(scoring.bundles_to_score_rows(bundles, weights=weights))
    return bundles


def run_pipeline(
    force_ticker_map_refresh: bool = False,
    progress: Optional[ProgressCb] = None,
    weights: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """
    Full refresh. Returns a small summary dict for the UI.

    Wall-clock: ~50 issuers × (submissions + companyfacts + 0.15s throttle)
    plus Yahoo, typically a couple of minutes.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db.init_db()
    universe = load_universe()
    n = len(universe)
    if progress:
        progress(0, n, "", "Downloading SEC ticker map")
    ticker_map = sec_client.fetch_ticker_map(force_refresh=force_ticker_map_refresh)

    errors: list[str] = []
    valuation_by_ticker: dict[str, dict[str, Any]] = {}
    filings_by_ticker: dict[str, list[dict[str, Any]]] = {}
    companies_cik: dict[str, str] = {}

    for i, rec in enumerate(universe.itertuples(), start=1):
        ticker = rec.ticker
        name = rec.company_name
        if progress:
            progress(i - 1, n, ticker, f"Fetching {ticker} ({i}/{n})")
        try:
            row = fetch_one_company(ticker, str(name), ticker_map)
            if row is None:
                errors.append(f"{ticker}: no CIK mapping")
                continue
            persist_company(row)
            valuation_by_ticker[ticker] = row.get("valuation") or {}
            filings_by_ticker[ticker] = row.get("filings") or []
            if row.get("cik"):
                companies_cik[ticker] = row["cik"]
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unhandled error for %s", ticker)
            errors.append(f"{ticker}: {exc}")

    if progress:
        progress(n, n, "", "SEC full-text search (Item 2.01)")
    try:
        _supplement_item_201_from_efts(companies_cik, filings_by_ticker)
        for tkr, flist in filings_by_ticker.items():
            db.replace_filings(tkr, flist)
    except Exception as exc:  # noqa: BLE001
        logger.warning("EFTS supplement failed: %s", exc)

    if progress:
        progress(n, n, "", "Computing percentile scores")
    bundles = recompute_scores(weights=weights, valuation_by_ticker=valuation_by_ticker)
    return {
        "companies": n,
        "scored": sum(1 for b in bundles if b.composite is not None),
        "errors": errors,
    }


if __name__ == "__main__":
    summary = run_pipeline()
    print(summary)
