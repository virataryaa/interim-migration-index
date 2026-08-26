"""
Index — CFTC "Index Traders" positioning ingest (LSEG)
=======================================================
Pulls the CFTC CIT report's Index Traders category (passive commodity-index
money — GSCI/BCOM trackers, not discretionary Managed Money) for 13 ag
commodities, plus each commodity's front-ish continuous price, and computes
dollar notional so positioning can be compared to each commodity's
theoretical GSCI/BCOM target weight.

RIC scheme (verified live against LSEG 2026-08-26, using the same
COMM_LAST/TRDPRC_1 fields the COT_ALL project's cot_lseg_backfill.py already
uses in production for the same CFTC data family):
  Index Long   4<CFTC_CODE>PLNG   (field COMM_LAST)
  Index Short  4<CFTC_CODE>PSHT   (field COMM_LAST)
  Total OI     3CFTC<CFTC_CODE>OI (field COMM_LAST)
  Price        <ROOT>c2           (field TRDPRC_1) — 2nd-month continuation,
                                   same choice cot_lseg_backfill.py made for
                                   KC/CC/SB/CT; used uniformly here for all 13.

The reference sheet's own price RICs (Wv1, KWc2, Cv1, CTv1, KCv1, ...) mix
v1/c2 per commodity and several (CTv1, KCv1, CCv1, SBv1) came back "Access
Denied" against this account's entitlements — <ROOT>c2 is used for every
commodity instead, verified live for all 13.

Usage:
    python ingest_lseg.py              # incremental (last 30 days)
    python ingest_lseg.py --full       # full history from 2016-01-01
"""
import argparse
import datetime
import logging
import sys
import time
from pathlib import Path

import pandas as pd
pd.set_option("future.no_silent_downcasting", True)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOG_DIR / "ingest_lseg.log", encoding="utf-8")],
)
log = logging.getLogger(__name__)

DB_DIR   = Path(__file__).parent.parent / "Database"
OUT_FILE = DB_DIR / "index_positioning.parquet"
START_FULL = "2016-01-01"

# ── Commodity config ──────────────────────────────────────────────────────────
# lot_size/unit/multiplier verified against the reference workbook's TICKER
# sheet. target_weight_pct is the exact "Used" row from the "GSCI and BCOM
# weights" sheet (GSCI 2026 / BCOM 2026, 60% GSCI + 40% BCOM blend,
# re-weighted to the ag-only subset so the 13 sum to 100%) — full precision,
# not rounded to whole numbers. Re-derive at each January GSCI/BCOM rebalance
# — these are 2026 weights, not a permanent constant.
#
# price_ric: verified 2026-08-26 against the source workbook's raw DATA
# sheet (13/13 CFTC Long/Short/OI already matched byte-for-byte; only price
# differed). The workbook's TICKER sheet specifies a mix of v1 (most-active/
# volume-based front-ish contract) and c2 (calendar 2nd-month) per commodity
# — NOT c2 uniformly, which is what an earlier version of this file assumed
# after several v1 tickers came back Access Denied on first pass. Re-checked
# ticker-by-ticker this time:
#   - Wv1, Cv1, Sv1, BOv1 (SRW/Corn/Soybean/Bean Oil) ARE accessible on this
#     account and match the workbook closely — used directly.
#   - KWc2, SMc2, LHc2, LCc2, FCc2 (HRW/Meal/Hog/Live/Feeder) are what the
#     workbook itself specifies — unchanged, already matched well.
#   - CTv1, KCv1, CCv1, SBv1 (Cotton/Coffee/Cocoa/Sugar) are genuinely
#     Access Denied on this account. For Cotton/Coffee, c2 already matched
#     the workbook closely, so it's kept as the fallback. For Cocoa/Sugar,
#     c2 was off by ~2.7%/~6%; c1 (front-month) matched to within ~0.02% —
#     so c1 is used as the accessible proxy for those two specifically.
COMMODITIES = {
    "SRW":      {"cftc_code": "001602", "price_ric": "Wv1",  "lot_size": 5000,  "unit": "USc", "multiplier": 50,  "target_weight_pct": 8.0368},
    "HRW":      {"cftc_code": "001612", "price_ric": "KWc2", "lot_size": 5000,  "unit": "USc", "multiplier": 50,  "target_weight_pct": 4.6636},
    "CORN":     {"cftc_code": "002602", "price_ric": "Cv1",  "lot_size": 5000,  "unit": "USc", "multiplier": 50,  "target_weight_pct": 16.0161},
    "SOYBEAN":  {"cftc_code": "005602", "price_ric": "Sv1",  "lot_size": 5000,  "unit": "USc", "multiplier": 50,  "target_weight_pct": 12.7899},
    "BEAN OIL": {"cftc_code": "007601", "price_ric": "BOv1", "lot_size": 60000, "unit": "USc", "multiplier": 600, "target_weight_pct": 3.6031},
    "MEAL":     {"cftc_code": "026603", "price_ric": "SMc2", "lot_size": 100,   "unit": "USD", "multiplier": 100, "target_weight_pct": 3.7437},
    "COTTON":   {"cftc_code": "033661", "price_ric": "CTc2", "lot_size": 50000, "unit": "USc", "multiplier": 500, "target_weight_pct": 3.7181},
    "HOG":      {"cftc_code": "054642", "price_ric": "LHc2", "lot_size": 40000, "unit": "USc", "multiplier": 400, "target_weight_pct": 8.6182},
    "LIVE":     {"cftc_code": "057642", "price_ric": "LCc2", "lot_size": 40000, "unit": "USc", "multiplier": 400, "target_weight_pct": 14.8981},
    "FEEDER":   {"cftc_code": "061641", "price_ric": "FCc2", "lot_size": 50000, "unit": "USc", "multiplier": 500, "target_weight_pct": 5.1364},
    "COCOA":    {"cftc_code": "073732", "price_ric": "CCc1", "lot_size": 10,    "unit": "USD", "multiplier": 10,  "target_weight_pct": 4.4464},
    "SUGAR":    {"cftc_code": "080732", "price_ric": "SBc1", "lot_size": 112000,"unit": "USc", "multiplier": 1120,"target_weight_pct": 7.0657},
    "COFFEE":   {"cftc_code": "083731", "price_ric": "KCc2", "lot_size": 37500, "unit": "USc", "multiplier": 375, "target_weight_pct": 7.2638},
}

