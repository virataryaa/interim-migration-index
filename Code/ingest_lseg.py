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
DAILY_PRICE_FILE = DB_DIR / "daily_prices.parquet"
START_FULL = "2016-01-01"

# ── Commodity config ──────────────────────────────────────────────────────────
# lot_size/unit/multiplier verified against the reference workbook's TICKER
# sheet.
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
    "SRW":      {"cftc_code": "001602", "price_ric": "Wv1",  "lot_size": 5000,  "unit": "USc", "multiplier": 50},
    "HRW":      {"cftc_code": "001612", "price_ric": "KWc2", "lot_size": 5000,  "unit": "USc", "multiplier": 50},
    "CORN":     {"cftc_code": "002602", "price_ric": "Cv1",  "lot_size": 5000,  "unit": "USc", "multiplier": 50},
    "SOYBEAN":  {"cftc_code": "005602", "price_ric": "Sv1",  "lot_size": 5000,  "unit": "USc", "multiplier": 50},
    "BEAN OIL": {"cftc_code": "007601", "price_ric": "BOv1", "lot_size": 60000, "unit": "USc", "multiplier": 600},
    "MEAL":     {"cftc_code": "026603", "price_ric": "SMc2", "lot_size": 100,   "unit": "USD", "multiplier": 100},
    "COTTON":   {"cftc_code": "033661", "price_ric": "CTc2", "lot_size": 50000, "unit": "USc", "multiplier": 500},
    "HOG":      {"cftc_code": "054642", "price_ric": "LHc2", "lot_size": 40000, "unit": "USc", "multiplier": 400},
    "LIVE":     {"cftc_code": "057642", "price_ric": "LCc2", "lot_size": 40000, "unit": "USc", "multiplier": 400},
    "FEEDER":   {"cftc_code": "061641", "price_ric": "FCc2", "lot_size": 50000, "unit": "USc", "multiplier": 500},
    "COCOA":    {"cftc_code": "073732", "price_ric": "CCc1", "lot_size": 10,    "unit": "USD", "multiplier": 10},
    "SUGAR":    {"cftc_code": "080732", "price_ric": "SBc1", "lot_size": 112000,"unit": "USc", "multiplier": 1120},
    "COFFEE":   {"cftc_code": "083731", "price_ric": "KCc2", "lot_size": 37500, "unit": "USc", "multiplier": 375},
}

