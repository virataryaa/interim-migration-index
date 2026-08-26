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

All verified live against LSEG:

| Series | RIC pattern | Field |
|---|---|---|
| Index Long | `4<CFTC_CODE>PLNG` | `COMM_LAST` |
| Index Short | `4<CFTC_CODE>PSHT` | `COMM_LAST` |
| Total OI | `3CFTC<CFTC_CODE>OI` | `COMM_LAST` |
| Price | see `price_ric` per commodity below | `TRDPRC_1` |

This is the exact same RIC family and field names the COT_ALL project's
`cot_lseg_backfill.py` already uses in production for KC/CC/SB/CT — this
project just widens the same pattern to all 13 ag commodities and pulls the
Index Traders category instead of MM/Producer/Swap.

**Price RIC — verified 2026-08-26 against the source workbook's raw `DATA`
sheet.** CFTC Long/Short/OI matched byte-for-byte for all 13 commodities on
the first attempt; only price needed fixing. The workbook's `TICKER` sheet
specifies a *mix* of `v1` (most-active/volume-based) and `c2` (calendar
2nd-month) per commodity — not `c2` uniformly, which an earlier version of
this project assumed after a few `v1` tickers came back `Access Denied`:

| Commodity | `price_ric` used | Why |
|---|---|---|
| SRW, Corn, Soybean, Bean Oil | `Wv1`/`Cv1`/`Sv1`/`BOv1` | the workbook's own ticker — directly accessible, matches to <0.1% |
| HRW, Meal, Hog, Live, Feeder | `KWc2`/`SMc2`/`LHc2`/`LCc2`/`FCc2` | the workbook's own ticker (already `c2`) |
| Cotton, Coffee | `CTc2`/`KCc2` | workbook wants `CTv1`/`KCv1`, both `Access Denied` on this account; `c2` already matches closely (<0.3%) |
| Cocoa, Sugar | `CCc1`/`SBc1` | workbook wants `CCv1`/`SBv1`, both `Access Denied`; `c2` was off by ~2.7%/~6%, but `c1` (front-month) matches to <0.02% |

## The weight-deviation methodology

**Verified line-by-line against the source workbook** (`Index Monitoring.xlsx`,
`RECAP` sheet) on 2026-08-26 — this replaced an earlier, unverified
reconstruction that used a static January-reference lot count. The real
formula does no such per-year anchoring:

`target_weight_pct` per commodity (in `Code/ingest_lseg.py`'s `COMMODITIES`
dict) is the exact "Used" row from the workbook's "GSCI and BCOM weights"
sheet — full precision, not rounded (rounding to whole numbers previously
summed to 101% instead of 100%). It is a **fixed constant** — re-derived
once a year at the GSCI/BCOM January rebalance, but never re-anchored
per-date the way the earlier version assumed.

At every date (workbook `RECAP` cell refs in parens):
1. `Actual Weight % = Nominal Net USD ÷ Total Pool × 100` (`BQ = BC/$BP`)
2. `Deviation % = Actual Weight % − Target Weight %` (`CL = BQ-BQ$2`)
3. `Deviation $ = Deviation % ÷ 100 × Total Pool` (`CY = CL*BP`)
4. `Deviation Lots = Deviation $ ÷ (Price × Multiplier)` at that date's own
   price (`DL = CL*BP/O`)
5. `Deviation % of OI = Deviation Lots ÷ Total OI × 100` (`DY = DL/AB`)

All five are computed fresh at every date — none of them hold anything
static across the year. Steps 4 and 5 feed the "Weight Deviation (Lots)"
tab and the Snapshot tab's extra columns.

## What's here

- **`Code/ingest_lseg.py`** — pulls all 13 commodities' Long/Short/OI/Price
  history from LSEG, computes `Index Net`, `Nominal Net USD`
  (= Index Net × Price × Multiplier), and `Net Pct OI`. `--full` for a full
  2016-01-01 backfill, no flag for a 30-day incremental refresh.
- **`Database/index_positioning.parquet`** — one row per commodity per
  (weekly-cadence) date.
- **`Dashboard/app.py`** — four tabs (consolidated from an earlier six-tab
  layout that spread the same information across overlapping views):
  - **Snapshot** — Target vs Actual weight bar chart (By Commodity or By
    Group — group-level target/actual % are a plain sum of member
    commodities' %, since every commodity's % already shares the same
    denominator, Total Pool $), plus a master HTML table with every
    commodity's group, target %, actual %, deviation ($, lots, %OI),
    nominal $, net lots, and % of OI side by side, at the latest date.
  - **What It Is** — actual positioning only, no target comparison: Total
    Ags Net Index and By-Group breakdown (Softs/Grains/Oilseeds/Livestock)
    side by side, plus a Position-vs-Price attribution table (1/4/13/52-week
    lookback) that splits each commodity's Nominal $ change into how much
    came from lots changing vs price moving.
  - **What It Should Be** — actual vs the GSCI/BCOM target: the
    Over/Under-vs-target lots chart (Softs/Grains/Oilseeds/Livestock/Custom
    selector), a scrollable weekly %-of-weight deviation table across all
    13 commodities, and a target-weight/RIC reference expander.
  - **Per-Commodity Detail** — Long/Short/Net, % of OI, nominal $ for one
    selected commodity.

Grains (SRW/HRW/Corn) and Oilseeds (Soybean/Bean Oil/Meal) are tracked as
separate groups — they're different crop complexes even though both come
from the CBOT grain/oilseed pit.
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
