# M&A Target Screener

Local Streamlit app that screens a mid-cap public-company universe for **signals historically associated with becoming an M&A target**, ranks names with transparent percentile scores, and lets you inspect *why* a name ranked where it did.

**This is a screening tool for educational and portfolio use — not investment advice, and not a forecast that any company will be acquired.** High scores mean “looks more target-like *versus this universe on these proxies*,” not “will be taken out.”

![Dashboard screenshot](docs/screenshot.png)

*Add a PNG at `docs/screenshot.png` after you run the app (sidebar + ranked table + one detail view is the shot interviewers want).*

---

## Setup

Requires **Python 3.10+**. On a fresh macOS machine you also need the Xcode Command Line Tools so `python3` / `pip` / `git` actually run (`xcode-select --install`).

```bash
git clone <your-fork-url> ma-target-screener
cd ma-target-screener

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
```

Edit `.env` and set a real SEC User-Agent. The SEC blocks anonymous clients:

```text
SEC_USER_AGENT=Jane Doe jane.doe@university.edu
```

Use **your name and a contact email** — that is what EDGAR expects.

```bash
streamlit run app.py
```

On first launch the ranked table is empty. Click **Refresh Data** in the sidebar. Expect **~2–4 minutes** for ~50 issuers: each name needs submissions + companyfacts from EDGAR, Yahoo Finance for the quote, and we sleep 150ms between SEC calls (plus retries). Later visits load instantly from `screener.db`.

Optional checks:

```bash
# Scoring math only (no network)
pytest

# Hit EDGAR for three tickers (needs .env)
python sec_client.py
```

`screener.db` and `.env` are gitignored on purpose.

---

## How to use the dashboard

1. **Refresh Data** when you want a new pull (progress is per ticker).
2. Filter by **sector**, **2-digit SIC**, **market cap**, and **minimum composite**.
3. Drag the five **weight sliders**. Raw slider values are **normalized to 100%** so ranking always uses a proper weighted average. Signal *percentiles* do not change until you refresh data; only the composite does.
4. Click a ticker in the select box under the table for:
   - 5-year operating-margin chart
   - approximate valuation path
   - filing history (13D and 8-K Item 2.01 highlighted)
   - a written “why this score” note per signal
5. **Export to CSV** downloads the *current* filtered ranking.

Badge colors: **green ≥ 67th percentile** of the universe, **yellow 33–66**, **red < 33**. Green means “high on this takeout-proxy,” not “high quality.”

---

## Methodology (plain English)

Every name in `data/universe.csv` gets five raw measurements. Each measurement is converted to a **percentile rank (0–100) versus the other names in the book**. The composite is a weighted average of those percentiles (defaults: 25% margin, 25% activist, 20% industry, 15% valuation, 15% leverage). Missing inputs drop out of that name’s composite and the weights on the remaining signals are renormalized — we do not invent a “50.”

| Signal | What we measure | Why it is a *proxy* (not proof) |
| --- | --- | --- |
| Margin compression | Slope of operating margin (operating income / revenue) over up to 5 fiscal years. More negative → higher rank. | Boards under sustained profitability pressure often explore a sale, a scale merger, or a sponsor process. |
| Activist (13D) | Schedule **13D** (not 13G) in the last 12 months. 100 if last 6 months, 60 if 6–12, else 0 — then percentiled. | 13D is the “I may influence control” form. 13G is passive and is ignored. |
| Industry consolidation | Count of *other* universe peers with the same **2-digit SIC** that filed an **8-K Item 2.01** (completed acquisition or disposition) in 24 months. | Deals cluster by industry. Peer closings are a heat map, not a thesis on this issuer. |
| Valuation discount | Current trailing P/E (or EV/EBITDA if that is all Yahoo has) vs. an approximate 3-year average multiple from yfinance history. Larger discount → higher rank. | Acquirers shop cheaper assets first. Free Yahoo data is **not** a Capital IQ PE series — see comments in `market_data.py`. |
| Leverage stress | Change in (long-term debt current + noncurrent) / operating income across the last two fiscal years. Rising → higher rank. | Stressed capital structures force strategic alternatives. Operating income is a stand-in for EBITDA because D&A tags are messy in XBRL. That **overstates** leverage for capital-intensive names. |

Full interview write-up: [`METHODOLOGY.md`](METHODOLOGY.md).

---

## Project layout

| File | Role |
| --- | --- |
| `app.py` | Streamlit UI |
| `fetch_pipeline.py` | Universe loop, progress callback, score write |
| `sec_client.py` | EDGAR ticker map, submissions, companyfacts, EFTS search, retries |
| `market_data.py` | yfinance quote + 3y multiple approximation |
| `database.py` | SQLite schema and helpers (`screener.db`) |
| `scoring.py` | Five signals, percentiles, weighted composite |
| `data/universe.csv` | 50 mid-cap names across industrials, healthcare, consumer, tech, financials, energy, materials |
| `tests/test_scoring.py` | Sample-data unit tests |

---

## Data sources (exact endpoints)

- Ticker → CIK: `https://www.sec.gov/files/company_tickers.json` (cached under `data/cache/`)
- Filings / SIC: `https://data.sec.gov/submissions/CIK##########.json`
- XBRL facts: `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`
- Full-text (Item 2.01 cross-check): `https://efts.sec.gov/LATEST/search-index` (undocumented — parsed defensively)
- Market data: `yfinance`

SEC calls send `User-Agent: $SEC_USER_AGENT`, retry three times with exponential backoff, and sleep **0.15s** after each request.

---

## Expanding the universe

Append rows to `data/universe.csv` (`ticker,company_name`) and click **Refresh Data**. Prefer mid-caps; mega-caps are not the realistic takeout universe this screen is built for.