# ── Target weight — per-CALENDAR-YEAR now, not a single constant ────────────
# Each year = 60% S&P GSCI RPDW + 40% Bloomberg BCOM Target Weight, each
# re-weighted to sum to 100% within just this 13-commodity ag/livestock
# subset (GSCI and BCOM each cover ~24 commodities total incl. energy/other
# metals — irrelevant here). Stored as the two RAW (un-blended) per-index
# tables, NOT pre-blended into one "Target Weight Pct" — the blend ratio
# (default 60% GSCI / 40% BCOM) is applied dynamically in the dashboard
# (a sidebar slider), not baked in here, so it can be changed live without
# a re-ingest. Two commodities only exist on one side: GSCI has no Soybean
# Meal/Oil sub-indices (GSCI-side = 0 every year); BCOM has no Feeder
# Cattle (BCOM-side = 0 every year). Cocoa was dropped from BCOM entirely
# from ~2005 until it was re-added in 2026 (BCOM-side = 0 for 2017-2025,
# non-zero only in 2026). Source tables: S&P GSCI RPDW + Bloomberg BCOM
# Target Weight annual announcement PDFs, 2017-2026 (see Index README for
# the full source-URL list). Verified: at the default 60/40 ratio, each
# year's blended 13 sum to exactly 100.000%, and the 2026 row matches the
# single constant this replaced (hand-derived from the same two tables) to
# within 0.6pp on every commodity — consistent with source-rounding noise.
#
# Dates before 2017 (our history starts 2016) use the 2017 weights (no
# earlier GSCI/BCOM table available — see README 'Gaps'); dates from 2027
# onward use the 2026 weights until a new year's row is added here at the
# next January rebalance.
GSCI_WEIGHT_BY_YEAR = {
    2017: {"SRW": 12.8104, "HRW": 3.6657, "CORN": 19.0382, "SOYBEAN": 13.7564, "BEAN OIL": 0.0, "MEAL": 0.0, "COTTON": 6.2278, "HOG": 8.8687, "LIVE": 16.0820, "FEEDER": 4.5329, "COCOA": 1.4584, "SUGAR": 9.5782, "COFFEE": 3.9811},
    2018: {"SRW": 11.7508, "HRW": 4.3488, "CORN": 19.3118, "SOYBEAN": 14.2065, "BEAN OIL": 0.0, "MEAL": 0.0, "COTTON": 6.1799, "HOG": 8.5968, "LIVE": 15.7582, "FEEDER": 4.8570, "COCOA": 1.4160, "SUGAR": 9.6481, "COFFEE": 3.9260},
    2019: {"SRW": 12.5504, "HRW": 5.1987, "CORN": 19.7661, "SOYBEAN": 14.2456, "BEAN OIL": 0.0, "MEAL": 0.0, "COTTON": 6.3727, "HOG": 8.6434, "LIVE": 15.7685, "FEEDER": 5.7472, "COCOA": 1.4413, "SUGAR": 6.9936, "COFFEE": 3.2724},
    2020: {"SRW": 12.3223, "HRW": 5.4199, "CORN": 21.1955, "SOYBEAN": 13.4244, "BEAN OIL": 0.0, "MEAL": 0.0, "COTTON": 5.4458, "HOG": 8.8732, "LIVE": 16.8388, "FEEDER": 5.6230, "COCOA": 1.4782, "SUGAR": 6.5696, "COFFEE": 2.8094},
    2021: {"SRW": 13.6967, "HRW": 5.4354, "CORN": 21.0710, "SOYBEAN": 14.5360, "BEAN OIL": 0.0, "MEAL": 0.0, "COTTON": 4.6621, "HOG": 7.8214, "LIVE": 16.3502, "FEEDER": 5.1129, "COCOA": 1.6493, "SUGAR": 6.6046, "COFFEE": 3.0604},
    2022: {"SRW": 13.0733, "HRW": 5.0210, "CORN": 23.4960, "SOYBEAN": 16.6756, "BEAN OIL": 0.0, "MEAL": 0.0, "COTTON": 4.5110, "HOG": 8.4653, "LIVE": 13.5007, "FEEDER": 4.4751, "COCOA": 1.2786, "SUGAR": 6.5115, "COFFEE": 2.9918},
    2023: {"SRW": 13.8728, "HRW": 6.3093, "CORN": 23.7352, "SOYBEAN": 15.1229, "BEAN OIL": 0.0, "MEAL": 0.0, "COTTON": 5.3108, "HOG": 7.6600, "LIVE": 12.5052, "FEEDER": 4.4047, "COCOA": 1.0697, "SUGAR": 6.0869, "COFFEE": 3.9223},
    2024: {"SRW": 12.0206, "HRW": 5.4859, "CORN": 22.7199, "SOYBEAN": 15.0153, "BEAN OIL": 0.0, "MEAL": 0.0, "COTTON": 3.8428, "HOG": 7.9696, "LIVE": 15.1478, "FEEDER": 6.1485, "COCOA": 1.2683, "SUGAR": 7.1480, "COFFEE": 3.2333},
    2025: {"SRW": 9.6238, "HRW": 4.8388, "CORN": 17.5321, "SOYBEAN": 13.4895, "BEAN OIL": 0.0, "MEAL": 0.0, "COTTON": 3.9465, "HOG": 9.5123, "LIVE": 17.9745, "FEEDER": 8.2968, "COCOA": 3.1541, "SUGAR": 7.6467, "COFFEE": 3.9849},
    2026: {"SRW": 8.4211, "HRW": 4.4038, "CORN": 16.5656, "SOYBEAN": 11.0024, "BEAN OIL": 0.0, "MEAL": 0.0, "COTTON": 3.1096, "HOG": 11.7399, "LIVE": 18.4448, "FEEDER": 9.4954, "COCOA": 4.1769, "SUGAR": 6.0845, "COFFEE": 6.5560},
}
BCOM_WEIGHT_BY_YEAR = {
    2017: {"SRW": 9.0254, "HRW": 3.2189, "CORN": 20.1622, "SOYBEAN": 15.8876, "BEAN OIL": 7.6377, "MEAL": 7.8962, "COTTON": 3.9399, "HOG": 5.6976, "LIVE": 10.8212, "FEEDER": 0.0, "COCOA": 0.0, "SUGAR": 9.2458, "COFFEE": 6.4677},
    2018: {"SRW": 8.9469, "HRW": 3.5826, "CORN": 16.8396, "SOYBEAN": 16.3619, "BEAN OIL": 7.5441, "MEAL": 8.3347, "COTTON": 3.9917, "HOG": 5.6992, "LIVE": 11.8377, "FEEDER": 0.0, "COCOA": 0.0, "SUGAR": 9.7101, "COFFEE": 7.1515},
    2019: {"SRW": 8.7507, "HRW": 3.6062, "CORN": 16.4200, "SOYBEAN": 16.7907, "BEAN OIL": 8.6503, "MEAL": 9.5951, "COTTON": 3.9545, "HOG": 5.1529, "LIVE": 11.4009, "FEEDER": 0.0, "COCOA": 0.0, "SUGAR": 8.7730, "COFFEE": 6.9058},
    2020: {"SRW": 8.6411, "HRW": 4.2211, "CORN": 16.5691, "SOYBEAN": 16.0124, "BEAN OIL": 8.2349, "MEAL": 9.3597, "COTTON": 4.2382, "HOG": 5.0506, "LIVE": 11.4192, "FEEDER": 0.0, "COCOA": 0.0, "SUGAR": 8.5502, "COFFEE": 7.7037},
    2021: {"SRW": 8.1355, "HRW": 4.4301, "CORN": 15.7549, "SOYBEAN": 16.4035, "BEAN OIL": 9.0125, "MEAL": 10.1489, "COTTON": 4.2609, "HOG": 4.8672, "LIVE": 10.8454, "FEEDER": 0.0, "COCOA": 0.0, "SUGAR": 8.4231, "COFFEE": 7.7181},
    2022: {"SRW": 8.1438, "HRW": 4.7615, "CORN": 15.9957, "SOYBEAN": 16.5651, "BEAN OIL": 9.0766, "MEAL": 10.0724, "COTTON": 4.3008, "HOG": 5.0219, "LIVE": 10.2469, "FEEDER": 0.0, "COCOA": 0.0, "SUGAR": 7.9950, "COFFEE": 7.8204},
    2023: {"SRW": 8.0581, "HRW": 5.0587, "CORN": 15.9000, "SOYBEAN": 16.6816, "BEAN OIL": 9.4165, "MEAL": 10.1751, "COTTON": 4.4963, "HOG": 5.0270, "LIVE": 9.5723, "FEEDER": 0.0, "COCOA": 0.0, "SUGAR": 7.3717, "COFFEE": 8.2427},
    2024: {"SRW": 7.8947, "HRW": 5.0960, "CORN": 15.8622, "SOYBEAN": 16.5485, "BEAN OIL": 9.3823, "MEAL": 9.9174, "COTTON": 4.3984, "HOG": 4.9951, "LIVE": 9.7072, "FEEDER": 0.0, "COCOA": 0.0, "SUGAR": 7.8666, "COFFEE": 8.3317},
    2025: {"SRW": 7.8285, "HRW": 5.1045, "CORN": 15.5850, "SOYBEAN": 16.5327, "BEAN OIL": 9.3222, "MEAL": 9.7794, "COTTON": 4.4533, "HOG": 4.8052, "LIVE": 9.9540, "FEEDER": 0.0, "COCOA": 0.0, "SUGAR": 8.3024, "COFFEE": 8.3329},
    2026: {"SRW": 7.5685, "HRW": 4.9798, "CORN": 15.3705, "SOYBEAN": 14.8895, "BEAN OIL": 7.8521, "MEAL": 8.1552, "COTTON": 4.4349, "HOG": 4.9465, "LIVE": 10.7271, "FEEDER": 0.0, "COCOA": 4.7657, "SUGAR": 8.2052, "COFFEE": 8.1051},
}
_TW_MIN_YEAR, _TW_MAX_YEAR = min(GSCI_WEIGHT_BY_YEAR), max(GSCI_WEIGHT_BY_YEAR)

