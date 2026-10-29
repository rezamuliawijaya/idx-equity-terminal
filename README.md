# Indonesian Equity Research & Decision Terminal

A local, single-user research terminal for Indonesian (IDX) equities. It ingests financial statements and
prices from CSV (or manual entry), computes ratios and a DCF, scores companies on five factors, and maps
those scores onto a transparent `BUY / HOLD / SELL-REDUCE / AVOID / INSUFFICIENT DATA` decision. A delayed
IDX quote feed provides tape context next to the analysis, and never replaces the imported figures.

It is deliberately **not** a trading system. There is no broker connection, no order routing, and no bundled
financial statements. Every fundamental number in the application comes from a file you imported, and every
derived figure carries its formula, its inputs, and its status.

```
streamlit run app.py
```

---

## What this is, and what it is not

**Is**

- A reproducible pipeline: CSV → validated rows → periods → TTM → ratios → valuation → score → decision → report.
- Honest about missing data. A missing line item is `None`, never `0`. Banks get ratios suppressed where they
  are not meaningful. A stale price produces a data-quality penalty, not a silently invented price.
- Sector-aware. Banks are not valued on EV/EBITDA; mining, consumer, property and manufacturing have their own
  scoring expectations; the scoring profile is shown everywhere it matters.
- Auditable. Every metric stores a formula and the inputs used, and every stored period records its source URL,
  publication date and retrieval timestamp. Every stored quote records its vendor URL, last-trade time and
  retrieval time.
- Fully local. One SQLite file, one Streamlit process. The only outbound calls are the optional delayed quote
  feed and an AI provider you configure yourself.

**Is not**

- Investment advice, a forecast, or a backtest of a strategy that has been validated out of sample.
- A market-data vendor. Quotes come from an unofficial, delayed public endpoint, and the terminal ships with
  **synthetic demo data only**, clearly labelled as such.

> **The demo companies do not exist.** Tickers are prefixed `DEMO-`, the issuers are named "… (synthetic)",
> every figure is generated arithmetically, and prices are random walks. The demo exists to exercise the
> workflow end to end. It must never be read as research on a real issuer.

---

## Requirements

- Python 3.11+ (developed and tested on 3.12)
- Windows, macOS or Linux
- No paid services, no API keys. The core application needs no network; the delayed quote feed uses
  Yahoo Finance's public endpoints and degrades gracefully when they are unreachable.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate         # macOS / Linux
