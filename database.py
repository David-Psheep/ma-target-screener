"""
Local SQLite cache.

Why SQLite:
  A 50-name universe with filings + annual facts is tiny, needs zero ops, and
  lets Streamlit open instantly on later visits. Refresh Data is the only
  path that hits SEC/Yahoo.

WAL mode: Streamlit reruns can overlap a refresh; WAL reduces 'database is locked'.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "screener.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    ticker TEXT PRIMARY KEY,
    cik TEXT,
    name TEXT,
    sic TEXT,
    sic_description TEXT,
    sector TEXT,
    industry TEXT,
    price REAL,
    market_cap REAL,
    trailing_pe REAL,
    ev_ebitda REAL,
    week52_low REAL,
    week52_high REAL,
    last_refreshed TEXT
);

CREATE TABLE IF NOT EXISTS financials (
    ticker TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    revenue REAL,
    operating_income REAL,
    gross_profit REAL,
    net_income REAL,
    lt_debt_noncurrent REAL,
    lt_debt_current REAL,
    total_debt REAL,
    ebitda_proxy REAL,
    operating_margin REAL,
    PRIMARY KEY (ticker, fiscal_year)
);

CREATE TABLE IF NOT EXISTS filings (
    ticker TEXT NOT NULL,
    form_type TEXT,
    filing_date TEXT,
    accession_number TEXT,
    items TEXT,
    PRIMARY KEY (ticker, accession_number)
);

CREATE TABLE IF NOT EXISTS scores (
    ticker TEXT PRIMARY KEY,
    computed_at TEXT,
    margin_compression REAL,
    activist REAL,
    industry_consolidation REAL,
    valuation_discount REAL,
    leverage_stress REAL,
    composite_score REAL,
    details_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_filings_ticker_date ON filings (ticker, filing_date);
CREATE INDEX IF NOT EXISTS idx_financials_ticker ON financials (ticker);
"""


@contextmanager
def get_conn(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA_SQL)


def upsert_company(row: dict[str, Any], db_path: Path = DB_PATH) -> None:
    cols = [
        "ticker",
        "cik",
        "name",
        "sic",
        "sic_description",
        "sector",
        "industry",
        "price",
        "market_cap",
        "trailing_pe",
        "ev_ebitda",
        "week52_low",
        "week52_high",
        "last_refreshed",
    ]
    payload = {c: row.get(c) for c in cols}
    payload["ticker"] = str(payload["ticker"]).upper()
    if not payload.get("last_refreshed"):
        payload["last_refreshed"] = datetime.now(timezone.utc).isoformat()
    placeholders = ", ".join(":" + c for c in cols)
    assignments = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "ticker")
    sql = f"""
        INSERT INTO companies ({", ".join(cols)})
        VALUES ({placeholders})
        ON CONFLICT(ticker) DO UPDATE SET {assignments}
    """
    with get_conn(db_path) as conn:
        conn.execute(sql, payload)


def replace_financials(ticker: str, rows: list[dict[str, Any]], db_path: Path = DB_PATH) -> None:
    ticker = ticker.upper()
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM financials WHERE ticker = ?", (ticker,))
        conn.executemany(
            """
            INSERT INTO financials (
                ticker, fiscal_year, revenue, operating_income, gross_profit,
                net_income, lt_debt_noncurrent, lt_debt_current, total_debt,
                ebitda_proxy, operating_margin
            ) VALUES (
                :ticker, :fiscal_year, :revenue, :operating_income, :gross_profit,
                :net_income, :lt_debt_noncurrent, :lt_debt_current, :total_debt,
                :ebitda_proxy, :operating_margin
            )
            """,
            [{**r, "ticker": ticker} for r in rows],
        )


def replace_filings(ticker: str, rows: list[dict[str, Any]], db_path: Path = DB_PATH) -> None:
    ticker = ticker.upper()
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM filings WHERE ticker = ?", (ticker,))
        if not rows:
            return
        conn.executemany(
            """
            INSERT OR REPLACE INTO filings (
                ticker, form_type, filing_date, accession_number, items
            ) VALUES (
                :ticker, :form_type, :filing_date, :accession_number, :items
            )
            """,
            [{**r, "ticker": ticker} for r in rows],
        )


def replace_scores(rows: list[dict[str, Any]], db_path: Path = DB_PATH) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM scores")
        for r in rows:
            details = r.get("details") or {}
            conn.execute(
                """
                INSERT INTO scores (
                    ticker, computed_at, margin_compression, activist,
                    industry_consolidation, valuation_discount, leverage_stress,
                    composite_score, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["ticker"].upper(),
                    r.get("computed_at") or now,
                    r.get("margin_compression"),
                    r.get("activist"),
                    r.get("industry_consolidation"),
                    r.get("valuation_discount"),
                    r.get("leverage_stress"),
                    r.get("composite_score"),
                    json.dumps(details),
                ),
            )


def load_companies_df(db_path: Path = DB_PATH) -> pd.DataFrame:
    init_db(db_path)
    with get_conn(db_path) as conn:
        return pd.read_sql_query("SELECT * FROM companies", conn)


def load_financials_df(db_path: Path = DB_PATH) -> pd.DataFrame:
    init_db(db_path)
    with get_conn(db_path) as conn:
        return pd.read_sql_query("SELECT * FROM financials", conn)


def load_filings_df(ticker: Optional[str] = None, db_path: Path = DB_PATH) -> pd.DataFrame:
    init_db(db_path)
    with get_conn(db_path) as conn:
        if ticker:
            return pd.read_sql_query(
                "SELECT * FROM filings WHERE ticker = ? ORDER BY filing_date DESC",
                conn,
                params=(ticker.upper(),),
            )
        return pd.read_sql_query("SELECT * FROM filings ORDER BY ticker, filing_date DESC", conn)


def load_scores_df(db_path: Path = DB_PATH) -> pd.DataFrame:
    init_db(db_path)
    with get_conn(db_path) as conn:
        df = pd.read_sql_query("SELECT * FROM scores", conn)
    if not df.empty and "details_json" in df.columns:
        df["details"] = df["details_json"].apply(
            lambda s: json.loads(s) if isinstance(s, str) and s else {}
        )
    return df


def last_refresh_timestamp(db_path: Path = DB_PATH) -> Optional[str]:
    df = load_companies_df(db_path)
    if df.empty or "last_refreshed" not in df.columns:
        return None
    vals = df["last_refreshed"].dropna()
    if vals.empty:
        return None
    return str(vals.max())
