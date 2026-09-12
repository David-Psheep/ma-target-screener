# Methodology — M&A Target Screener

Interview / resume language you can adapt. The product is a **screen**, not a model that “predicts deals.”

---

## One-liner (resume)

Built a Python/Streamlit screener that ranks ~50 mid-cap issuers on five EDGAR- and market-based proxies for takeout likelihood (margin compression, 13D activism, industry 8-K deal flow, valuation vs. own history, leverage stress), with percentile scoring, live-reweighting, and SQLite caching.

---

## How to talk about it in 60 seconds

“I didn’t try to predict who gets acquired — that’s not identifiable from public filings alone. I built a **relative ranking** of mid-caps on signals that *show up around* sale processes: profitability pressure, an activist 13D, peers actually closing deals, a discount to the name’s own multiple, and rising leverage. Each signal is a percentile versus the universe so I’m not imposing arbitrary cutoffs. Weights default to something an associate might start with, but the UI renormalizes sliders to 100% so you can stress-test the book live. The app caches EDGAR and Yahoo in SQLite because those APIs are unofficial and fail; Refresh Data is explicit, startup is instant.”

---

## Why each signal is a legitimate *proxy*

### 1. Margin compression (25% default)

**Construction.** Operating margin = `OperatingIncomeLoss / Revenues` from SEC companyfacts (10-K USD facts, latest filed point per fiscal year). OLS slope over the last five years (or fewer if that is all that exists). Raw score = **minus** the slope so compression ranks higher. Then percentile vs. universe.

**Why it belongs.** Strategic buyers pay for cost synergies when a target’s standalone margins are fading. Sponsors underwrite “fixable” operations. A board that has already missed several years of margin is more likely to hire a banker. This is correlation with *process initiation*, not with deal completion or premium.

**Failure modes to volunteer.** Tagging differs (`Revenues` vs. `RevenueFromContractWithCustomerExcludingAssessedTax`). Banks and some insurers will simply have nulls and drop out of this sleeve. A one-year COVID dip can look like “compression.”

### 2. Activist investor — Schedule 13D (25%)

**Construction.** From `submissions` recent filings, detect `SC 13D` / `SC 13D/A` and **exclude 13G**. Last 6 months → raw 100; 6–12 months → 60; else 0; then percentile.

**Why it belongs.** 13D is the statutory “I may influence control” form. Campaigns often explicitly put a sale on the table. 13G is the passive analogue (index funds, 13G filers) and would flood the screen with noise if included.

**Failure modes.** The submissions JSON is a rolling window, not a complete 13D history. A settled activist from 18 months ago will look like a zero. Amendments (`13D/A`) count because they keep the situation live.

### 3. Industry consolidation — 8-K Item 2.01 (20%)

**Construction.** Issuer SIC from submissions. Peers = other universe tickers with the same **2-digit** SIC. Count how many of those peers filed an 8-K whose `items` list includes **2.01** (completion of acquisition or disposition of assets) in 24 months. We also try the unofficial EFTS full-text index as a backfill when `items` is blank.

**Why 2-digit, not 4-digit.** In a 50-name book, 4-digit SIC is mostly singletons. 2-digit is the industry-group cut used in a lot of first-pass comps.

**Why Item 2.01.** It is a *closed* deal (or asset sale), not a rumor and not Item 1.01 (entry into a material agreement) which fires on a lot of non-M&A contracts.

**Failure modes.** Item 2.01 includes dispositions, not just acquisitions. A peer selling a division still heats the score. Universe bias: if you only load one bank, financials will show zero peer deals by construction.

### 4. Valuation discount vs. own history (15%)

**Construction.** Prefer current trailing P/E from yfinance. Approximate a 3-year average multiple from year-end closes divided by current trailing EPS, or year-end price × current shares / annual net income when the income statement is available. Raw score = `(avg − current) / |avg|`. Then percentile.

**Why it belongs.** In practice bankers still lead with “it’s cheap vs. itself.” Relative undervaluation is a necessary (not sufficient) condition for many financial buyers and for strategies that cannot pay a full premium on a peak multiple.

**Limitation you must say out loud.** Free Yahoo data is **not** a historical PE vendor series. Holding EPS constant means the “average multiple” mostly tracks the **price** path. We refuse to mix EV/EBITDA (current) with a P/E-like history; if we cannot compare like with like, the signal is null rather than a fake 0% discount. See comments in `market_data.py`.

### 5. Leverage stress (15%)

**Construction.** `(LongTermDebtNoncurrent + LongTermDebtCurrent) / OperatingIncomeLoss` for the two most recent fiscal years with positive operating income. Raw score = change in that ratio (up = worse). Then percentile.

**Why it belongs.** Refinancing walls, downgrades, and sponsor recaps all push boards toward strategic alternatives. Rising leverage with flat or falling EBIT is a classic “something has to give” tape.

**Limitation.** Spec (and XBRL reality): D&A is not reliably tagged, so **EBITDA is proxied by operating income**. That overstates leverage for high-D&A industrials and understates it if operating income is inflated by one-time items. Negative operating income is excluded because the ratio is meaningless.

---

## Scoring philosophy (expect this follow-up)

- **Percentiles, not cutoffs.** A P/E of 12 means nothing without the rest of the book. Rank vs. universe updates when the universe changes.
- **No imputed 50s.** Missing data is missing. Composite renormalizes across available sleeves so a bank without “revenue” is not automatically punished on margin.
- **Weights are a prior, not truth.** Defaults overweight the two signals that are most “process-like” (margins, 13D). Interviewers can drag sliders; ranking updates without another SEC pull.
- **Cache aggressively.** EDGAR will 403/429. The product has to work in a demo when the network is ugly.

---

## What this is not

- Not a probability of deal announcement.
- Not a premium or timing model.
- Not a substitute for reading the 10-K, the 13D letter, or the credit agreement.
- Not investment advice.

If asked how you would extend it: add 8-K Item 1.01 + exhibit parsing for merger agreements, Form 4 cluster detection, credit-spread or ratings notches, and a proper point-in-time PE from a paid vendor so the valuation sleeve stops using a constant-EPS approximation.