pip install -r requirements.txt
```

Development extras (pytest):

```powershell
pip install -r requirements-dev.txt
```

## Run

```powershell
streamlit run app.py
```

The app opens at <http://localhost:8501>. On first launch the database is created empty at
`data/terminal.db` and every page shows an empty state pointing at **Data Management**.

### Quick start with the synthetic dataset

In the app: **Data Management → Companies & demo → Load synthetic demo data**.

Or from the command line:

```powershell
python scripts/seed_demo_data.py            # seed the default database
python scripts/seed_demo_data.py --reset    # start from a clean database
python scripts/seed_demo_data.py --files-only  # just write the demo CSVs to data/sample
```

Then explore: **Market Overview → Stock Screener → Company Research → Valuation Lab → Investment Decisions**.
The quote tape and **Live Market** page need outbound access; enable the feed in **Data Management → Live feed**.

---

## Pages

| Page | Purpose |
|---|---|
| **Market Overview** | Database-wide counts, decision distribution, top scores, sector averages, data freshness, IHSG and advancer/decliner context. |
| **Live Market** | Delayed IDX quotes: status panel, quote grid, movers, intraday chart, symbol management, EOD sync into `prices`. |
| **Stock Screener** | Filter on metrics and score, sort, save/load presets, export CSV. |
| **Company Research** | Seven tabs: overview, statements, ratios, valuation, peers, risks, notes — with a delayed quote strip above the analysis. |
| **Valuation Lab** | Editable DCF assumptions, full projection schedule, WACC/terminal-growth sensitivity, enterprise-to-equity bridge, saved runs. |
| **Investment Decisions** | Decision board for every company, factor matrix, rule trace, risk flags, invalidation conditions, archived snapshots. |
| **Watchlist** | Research shortlist with target margin of safety, price alerts and a delayed live-price column, evaluated against stored data. |
| **Paper Portfolio** | Simulated IDX trading with lot sizes, fees, tax, slippage, position and sector limits, equity snapshots. |
| **Backtesting** | Point-in-time scoring, periodic rebalancing, costs, benchmark comparison, drawdown, trade log. |
| **Research Reports** | Markdown/PDF/Excel/JSON/CSV bundles, single or batch, with provenance tables. |
| **Data Management** | CSV import with validation preview, manual entry, company metadata, demo seeding, live feed, settings, provider status, backups, import log. |

### Interface

The UI is styled as a terminal rather than a web dashboard: black canvas, amber function codes, monospaced
figures, dense tables, a scrolling delayed-quote tape above the page, and a fixed status bar. The sidebar is
the function menu (`MKT`, `LIVE`, `SCRN`, `DES`, `VAL`, `DEC`, `MON`, `PORT`, `BT`, `RPT`, `DATA`). Each page
shows its code next to the title and a WIB blotter timestamp. Every live figure is labelled delayed.

---

## Live market data (delayed)

The live feed is **delayed, unofficial vendor data**, not an exchange feed and not an execution price.

| Aspect | Behaviour |
|---|---|
| Source | Yahoo Finance public `v7/finance/spark` (batched) and `v8/finance/chart/<symbol>` endpoints. |
| Coverage | Any IDX symbol; the default feed is IHSG plus 11 liquid tickers. `IHSG` maps to `^JKSE`; `BBCA` maps to `BBCA.JK`. |
| Delay | Roughly 10–20 minutes. The last-trade timestamp is shown, not just the retrieval time. |
| Storage | `live_symbols` holds the symbol map; `live_quotes` holds the last good quote per symbol with vendor URL, quote time, retrieval time and a `delayed` flag. |
| Failure | A failed or rate-limited request raises a typed error. The UI falls back to the last stored quote and labels it **STALE**; a failed quote never overwrites a good price. |
| Unknown symbol | Reported as an error with no price. Nothing is zero-filled or carried forward. |
| EOD sync | **Sync EOD to prices** writes the vendor's actual daily closes into `prices` (companies that already exist) and into `benchmark_prices` (IHSG). That is the only path from live data into the analysis. |
| Refresh | The tape refreshes every 30 s; the Live Market grid every 60 s (toggleable). A 15 s in-process cache and a request throttle keep the vendor load sane. |

The fundamental side never reads `live_quotes`. Scores, fair values and margin of safety are computed from the
closes stored in `prices`; the quote strip on Company Research shows both numbers and the distance between them
when they differ.

Try it without leaving the app: **Data Management → Live feed → Enable default IDX feed**, then **Refresh
quotes**. On a machine without outbound access the page states that the vendor is unreachable and shows the
stored quotes with their age instead.

---

## Data model

```
companies ─┬─ periods ── financial_items
           ├─ prices
           ├─ watchlist
           ├─ research_notes
           └─ valuation_runs / recommendations