def _weight_pct_for(table: dict, commodity: str, dates: pd.Series) -> pd.Series:
    """Per-row weight looked up by each date's calendar year, clamped to
    the years we actually have a table for."""
    years = dates.dt.year.clip(_TW_MIN_YEAR, _TW_MAX_YEAR)
    return years.map(lambda y: table[y][commodity])

def gsci_weight_pct_for(commodity: str, dates: pd.Series) -> pd.Series:
    return _weight_pct_for(GSCI_WEIGHT_BY_YEAR, commodity, dates)

def bcom_weight_pct_for(commodity: str, dates: pd.Series) -> pd.Series:
    return _weight_pct_for(BCOM_WEIGHT_BY_YEAR, commodity, dates)

FETCH_RETRIES = 3
FETCH_BACKOFF = 5

# ── Volatility-only price source overrides ──────────────────────────────────
# The weekly "Price" column above (price_ric, e.g. KCc2) stays UNCHANGED —
# it drives $ notional (Nominal Net USD = lots x Price x Multiplier) and must
# stay a real, correctly-denominated futures price. But price_ric is a fixed
# "always N-months-out" continuation, which back-tested (2026-08-28) as
# understating realized vol vs. a roll-managed series by ~12% on average for
# Coffee (0.0206 vs 0.0231 daily-return std, 2020-2026) — presumably because
# a fixed far-dated continuation is systematically calmer than what an
# index fund's actual roll-managed exposure realizes. Since vol only needs
# %-returns (unit-agnostic), it's safe to source it from a DIFFERENT series
# than the $ Price column without creating a Price/Vol mismatch:
#   - The 4 ICE-softs also covered by our OWN Rollex builder (roll-adjusted,
#     already verified near-identical vol to GSCI's own sub-index in the
#     same backtest) — read locally from the Interim_Migration/Rollex
#     project, whose own LSEG automator keeps it current daily (verified
#     2026-08-28: through the SAME day as this ingest ran). NOTE: there is
#     also a legacy, no-longer-updated Rollex build under ICEBREAKER/Rollex
#     (~1 month stale as of this check) — do NOT point at that one.
#   - The other 9 (no Rollex equivalent) — the S&P GSCI single-commodity
#     sub-index RIC instead (verified live 2026-08-28, all 9 accessible).
ROLLEX_DIR = Path(r"C:\Users\virat.arya\ETG\SoftsDatabase - Documents\Database\Hardmine\Interim_Migration\Rollex\Database")
ROLLEX_VOL_SOURCE = {
    "COTTON": "rollex_CT.parquet",
    "COCOA":  "rollex_CC.parquet",
    "SUGAR":  "rollex_SB.parquet",
    "COFFEE": "rollex_KC.parquet",
}
GSCI_VOL_SOURCE = {
    "SRW":      ".SPGSWHP",
    "HRW":      ".SPGSKWP",
    "CORN":     ".SPGSCNP",
    "SOYBEAN":  ".SPGSSOP",
    "BEAN OIL": ".SPGSBOP",
    "MEAL":     ".SPGSSMP",
    "HOG":      ".SPGSLHP",
    "LIVE":     ".SPGSLCP",
    "FEEDER":   ".SPGSFCP",
}


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


