"""
SEC EDGAR HTTP client.

Why a dedicated module:
  The SEC's public JSON endpoints are undocumented, rate-limited, and will
  reject clients that omit a proper User-Agent. Isolating retries, throttling,
  and response-shape checks here keeps the pipeline from crashing when a
  single ticker's JSON is malformed.

Required header:
  User-Agent: "{Name} {email}"  — loaded from SEC_USER_AGENT in .env
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
import os

load_dotenv()

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
TICKER_MAP_PATH = CACHE_DIR / "company_tickers.json"

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

# Stay well under the unofficial ~10 req/s ceiling.
INTER_REQUEST_SLEEP_SEC = 0.15
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT_SEC = 30

# XBRL tag names vary by filer. Order is preference: first hit wins.
REVENUE_TAGS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
)
OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
GROSS_PROFIT_TAGS = ("GrossProfit",)
LT_DEBT_NONCURRENT_TAGS = ("LongTermDebtNoncurrent", "LongTermDebt")
LT_DEBT_CURRENT_TAGS = ("LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent")
NET_INCOME_TAGS = ("NetIncomeLoss",)


class SecConfigError(RuntimeError):
    """Raised when SEC_USER_AGENT is missing — better to fail loud than get banned."""


def _user_agent() -> str:
    ua = (os.getenv("SEC_USER_AGENT") or "").strip()
    if not ua:
        raise SecConfigError(
            "SEC_USER_AGENT is not set. Copy .env.example to .env and put "
            "your name and email in the format 'Jane Doe jane@university.edu'."
        )
    return ua


def _request_with_retry(
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    extra_headers: Optional[dict[str, str]] = None,
) -> requests.Response:
    """
    GET with 3 attempts and exponential backoff (1s, 2s, 4s).

    Why: EDGAR returns 403 (bad UA), 429 (rate limit), and occasional 5xx.
    Retrying a few times is cheaper than aborting a 50-ticker refresh.
    """
    headers = {
        "User-Agent": _user_agent(),
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    last_exc: Optional[BaseException] = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = requests.get(
                url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SEC
            )
            # Throttle *after* the call so a burst of 50 tickers cannot exceed 10 rps.
            time.sleep(INTER_REQUEST_SLEEP_SEC)
            if resp.status_code in (403, 429, 500, 502, 503, 504):
                raise requests.HTTPError(
                    f"SEC HTTP {resp.status_code} for {url}", response=resp
                )
            resp.raise_for_status()
            return resp
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            backoff = 2 ** attempt  # 1, 2, 4 seconds
            logger.warning(
                "SEC request failed (attempt %s/%s) %s: %s — retrying in %ss",
                attempt + 1,
                MAX_ATTEMPTS,
                url,
                exc,
                backoff,
            )
            time.sleep(backoff)
    raise RuntimeError(f"SEC request failed after {MAX_ATTEMPTS} attempts: {url}") from last_exc


def pad_cik(cik: int | str) -> str:
    """Submissions/companyfacts URLs require a 10-digit zero-padded CIK."""
    return str(int(str(cik).strip())).zfill(10)


def fetch_ticker_map(force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """
    Download company_tickers.json once and cache it on disk.

    Returns a dict keyed by UPPERCASE ticker:
      {"WSO": {"cik": "0000104169", "title": "WATSCO INC"}, ...}

    Why cache: this file is ~10k issuers and rarely needs a live pull on every
    Streamlit rerun. Refresh Data can pass force_refresh=True.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if TICKER_MAP_PATH.exists() and not force_refresh:
        raw = json.loads(TICKER_MAP_PATH.read_text(encoding="utf-8"))
        logger.info("Loaded cached ticker map from %s", TICKER_MAP_PATH)
    else:
        resp = _request_with_retry(SEC_TICKERS_URL)
        raw = resp.json()
        TICKER_MAP_PATH.write_text(json.dumps(raw), encoding="utf-8")
        logger.info("Downloaded and cached ticker map (%s entries)", len(raw))

    by_ticker: dict[str, dict[str, Any]] = {}
    # The file is a dict of {"0": {cik_str, ticker, title}, "1": ...}
    if not isinstance(raw, dict):
        logger.error("company_tickers.json was not a dict: %s", type(raw))
        return by_ticker

    for _idx, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        ticker = str(rec.get("ticker") or "").upper().strip()
        cik = rec.get("cik_str")
        title = rec.get("title") or ""
        if not ticker or cik is None:
            continue
        by_ticker[ticker] = {"cik": pad_cik(cik), "title": title, "cik_str": cik}
    return by_ticker