benchmark_prices, transactions, equity_snapshots, portfolios,
screener_presets, import_log, settings, meta,
live_symbols, live_quotes
```

Each `periods` row is one reporting period: annual or quarter, cumulative or discrete, consolidated or
parent-only. Each `financial_items` row is one line item with both the as-reported value and unit *and* the
converted absolute rupiah value.

### Two supported CSV layouts

**Long format** (recommended — one row per line item):

```csv
ticker,period_end,period_type,statement,line_item,value,unit,currency,is_consolidated,source_url,publication_date,notes
BBCA,2023-12-31,annual,income,revenue,102300000000000,rupiah,IDR,true,https://www.idx.co.id/,2024-03-31,
BBCA,2023-12-31,annual,balance,total_assets,1300000000000000,rupiah,IDR,true,https://www.idx.co.id/,2024-03-31,
```

**Wide format** (one row per period, one column per line item):

```csv
ticker,company_name,sector,period_end,period_type,unit,currency,is_consolidated,revenue,net_income,total_assets,...
BBCA,Bank Central Asia,Financials,2023-12-31,annual,million,IDR,true,102300000,40500,1300000000,...
```

Price CSV: `ticker,date,close` with optional `volume` and `value`.

Templates: **Data Management → Statements/Prices → Generate templates**, or look in `data/imports/`.
Header aliases are accepted in English and Indonesian (`nilai`, `tanggal`, `sektor`, `konsolidasi`, …), and
file encodings UTF-8, cp1252 and latin-1 all parse.

### Units and currencies

Every value is converted to absolute rupiah on import using the declared unit (`rupiah`, `thousand`,
`million`, `billion`, `trillion`). A value declared in millions is stored at ×1,000,000. Nothing is inferred
from magnitude — guessed units are how a terminal ends up off by a factor of a thousand.

### Annual and quarterly data

IDX quarterly reports are **cumulative year-to-date**. The importer stores the declared basis
(`cumulative` / `discrete`) and the parser derives discrete quarters by differencing consecutive cumulative
periods within the same fiscal year. Q1 must already be discrete; if the sequence is broken, TTM falls back
to the latest annual figure and says so rather than silently producing a wrong number.

TTM is built from four discrete quarters when available, otherwise from the latest annual period. The
`ttm_explanation` string is surfaced in the UI and in every report.

### Validation

Two layers:

1. **Row level** (Pydantic): types, ticker shape, unit vocabulary, currency, date parsing.
2. **Dataset level**: duplicate detection, accounting identities (assets = liabilities + equity, gross profit,
   EBIT build, EBITDA), and completeness checks.

Issues carry a severity. Errors block the import; warnings do not. The UI shows the exact rows before you
commit, and a failed write is rolled back rather than half-applied.

---

## Analytics

### Ratios

- Profitability: gross/operating/net margin, ROA, ROE, ROIC, cash conversion.
- Growth: revenue and earnings growth (YoY and CAGR), TTM growth.
- Health: current ratio, quick ratio, D/E, net debt/EBITDA, interest coverage, equity/assets.
- Cash flow: CFO/net income, FCF, FCF margin, dividend payout.
- Valuation: P/E, P/B, EV/EBITDA, EV/Sales, FCF yield, dividend yield.
- Momentum/liquidity: price returns over multiple horizons, average daily traded value.

Metric status is one of `reported`, `calculated`, `estimated`, `not_meaningful`, `unavailable`. Every metric
carries its formula and inputs. A `not_meaningful` ratio (for a bank: current ratio, D/E, net debt/EBITDA,
interest coverage) is displayed as such and excluded from scoring — not scored as zero.

### DCF

Unlevered free cash flow to the firm:

```
FCFF  = EBIT × (1 − tax) + D&A − Capex − ΔNWC
EV    = Σ PV(FCFF_t) + PV(terminal value)
TV    = FCFF_{n+1} / (WACC − g)
Equity value = EV − net debt + non-operating assets − minority interest
Value per share = equity value / shares outstanding
```

- Growth fades linearly from the year-1 rate to terminal growth.
- `terminal growth ≥ WACC` is rejected outright; sensitivity cells for those combinations are blank.
- The terminal value share of EV is shown, and warned about when it dominates.
- Margin of safety = `(fair value − price) / fair value`.
- Banks are excluded from FCFF because interest is an operating item; they use justified P/B
  `(ROE − g) / (COE − g)` with `COE = WACC + 2%`, plus P/B and P/E multiples.

### Blended fair value

Available methods are blended with DCF 60% / relative 40% by default, **renormalised across whichever methods
actually produced a value**. A missing method is never weighted as zero and never imputed.

### Scoring and the decision engine

Five factors, configurable weights (defaults):

| Factor | Weight | Looks at |
|---|---|---|
| Quality | 30% | ROE, ROIC, margins, cash conversion |
| Growth | 20% | Revenue and earnings growth, consistency |
| Valuation | 30% | P/E, P/B, EV/EBITDA, FCF yield, margin of safety |
| Health | 10% | Leverage, liquidity, coverage |
| Momentum / liquidity | 10% | Price trend and traded value |

Each factor score is a weighted blend of documented breakpoints. Component detail is expandable in the UI.

Decision policy (defaults, all configurable):

- **BUY**: score ≥ 65, margin of safety ≥ 15%, confidence ≥ 70%, positive earnings, no critical flag.
- **HOLD**: score ≥ 45 without meeting every BUY condition.
- **SELL / REDUCE**: score < 45.
- **AVOID**: any unresolved critical flag.
- **INSUFFICIENT DATA**: factor coverage < 45%, confidence < 55%, or essential statements/prices stale or missing.

Confidence is factor coverage multiplied by data-quality penalties (synthetic data, stale prices, estimated
line items, missing periods). It measures evidence quality and model limitations — not the probability of
profit. Every recommendation carries factor contributions, reasons, risk flags, invalidation conditions and
the confidence calculation.

### Backtesting

Point-in-time fundamentals are served by `publication_date`; when it is missing, a configurable filing lag
(default 120 days) is applied so a period is never visible before it could have been filed. Rebalancing is
monthly or quarterly, costs and slippage are charged on every trade, and the benchmark is optional.

What the backtest cannot fix: survivorship bias without complete delisting records, corporate actions not
reflected in the imported price series, and a sample of four synthetic companies. The page states these
limitations on every run, and labels synthetic results as such.

---

## Configuration

Defaults live in `config/settings.py` and can be edited in **Data Management → Settings**. Settings persist
in the database, so they drive the whole terminal. Environment variables can relocate data:

```powershell
$env:TERMINAL_DATA_DIR = "D:\idx-data"
$env:TERMINAL_DB_PATH  = "D:\idx-data\terminal.db"
```

Copy `.env.example` to `.env` for a persistent local override. Supported variables:

| Variable | Meaning |
|---|---|
| `TERMINAL_DATA_DIR` | Root folder for database, imports, exports, backups, samples |
| `TERMINAL_DB_PATH` | Explicit database file path |
| `AI_PROVIDER`, `AI_API_KEY`, `AI_MODEL`, `AI_BASE_URL` | Optional AI assistant (see below) |

### Optional AI research assistant

The terminal works fully without it. When configured (`openai`, `openrouter`, or a local `ollama`), the
assistant receives only figures already computed locally, and its reply is checked for numeric fidelity and
citation validity before display. It has no database access, no filesystem access, and cannot place orders.

---

## Testing

```powershell
python -m pytest
```

The suite covers unit normalization, cumulative-to-discrete quarterly conversion, TTM and annual fallback,
negative-earnings handling, DCF terminal value and the WACC guard, the equity bridge, margin of safety,
threshold boundaries, sector-specific scoring (including bank ratio suppression), trading costs,
look-ahead avoidance in the backtest, database backup/restore, the delayed-quote parser and fallbacks (with a
fake transport, so the tests never touch the network), EOD sync routing, and navigation through Streamlit's own
`AppTest` harness (which catches session-state and page-render faults a unit test cannot).

---

## Project layout

```
app.py                      Streamlit entry point and navigation
config/settings.py          Thresholds, sector mapping, dataclass settings, .env loader
src/data/                   Normalisation, validation, parsing, CSV import, providers, demo data
                            live.py (delayed IDX provider), live_sync.py (EOD → prices)