FETCH_RETRIES = 3
FETCH_BACKOFF = 5


def _history(ld, ric: str, field: str, start: str, end: str, label: str) -> pd.Series | None:
    for attempt in range(FETCH_RETRIES):
        try:
            d = ld.get_history(universe=[ric], fields=[field], start=start, end=end,
                               interval="daily", count=10000)
            if d is None or d.empty:
                log.warning("  EMPTY: %s (%s)", ric, label)
                return None
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = [c[0] for c in d.columns]
            s = d.iloc[:, 0]
            s.index.name = "Date"
            return s
        except Exception as e:
            if "429" in str(e) and attempt < FETCH_RETRIES - 1:
                wait = FETCH_BACKOFF * (attempt + 1)
                log.warning("  rate-limited on %s, retrying in %ds", ric, wait)
                time.sleep(wait)
                continue
            log.warning("  MISSING: %s (%s) — %s", ric, label, str(e)[:150])
            return None
    return None


def fetch_commodity(ld, name: str, cfg: dict, start: str, end: str) -> pd.DataFrame:
    code = cfg["cftc_code"]
    long_s  = _history(ld, f"4{code}PLNG", "COMM_LAST", start, end, f"{name} Index Long")
    time.sleep(1)
    short_s = _history(ld, f"4{code}PSHT", "COMM_LAST", start, end, f"{name} Index Short")
    time.sleep(1)
    oi_s    = _history(ld, f"3CFTC{code}OI", "COMM_LAST", start, end, f"{name} Total OI")
    time.sleep(1)
    px_s    = _history(ld, cfg["price_ric"], "TRDPRC_1", start, end, f"{name} Price")
    time.sleep(1)

    if long_s is None or short_s is None:
        log.error("  %s: missing Long/Short — skipping commodity", name)
        return pd.DataFrame()

    df = pd.concat({"Index Long": long_s, "Index Short": short_s,
                    "Total OI": oi_s, "Price": px_s}, axis=1)
    df = df.dropna(subset=["Index Long", "Index Short"], how="all")
    df["Index Net"] = df["Index Long"] - df["Index Short"]
    df["Nominal Net USD"] = df["Index Net"] * df["Price"] * cfg["multiplier"]
    df["Net Pct OI"] = (df["Index Net"] / df["Total OI"] * 100).where(df["Total OI"] > 0)

    df = df.reset_index()
    df.insert(0, "Commodity", name)
    df["Lot Size"] = cfg["lot_size"]
    df["Unit"] = cfg["unit"]
    df["Multiplier"] = cfg["multiplier"]
    df["Target Weight Pct"] = cfg["target_weight_pct"]
    log.info("  %s -> %d rows, %s to %s", name, len(df),
             df["Date"].min().date() if len(df) else "—", df["Date"].max().date() if len(df) else "—")
    return df


def main(full: bool):
    import lseg.data as ld
    ld.open_session()
    log.info("LSEG session opened.")

    end = datetime.date.today().isoformat()
    if full:
        start = START_FULL
        log.info("Mode: FULL | window: %s -> %s", start, end)
    else:
        existing = pd.read_parquet(OUT_FILE) if OUT_FILE.exists() else None
        if existing is not None and not existing.empty:
            start = (pd.to_datetime(existing["Date"]).max() - pd.Timedelta(days=30)).date().isoformat()
        else:
            start = START_FULL
        log.info("Mode: INCREMENTAL | window: %s -> %s", start, end)

    frames = []
    for name, cfg in COMMODITIES.items():
        log.info("Fetching %s (CFTC %s)...", name, cfg["cftc_code"])
        frames.append(fetch_commodity(ld, name, cfg, start, end))

    ld.close_session()

    new_df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    if new_df.empty:
        log.error("No data fetched for any commodity — aborting without touching the parquet.")
        sys.exit(1)

    if OUT_FILE.exists() and not full:
        old_df = pd.read_parquet(OUT_FILE)
        combined = pd.concat([old_df, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["Commodity", "Date"], keep="last")
    else:
        combined = new_df

    combined = combined.sort_values(["Commodity", "Date"]).reset_index(drop=True)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(OUT_FILE, index=False)
    log.info("Saved -> %s | %d rows | %d commodities", OUT_FILE.name, len(combined),
             combined["Commodity"].nunique())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Full history from 2016-01-01")
    args = parser.parse_args()
    main(full=args.full)
