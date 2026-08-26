# Index — CFTC "Index Traders" Positioning

Tracks passive commodity-index-fund money (the CFTC CIT report's **Index
Traders** category — GSCI/BCOM trackers, not discretionary Managed Money)
across 13 agricultural commodities, converts reported lots into real dollar
notional, and compares each commodity's share of that pool to its
theoretical GSCI/BCOM target weight. Since index funds only rebalance back
to target once a year (January), the gap between actual and target is a
structural, largely mechanical flow, not a discretionary bet — a commodity
running well above its target weight is due scheduled *selling* at the
next rebalance; one running below is due scheduled *buying*.

## Commodities & RIC scheme

13 ag commodities from the CFTC CIT report: SRW, HRW, Corn, Soybean, Bean
Oil, Meal, Cotton, Hog, Live Cattle, Feeder Cattle, Cocoa, Sugar, Coffee.

All verified live against LSEG (2026-08-26):

| Series | RIC pattern | Field |
|---|---|---|
| Index Long | `4<CFTC_CODE>PLNG` | `COMM_LAST` |
| Index Short | `4<CFTC_CODE>PSHT` | `COMM_LAST` |
| Total OI | `3CFTC<CFTC_CODE>OI` | `COMM_LAST` |
| Price | `<ROOT>c2` (2nd-month continuation) | `TRDPRC_1` |

This is the exact same RIC family and field names the COT_ALL project's
`cot_lseg_backfill.py` already uses in production for KC/CC/SB/CT — this
project just widens the same pattern to all 13 ag commodities and pulls the
Index Traders category instead of MM/Producer/Swap.

**Price RIC note:** the original reference sheet used a mix of `v1`/`c2`
tickers (`Wv1`, `KWc2`, `CTv1`, `KCv1`, ...); several of the `v1` tickers
(`CTv1`, `KCv1`, `CCv1`, `SBv1`) came back `Access Denied` against this
account's entitlements. `<ROOT>c2` is used uniformly for all 13 instead —
verified live for every commodity.

## The weight-deviation methodology

`target_weight_pct` per commodity (in `Code/ingest_lseg.py`'s `COMMODITIES`
dict) comes from the provided GSCI 2026 / BCOM 2026 60/40 blend, re-weighted
to the ag-only subset (the "Used" row on the reference sheet). **Re-derive
these at each January GSCI/BCOM rebalance** — they are 2026 weights, not a
permanent constant.

The Dashboard's "Weight Deviation (Lots)" tab: at the first available date
each year, the total actual Index-Trader pool ($) across all 13 commodities
is taken as that year's reference pool size. Each commodity's *target* lot
count is back-solved from its target weight against that reference pool and
that date's price, then held static for the rest of the year (no further
rebalancing assumed until the next January). What's plotted is actual
reported net lots minus that static reference — this is a specific,
documented interpretation of "deviation from start-of-year weights," not
verbatim from the brief (which described the concept but not the exact
reference-point formula), so it's worth checking against the original
source of the reference chart if the shape doesn't match.

## What's here

- **`Code/ingest_lseg.py`** — pulls all 13 commodities' Long/Short/OI/Price
  history from LSEG, computes `Index Net`, `Nominal Net USD`
  (= Index Net × Price × Multiplier), and `Net Pct OI`. `--full` for a full
  2016-01-01 backfill, no flag for a 30-day incremental refresh.
- **`Database/index_positioning.parquet`** — one row per commodity per
  (weekly-cadence) date.
- **`Dashboard/app.py`** — six tabs: **Overview** (the original brief's
  visuals together — Total Ags Net Index, Under/Over vs Start-of-Year
  Weights, and a scrollable weekly %-of-weight deviation table across all
  13 commodities, plus a target-weight/RIC reference expander), **Snapshot**
  (one master HTML table — every commodity's group, target %, actual %,
  deviation, $ notional, net lots, and % of OI side by side, at the latest
  date), Total Ags Index (aggregate $ pool, 2016–present), Weight Deviation
  (Lots), Composition vs Target (latest actual vs target weight, all 13, as
  an HTML-styled table), Per-Commodity Detail (Long/Short/Net, % of OI,
  nominal $).
- **`Automator/run.bat`** — runs the incremental ingest, commits + pushes
  `Database/` if changed, emails pass/fail via Outlook.

## Running it

```bash
python Code/ingest_lseg.py --full     # first run — full 2016-01-01 backfill
python Code/ingest_lseg.py            # subsequent runs — 30-day incremental
streamlit run Dashboard/app.py
```

Requires an authenticated LSEG Workspace/Eikon session on the host running
the ingest.