def fetch_submissions(cik: int | str) -> dict[str, Any]:
    url = SEC_SUBMISSIONS_URL.format(cik=pad_cik(cik))
    resp = _request_with_retry(url)
    data = resp.json()
    if not isinstance(data, dict):
        logger.error("submissions payload was not a dict for CIK %s: %s", cik, type(data))
        return {}
    return data


def fetch_companyfacts(cik: int | str) -> dict[str, Any]:
    url = SEC_COMPANYFACTS_URL.format(cik=pad_cik(cik))
    resp = _request_with_retry(url)
    data = resp.json()
    if not isinstance(data, dict):
        logger.error("companyfacts payload was not a dict for CIK %s: %s", cik, type(data))
        return {}
    return data


def fetch_full_text_search(
    query: str,
    forms: str,
    start: date,
    end: date,
    *,
    max_pages: int = 5,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """
    Undocumented EFTS full-text search. Shape is not guaranteed — every key is
    checked before use, and unexpected payloads are logged (truncated).
    """
    hits_out: list[dict[str, Any]] = []
    for page in range(max_pages):
        params = {
            "q": query,
            "forms": forms,
            "dateRange": "custom",
            "startdt": start.isoformat(),
            "enddt": end.isoformat(),
            "from": page * page_size,
        }
        try:
            resp = _request_with_retry(SEC_EFTS_URL, params=params)
        except Exception as exc:
            logger.warning("EFTS search failed: %s", exc)
            break

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("EFTS returned non-JSON. Body head: %s", resp.text[:500])
            break

        if not isinstance(payload, dict) or "hits" not in payload:
            logger.warning(
                "EFTS unexpected top-level shape. Keys=%s body_head=%s",
                list(payload.keys()) if isinstance(payload, dict) else type(payload),
                str(payload)[:500],
            )
            break

        hits_block = payload.get("hits")
        if not isinstance(hits_block, dict):
            logger.warning("EFTS hits was not a dict: %s", type(hits_block))
            break

        page_hits = hits_block.get("hits")
        if not isinstance(page_hits, list):
            logger.warning("EFTS hits.hits was not a list: %s", type(page_hits))
            break

        if not page_hits:
            break

        hits_out.extend(page_hits)
        total = hits_block.get("total")
        total_n = None
        if isinstance(total, dict):
            total_n = total.get("value")
        elif isinstance(total, int):
            total_n = total
        if total_n is not None and len(hits_out) >= int(total_n):
            break
        if len(page_hits) < page_size:
            break

    return hits_out


def parse_recent_filings(submissions: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Flatten the parallel arrays in submissions['filings']['recent'] into rows.

    Why: EDGAR stores form, filingDate, accessionNumber, items as sibling
    arrays of equal length rather than a list of objects.
    """
    filings_obj = submissions.get("filings") if isinstance(submissions, dict) else None
    if not isinstance(filings_obj, dict):
        return []
    recent = filings_obj.get("recent")
    if not isinstance(recent, dict):
        return []

    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    items = recent.get("items") or []

    n = min(len(forms), len(dates), len(accessions))
    rows = []
    for i in range(n):
        item_str = items[i] if i < len(items) else ""
        rows.append(
            {
                "form_type": str(forms[i]),
                "filing_date": str(dates[i]),
                "accession_number": str(accessions[i]),
                "items": str(item_str) if item_str is not None else "",
            }
        )
    return rows


def extract_issuer_meta(submissions: dict[str, Any]) -> dict[str, Any]:
    sic = submissions.get("sic")
    sic_str = str(sic).strip() if sic is not None else ""
    # Preserve 4-digit SIC (leading zeros matter for a few industries).
    if sic_str.isdigit():
        sic_str = sic_str.zfill(4)
    return {
        "name": submissions.get("name") or "",
        "sic": sic_str,
        "sic_description": submissions.get("sicDescription") or "",
        "tickers": submissions.get("tickers") or [],
        "exchanges": submissions.get("exchanges") or [],
    }


def _usd_facts_for_tags(facts: dict[str, Any], tags: tuple[str, ...]) -> list[dict[str, Any]]:
    """Return USD facts for the first tag that exists under us-gaap."""
    if not isinstance(facts, dict):
        return []
    us_gaap = (facts.get("facts") or {}).get("us-gaap") if isinstance(facts.get("facts"), dict) else None
    if not isinstance(us_gaap, dict):
        return []

    chosen = None
    for tag in tags:
        if tag in us_gaap and isinstance(us_gaap[tag], dict):
            chosen = us_gaap[tag]
            break
    if chosen is None:
        return []

    units = chosen.get("units")
    if not isinstance(units, dict):
        return []
    # Prefer USD; some filers only publish USD/shares which we skip.
    series = units.get("USD")
    if not isinstance(series, list):
        return []
    return [pt for pt in series if isinstance(pt, dict)]


def annual_values_by_fy(facts: dict[str, Any], tags: tuple[str, ...]) -> dict[int, float]:
    """
    Latest 10-K (or 10-K/A) USD value per fiscal year for the first available tag.

    Why 'latest filed wins': restatements arrive as amendments; using the most
    recently filed point for that FY is closer to what an analyst would use.
    """
    points = _usd_facts_for_tags(facts, tags)
    by_fy: dict[int, tuple[str, float]] = {}
    for pt in points:
        form = str(pt.get("form") or "")
        if form not in ("10-K", "10-K/A"):
            continue
        fy = pt.get("fy")
        val = pt.get("val")
        filed = str(pt.get("filed") or "")
        if fy is None or val is None:
            continue
        try:
            fy_i = int(fy)
            val_f = float(val)
        except (TypeError, ValueError):
            continue
        prev = by_fy.get(fy_i)
        if prev is None or filed >= prev[0]:
            by_fy[fy_i] = (filed, val_f)
    return {fy: val for fy, (_filed, val) in by_fy.items()}


def extract_financial_rows(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Align annual us-gaap series onto fiscal_year rows. Missing tags stay None."""
    revenue = annual_values_by_fy(facts, REVENUE_TAGS)
    opinc = annual_values_by_fy(facts, OPERATING_INCOME_TAGS)
    gp = annual_values_by_fy(facts, GROSS_PROFIT_TAGS)
    debt_nc = annual_values_by_fy(facts, LT_DEBT_NONCURRENT_TAGS)
    debt_c = annual_values_by_fy(facts, LT_DEBT_CURRENT_TAGS)
    ni = annual_values_by_fy(facts, NET_INCOME_TAGS)

    years = sorted(
        set(revenue) | set(opinc) | set(gp) | set(debt_nc) | set(debt_c) | set(ni)
    )
    rows = []
    for fy in years:
        rev = revenue.get(fy)
        oi = opinc.get(fy)
        ltd_nc = debt_nc.get(fy)
        ltd_c = debt_c.get(fy)
        total_debt = None
        if ltd_nc is not None or ltd_c is not None:
            total_debt = (ltd_nc or 0.0) + (ltd_c or 0.0)
        # EBITDA proxy: D&A tags are inconsistently populated; operating income
        # is the spec-mandated stand-in. This OVERSTATES leverage vs true EBITDA.
        ebitda_proxy = oi
        margin = None
        if rev and oi is not None and rev != 0:
            margin = oi / rev
        rows.append(
            {
                "fiscal_year": fy,
                "revenue": rev,
                "operating_income": oi,
                "gross_profit": gp.get(fy),
                "net_income": ni.get(fy),
                "lt_debt_noncurrent": ltd_nc,
                "lt_debt_current": ltd_c,
                "total_debt": total_debt,
                "ebitda_proxy": ebitda_proxy,
                "operating_margin": margin,
            }
        )
    return rows


def eight_k_has_item_201(items: str) -> bool:
    """
    8-K Item 2.01 = Completion of Acquisition or Disposition of Assets.
    EDGAR stores items as comma-separated strings like '2.01,9.01'.
    """
    if not items:
        return False
    parts = [p.strip() for p in str(items).replace(";", ",").split(",")]
    return any(p == "2.01" or p.startswith("2.01") for p in parts)


def is_schedule_13d(form_type: str) -> bool:
    """
    13D = active beneficial-ownership intent. 13G is the passive analogue and
    must NOT count. Amendments (SC 13D/A) are treated as a live 13D signal.
    """
    ft = (form_type or "").upper().replace(" ", "")
    if "13G" in ft:
        return False
    return ft in {"SC13D", "SC13D/A"} or ft.startswith("SC13D")


if __name__ == "__main__":
    # Smoke-test a handful of mid-caps before wiring the full universe.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sample = ["WSO", "HOLX", "MANH"]
    ticker_map = fetch_ticker_map()
    for tkr in sample:
        rec = ticker_map.get(tkr)
        print(f"\n=== {tkr} map={rec} ===")
        if not rec:
            print("NOT IN TICKER MAP")
            continue
        sub = fetch_submissions(rec["cik"])
        meta = extract_issuer_meta(sub)
        filings = parse_recent_filings(sub)
        facts = fetch_companyfacts(rec["cik"])
        fin = extract_financial_rows(facts)
        print("meta", meta)
        print("filings", len(filings), "13D", sum(1 for f in filings if is_schedule_13d(f["form_type"])))
        print("financial years", [r["fiscal_year"] for r in fin][-6:])