def fetch_vol_source_daily(ld, name: str, start: str, end: str) -> pd.Series | None:
    """Daily series used ONLY to compute realized volatility (see the
    ROLLEX_VOL_SOURCE / GSCI_VOL_SOURCE comment above) — deliberately not
    the same series as the weekly $ Price column."""
    if name in ROLLEX_VOL_SOURCE:
        p = ROLLEX_DIR / ROLLEX_VOL_SOURCE[name]
        if not p.exists():
            log.warning("  %s: Rollex file not found (%s) — vol source skipped this run", name, p)
            return None
        rx = pd.read_parquet(p, columns=["rollex_px"])
        rx.index = pd.to_datetime(rx.index)
        s = rx.loc[(rx.index >= pd.Timestamp(start)) & (rx.index <= pd.Timestamp(end)), "rollex_px"]
        s.index.name = "Date"
        return s if not s.empty else None
    if name in GSCI_VOL_SOURCE:
        return _history(ld, GSCI_VOL_SOURCE[name], "TRDPRC_1", start, end, f"{name} GSCI vol source")
    log.warning("  %s: no vol-source mapping — should not happen (13 commodities all mapped)", name)
    return None


def fetch_commodity(ld, name: str, cfg: dict, start: str, end: str) -> tuple[pd.DataFrame, pd.Series | None]:
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
        return pd.DataFrame(), px_s

    # px_s is a full DAILY series (not just the CFTC report's weekly dates).
    # If the exact report date (usually Tuesday) has no print, pull the
    # closest trading day within 3 days (Monday/Wednesday) instead of
    # reusing a stale price from a prior week.
    if px_s is not None:
        px_s = px_s.sort_index()
        px_aligned = px_s.reindex(long_s.index, method="nearest", tolerance=pd.Timedelta(days=3))
    else:
        px_aligned = None

    df = pd.concat({"Index Long": long_s, "Index Short": short_s,
                    "Total OI": oi_s, "Price": px_aligned}, axis=1)
    df = df.dropna(subset=["Index Long", "Index Short"], how="all")
    n_missing_px = df["Price"].isna().sum()
    if n_missing_px:
        log.warning("  %s: %d/%d weeks still had no price within 3 days — forward-filling", name, n_missing_px, len(df))
        df["Price"] = df["Price"].ffill()
    df["Index Net"] = df["Index Long"] - df["Index Short"]
    df["Nominal Net USD"] = df["Index Net"] * df["Price"] * cfg["multiplier"]
    df["Net Pct OI"] = (df["Index Net"] / df["Total OI"] * 100).where(df["Total OI"] > 0)

    df = df.reset_index()
    df.insert(0, "Commodity", name)
    df["Lot Size"] = cfg["lot_size"]
    df["Unit"] = cfg["unit"]
    df["Multiplier"] = cfg["multiplier"]
    df["GSCI Weight Pct"] = gsci_weight_pct_for(name, df["Date"])
    df["BCOM Weight Pct"] = bcom_weight_pct_for(name, df["Date"])
    log.info("  %s -> %d rows, %s to %s", name, len(df),
             df["Date"].min().date() if len(df) else "—", df["Date"].max().date() if len(df) else "—")
    return df, px_s


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
    daily_frames = []
    for name, cfg in COMMODITIES.items():
        log.info("Fetching %s (CFTC %s)...", name, cfg["cftc_code"])
        weekly_df, _ = fetch_commodity(ld, name, cfg, start, end)
        frames.append(weekly_df)
        vol_px = fetch_vol_source_daily(ld, name, start, end)
        time.sleep(1)
        if vol_px is not None and not vol_px.empty:
            d = vol_px.rename("Price").reset_index()
            d.insert(0, "Commodity", name)
            daily_frames.append(d)

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

    # ── Daily price history — used ONLY to compute realized volatility for
    #    the "Index in VaR" tab (same Price*Multiplier*Vol*Z methodology as
    #    COT_ALL's Spec VaR). Per-commodity vol source is Rollex (4 ICE
    #    softs) or GSCI sub-index (other 9) — see ROLLEX_VOL_SOURCE /
    #    GSCI_VOL_SOURCE above; NOT the same series as the weekly $ Price
    #    column, which stays on price_ric. ─────────────────────────────────
    if daily_frames:
        new_daily = pd.concat(daily_frames, ignore_index=True)
        if DAILY_PRICE_FILE.exists() and not full:
            old_daily = pd.read_parquet(DAILY_PRICE_FILE)
            combined_daily = pd.concat([old_daily, new_daily], ignore_index=True)
            combined_daily = combined_daily.drop_duplicates(subset=["Commodity", "Date"], keep="last")
        else:
            combined_daily = new_daily
        combined_daily = combined_daily.sort_values(["Commodity", "Date"]).reset_index(drop=True)
        combined_daily.to_parquet(DAILY_PRICE_FILE, index=False)
        log.info("Saved -> %s | %d rows | %d commodities", DAILY_PRICE_FILE.name, len(combined_daily),
                 combined_daily["Commodity"].nunique())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Full history from 2016-01-01")
    args = parser.parse_args()
    main(full=args.full)
