"""
Yahoo Finance market-data wrappers.

Why a thin wrapper:
  yfinance is unofficial, occasionally returns empty frames, and `info` can
  hang or omit keys (especially EV/EBITDA for banks). Callers should treat
  every field as Optional and keep screening the rest of the universe.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_quote(ticker: str) -> dict[str, Any]:
    """
    Snapshot used by the dashboard filters and the valuation signal.

    Returns keys that may all be None if Yahoo is down or the ticker is odd-lot.
    """
    empty = {
        "ticker": ticker.upper(),
        "price": None,
        "market_cap": None,
        "trailing_pe": None,
        "ev_ebitda": None,
        "week52_low": None,
        "week52_high": None,
        "sector": None,
        "industry": None,
    }
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance is not installed")
        return empty

    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        if not isinstance(info, dict):
            info = {}
        # fast_info is often more reliable for last price / market cap.
        fast = {}
        try:
            fast = dict(t.fast_info) if t.fast_info is not None else {}
        except Exception as exc:  # noqa: BLE001 — yfinance raises assorted types
            logger.debug("fast_info unavailable for %s: %s", ticker, exc)

        price = _safe_float(fast.get("last_price")) or _safe_float(info.get("currentPrice")) or _safe_float(
            info.get("regularMarketPrice")
        )
        market_cap = _safe_float(fast.get("market_cap")) or _safe_float(info.get("marketCap"))
        trailing_pe = _safe_float(info.get("trailingPE"))
        ev_ebitda = _safe_float(info.get("enterpriseToEbitda"))
        week52_low = _safe_float(fast.get("year_low")) or _safe_float(info.get("fiftyTwoWeekLow"))
        week52_high = _safe_float(fast.get("year_high")) or _safe_float(info.get("fiftyTwoWeekHigh"))
        sector = info.get("sector")
        industry = info.get("industry")

        return {
            "ticker": ticker.upper(),
            "price": price,
            "market_cap": market_cap,
            "trailing_pe": trailing_pe,
            "ev_ebitda": ev_ebitda,
            "week52_low": week52_low,
            "week52_high": week52_high,
            "sector": sector if isinstance(sector, str) else None,
            "industry": industry if isinstance(industry, str) else None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("yfinance quote failed for %s: %s", ticker, exc)
        return empty


def get_historical_multiples(ticker: str) -> dict[str, Any]:
    """
    Approximate a 3-year average valuation multiple from free yfinance data.

    LIMITATION (read this in an interview):
      Yahoo does not publish a clean historical trailing-P/E series through the
      free API. We approximate each of the last ~3 year-end P/Es as:

          year_end_close / trailing_EPS

      using *current* trailing EPS (from info['trailingEps']) held constant
      across history when a per-year EPS series is not available. That means
      the 'historical multiple' mostly captures PRICE variation, not a true
      restated earnings yield. When trailing EPS is missing we fall back to
      EV/EBITDA (current only) vs that same current figure — i.e. no discount —
      and the valuation signal becomes None so it does not pollute rankings.

    If annual net income *and* year-end close exist, we prefer
    price * (current shares) / net_income as a second-best 'P/E' for that year.
    """
    result: dict[str, Any] = {
        "ticker": ticker.upper(),
        "current_multiple": None,
        "multiple_type": None,
        "avg_3y_multiple": None,
        "discount_pct": None,
        "yearly": [],
        "method_note": "",
    }
    try:
        import yfinance as yf
    except ImportError:
        result["method_note"] = "yfinance missing"
        return result

    try:
        t = yf.Ticker(ticker)
        info = t.info if isinstance(t.info, dict) else {}
        quote = get_quote(ticker)

        current_pe = quote.get("trailing_pe")
        current_ev = quote.get("ev_ebitda")
        trailing_eps = _safe_float(info.get("trailingEps"))
        shares = _safe_float(info.get("sharesOutstanding"))

        hist = t.history(period="3y", auto_adjust=True)
        if hist is None or hist.empty:
            result["method_note"] = "no price history"
            result["current_multiple"] = current_pe or current_ev
            result["multiple_type"] = "trailing_pe" if current_pe else ("ev_ebitda" if current_ev else None)
            return result

        # Year-end (or last available) close for each calendar year in the window.
        closes = hist["Close"].dropna()
        yearly_close = closes.groupby(closes.index.year).last()

        yearly_pe: list[dict[str, Any]] = []
        income = None
        try:
            income = t.income_stmt
        except Exception as exc:  # noqa: BLE001
            logger.debug("income_stmt failed for %s: %s", ticker, exc)

        net_income_by_year: dict[int, float] = {}
        if income is not None and not income.empty:
            # yfinance income_stmt columns are period-end timestamps.
            ni_row = None
            for label in ("Net Income", "NetIncome", "Net Income Common Stockholders"):
                if label in income.index:
                    ni_row = income.loc[label]
                    break
            if ni_row is not None:
                for col, val in ni_row.items():
                    try:
                        yr = int(pd.Timestamp(col).year)
                        fv = _safe_float(val)
                        if fv is not None:
                            net_income_by_year[yr] = fv
                    except Exception:  # noqa: BLE001
                        continue

        for yr, close in yearly_close.items():
            close_f = _safe_float(close)
            if close_f is None:
                continue
            pe_approx = None
            method = ""
            ni = net_income_by_year.get(int(yr))
            if ni and shares and ni != 0:
                # Market cap at year-end using *current* share count (another limitation).
                pe_approx = (close_f * shares) / ni
                method = "year_end_price_x_current_shares / annual_net_income"
            elif trailing_eps and trailing_eps != 0:
                pe_approx = close_f / trailing_eps
                method = "year_end_price / current_trailing_eps (EPS held constant)"
            yearly_pe.append(
                {"year": int(yr), "close": close_f, "multiple": pe_approx, "method": method}
            )

        multiples = [r["multiple"] for r in yearly_pe if r["multiple"] is not None and r["multiple"] > 0]
        avg = sum(multiples) / len(multiples) if multiples else None

        current = current_pe
        mtype = "trailing_pe"
        if current is None or current <= 0:
            current = current_ev
            mtype = "ev_ebitda"

        discount = None
        if current is not None and avg is not None and avg != 0 and mtype == "trailing_pe":
            # Positive discount_pct = cheaper than own history.
            discount = (avg - current) / abs(avg)
        elif current is not None and avg is not None and avg != 0 and mtype == "ev_ebitda":
            # We usually cannot reconstruct a 3y EV/EBITDA path from free data.
            # If we only have current EV/EBITDA, skip rather than fake a 0% discount.
            if any(r["multiple"] for r in yearly_pe):
                # yearly_pe is a P/E approximation; do not mix with EV/EBITDA.
                discount = None
                avg = None
                result["method_note"] = (
                    "Current multiple is EV/EBITDA but history is P/E-like; "
                    "valuation signal left blank rather than mixing units."
                )
            else:
                discount = None

        if not result["method_note"]:
            result["method_note"] = (
                "3y average uses year-end close divided by current trailing EPS "
                "or by annual net income / current shares. Not a vendor PE series."
            )

        result.update(
            {
                "current_multiple": current,
                "multiple_type": mtype if current is not None else None,
                "avg_3y_multiple": avg,
                "discount_pct": discount,
                "yearly": yearly_pe,
            }
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("historical multiples failed for %s: %s", ticker, exc)
        result["method_note"] = str(exc)
        return result