src/database/               SQLite schema, connection handling, backups, repositories
src/analytics/              Ratios, risk flags, valuation, scoring, decision engine, backtesting
src/portfolio/              Paper portfolio ledger and watchlist alerts
src/reports/                Report assembly and PDF/Excel/Markdown/JSON/CSV export
src/ai/                     Optional provider abstraction and guarded assistant
src/ui/                     One module per page, shared components
                            theme.py (terminal CSS), live_tape.py, live_market.py, live_widgets.py
scripts/seed_demo_data.py   CLI seeding
tests/                      Pytest suite
data/                       Database, imports, exports, backups, generated samples (git-ignored)
```

Outputs: `data/exports/` (reports), `data/backups/` (database backups), `data/sample/` (generated demo CSVs).

---

## Data provenance rules

These are enforced in code, not just documented:

1. No figure is invented. Missing values stay missing and are excluded from scoring and valuation.
2. Every stored period records source URL(s), publication date, retrieval timestamp and reporting basis.
3. Every derived metric exposes its formula and the inputs it used.
4. Every screen shows data freshness; stale or missing essentials downgrade the recommendation.
5. Synthetic data is labelled at the row, company, page and report level.
6. Imports are validated before writing, and writes that fail are rolled back.
7. All SQL uses parameter binding.
8. Live quotes are stored with vendor URL, last-trade time and retrieval time, and are always labelled as
   delayed. A failed request never overwrites the last good quote, and live prices never feed the valuation
   unless you explicitly run **Sync EOD to prices**, which writes real daily closes.

## Where to get real data

1. IDX listed-company financial statements: <https://www.idx.co.id/en/listed-companies/company-profiles>
2. IDX market statistics: <https://www.idx.co.id/en/market-data/statistics>
3. The issuer's own investor-relations pages — usually the primary document.

Record the exact URL and publication date on each imported row; both are shown in the reports.

---

## Limitations

- The live feed is delayed, unofficial and rate-limited. It is not a licensed exchange feed, it carries no
  bid/offer or depth, and it must not be used as an execution price. Corporate-action adjustment, delisting
  history and intraday history beyond what the vendor serves are not provided; import an adjusted series if
  you need one.
- Fundamental figures are only as good as what you import: no automatic statement downloader ships with this build.
- Rule-based scoring encodes the authors' priors. Thresholds are research parameters, not validated optima.
- The DCF is a sensitivity engine; its output is a scenario, not a target price.
- One paper portfolio and one local user. SQLite is single-writer.
- The demo dataset is synthetic and cannot validate anything.

Nothing here is investment advice. Verify every figure against the filed statements before acting on it.
