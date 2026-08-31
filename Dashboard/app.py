"""
Index — CFTC "Index Traders" Positioning Dashboard
====================================================
Tracks passive commodity-index-fund positioning (the CFTC CIT report's
"Index Traders" category — GSCI/BCOM trackers, not discretionary Managed
Money) across 13 ag commodities, converts lots to real dollar notional, and
compares each commodity's share of that pool to its theoretical GSCI/BCOM
target weight. Since index funds only rebalance back to target once a year
(January), the gap between actual and target is a structural, largely
mechanical flow — not a discretionary bet.

Data: Database/index_positioning.parquet, built by Code/ingest_lseg.py.
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from pathlib import Path

st.set_page_config(page_title="Index Positioning", layout="wide", initial_sidebar_state="expanded")

DB = Path(__file__).resolve().parents[1] / "Database"

NAVY, BLACK, GREEN, RED, GREY, AMBER = "#0a2463", "#1d1d1f", "#16a34a", "#dc2626", "#9ca3af", "#d97706"
st.markdown("""<style>
  [data-testid="stAppViewContainer"],[data-testid="stMain"],.main{background:#fafafa!important;color:#1d1d1f!important}
  [data-testid="stHeader"]{background:transparent!important}
  .block-container{padding-top:2rem!important;padding-bottom:1.5rem;max-width:1440px}
  hr{border:none!important;border-top:1px solid #e8e8ed!important;margin:.4rem 0!important}
  h1,h2,h3{color:#1d1d1f!important;font-weight:500!important}
</style>""", unsafe_allow_html=True)

_D = dict(template="plotly_white", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
          font=dict(family="-apple-system,Helvetica Neue,sans-serif", color=BLACK, size=10))

def lbl(text):
    return (f"<div style='background:{NAVY};padding:5px 13px;border-radius:5px;"
            f"margin-bottom:8px'><span style='font-size:.78rem;font-weight:500;"
            f"letter-spacing:.07em;text-transform:uppercase;color:#dde4f0'>{text}</span></div>")

def base_fig(height=380, yaxis_title=None):
    fig = go.Figure()
    fig.update_layout(height=height,
                       yaxis=dict(title=yaxis_title, gridcolor="#f0f0f0"),
                       legend=dict(orientation="h", y=1.05, font=dict(size=9)),
                       margin=dict(t=10, b=10, l=4, r=4), **_D)
    return fig

GROUPS = {
    "Softs":     ["COTTON", "COCOA", "SUGAR", "COFFEE"],
    "Grains":    ["SRW", "HRW", "CORN"],
    "Oilseeds":  ["SOYBEAN", "BEAN OIL", "MEAL"],
    "Livestock": ["HOG", "LIVE", "FEEDER"],
}
GROUP_OF = {c: g for g, comms in GROUPS.items() for c in comms}
COLORS = {
    "COTTON": "#0a2463", "COCOA": "#e8a020", "SUGAR": "#1a6b1a", "COFFEE": "#8b1a00",
    "SRW": "#4a7fb5", "HRW": "#7ec8c0", "CORN": "#c9a020", "SOYBEAN": "#6b8e23",
    "BEAN OIL": "#a0522d", "MEAL": "#8b6914",
    "HOG": "#c0392b", "LIVE": "#7b2d8b", "FEEDER": "#d35400",
}

@st.cache_data(ttl=1800)
def load_data() -> pd.DataFrame:
    df = pd.read_parquet(DB / "index_positioning.parquet")
    df["Date"] = pd.to_datetime(df["Date"])
    return df.sort_values(["Commodity", "Date"])

VAR_Z = 2.3263  # 99% one-tailed confidence — same constant COT_ALL's Spec VaR uses

@st.cache_data(ttl=1800)
def load_daily_prices() -> pd.DataFrame:
    d = pd.read_parquet(DB / "daily_prices.parquet")
    d["Date"] = pd.to_datetime(d["Date"])
    return d.sort_values(["Commodity", "Date"])

@st.cache_data(ttl=1800)
def compute_daily_vol(daily_df: pd.DataFrame) -> pd.DataFrame:
    """Rolling 20/60/120-day realized volatility of daily returns, per
    commodity — same methodology as COT_ALL's Spec VaR (_build_var_df in
    cot_app.py). Deliberately NOT sourced from the same price_ric series as
    the $ Price column: ingest_lseg.py feeds this from Rollex (the 4 ICE
    softs — the actively-maintained Interim_Migration/Rollex build) or the
    S&P GSCI single-commodity sub-index (the other 9) instead — a fixed-
    maturity continuation like price_ric back-tested (2026-08-28) as
    understating realized vol by ~12% on average vs. either of those roll-
    managed series, since it's structurally calmer than what an index
    fund's actual rolled exposure realizes. Using a different series than
    Price for this is fine since vol only needs %-returns (unit-agnostic)
    — no $-level mismatch. Computed on each commodity's OWN native trading-
    day sequence (not pivoted to a shared calendar) — pivoting+ffill would
    insert an artificial 0%-return day into a commodity's series on any
    date another commodity traded but it didn't, understating its true
    volatility."""
    frames = []
    for c, g in daily_df.groupby("Commodity"):
        g = g.sort_values("Date")
        ret = g["Price"].pct_change()
        d = pd.DataFrame({"Date": g["Date"].values, "Commodity": c})
        for w in (20, 60, 120):
            d[f"vol_{w}"] = ret.rolling(w, min_periods=max(5, w // 4)).std().values
        frames.append(d.dropna(subset=["vol_20", "vol_60", "vol_120"], how="all"))
    return pd.concat(frames, ignore_index=True)

@st.cache_data(ttl=1800)
def compute_index_var(df: pd.DataFrame, vol_df: pd.DataFrame, all_commodities: list,
                       vol_window: int) -> pd.DataFrame:
    """Index Traders' Net/Long/Short lots converted into 1-day 99% VaR $ —
    Price x Multiplier x realized-Vol(window) x Z — the exact same formula
    COT_ALL's "Specs in VaR" tab uses for Managed Money, so Index money's
    risk footprint can be compared on the same scale as Spec money's,
    rather than only by raw lots or notional $ (which ignore volatility)."""
    vcol = f"vol_{vol_window}"
    frames = []
    for c in all_commodities:
        dc = df[df["Commodity"] == c].sort_values("Date")
        vc = vol_df[vol_df["Commodity"] == c][["Date", vcol]].dropna().sort_values("Date")
        if dc.empty or vc.empty:
            continue
        m = pd.merge_asof(dc, vc, on="Date", direction="backward").dropna(subset=[vcol])
        if m.empty:
            continue
        vpl = m["Price"] * m["Multiplier"] * m[vcol] * VAR_Z
        m = m.assign(**{
            "VaR Per Lot": vpl,
            "Net VaR USD": m["Index Net"] * vpl,
            "Long VaR USD": m["Index Long"] * vpl,
            "Short VaR USD": m["Index Short"] * vpl,
        })
        frames.append(m[["Commodity", "Date", "Price", vcol, "VaR Per Lot",
                         "Net VaR USD", "Long VaR USD", "Short VaR USD"]])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

@st.cache_data(ttl=1800)
def compute_deviation_var(dev_df: pd.DataFrame, var_df: pd.DataFrame) -> pd.DataFrame:
    """The over/under-vs-target deviation, in VaR $ terms — same VaR Per Lot
    (Price x Multiplier x Vol x Z) the Index-in-VaR tab uses for actual Net
    lots, applied instead to Deviation Lots (the target-weight gap). Puts
    the raw lot deviation on the same $-risk scale across commodities of
    very different volatility, instead of comparing lots 1-for-1."""
    m = dev_df.merge(var_df[["Commodity", "Date", "Price", "VaR Per Lot"] +
                            [c for c in var_df.columns if c.startswith("vol_")]],
                     on=["Commodity", "Date"], how="inner")
    m["Deviation VaR USD"] = m["Deviation Lots"] * m["VaR Per Lot"]
    return m

@st.cache_data(ttl=1800)
def compute_deviation(df: pd.DataFrame, pool: pd.DataFrame, total_pool: pd.Series,
                       all_commodities: list) -> pd.DataFrame:
    """Verified against the source workbook's RECAP sheet — target weight is
    a per-CALENDAR-YEAR value (re-derived at each January GSCI/BCOM
    rebalance — see ingest_lseg.py's TARGET_WEIGHT_BY_YEAR — NOT a single
    constant held flat across all years), and at every single date:
      Deviation %   = Actual Weight % - Target Weight %          (RECAP: CL = BQ-BQ$2)
      Deviation USD = Deviation % / 100 * Total Pool              (RECAP: CY = CL*BP)
      Deviation Lots= Deviation USD / (Price * Multiplier)        (RECAP: DL = CL*BP/O)
      Deviation %OI = Deviation Lots / Total OI                   (RECAP: DY = DL/AB)
    No annual anchoring/hold-static step — this replaces an earlier, incorrect
    from-scratch reconstruction that invented a static Jan-reference lot count.
    """
    cols = [c for c in all_commodities if c in pool.columns]
    price = df.pivot_table(index="Date", columns="Commodity", values="Price", aggfunc="last")[cols]
    oi = df.pivot_table(index="Date", columns="Commodity", values="Total OI", aggfunc="last")[cols]
    target_pivot = df.pivot_table(index="Date", columns="Commodity", values="Target Weight Pct", aggfunc="last")[cols]
    ref = df.drop_duplicates("Commodity").set_index("Commodity")
    multiplier = ref["Multiplier"].reindex(cols)

    actual_pct = pool[cols].div(total_pool, axis=0) * 100
    dev_pct = actual_pct.sub(target_pivot)
    dev_usd = dev_pct.div(100).mul(total_pool, axis=0)
    dev_lots = dev_usd / price.mul(multiplier, axis=1)
    dev_pct_oi = dev_lots.div(oi) * 100

    frames = []
    for c in cols:
        frames.append(pd.DataFrame({
            "Commodity": c, "Date": dev_pct.index,
            "Deviation Pct": dev_pct[c].values,
            "Deviation USD": dev_usd[c].values,
            "Deviation Lots": dev_lots[c].values,
            "Deviation Pct OI": dev_pct_oi[c].values,
        }))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

@st.cache_data(ttl=1800)
def compute_weekly_deviation_pct(df: pd.DataFrame, pool: pd.DataFrame, total_pool: pd.Series,
                                  all_commodities: list) -> pd.DataFrame:
    """Actual $-share of the pool minus each commodity's per-calendar-year
    GSCI/BCOM target weight, at every date — the simple %-of-weight
    deviation (distinct from the static Jan-reference *lots* deviation in
    compute_deviation())."""
    actual_pct = pool.div(total_pool, axis=0) * 100
    cols = [c for c in all_commodities if c in actual_pct.columns]
    target_pivot = df.pivot_table(index="Date", columns="Commodity", values="Target Weight Pct", aggfunc="last")[cols]
    dev = actual_pct[cols].sub(target_pivot)
    return dev

@st.cache_data(ttl=1800)
def compute_nominal_attribution(df: pd.DataFrame, all_commodities: list, weeks_back: int) -> pd.DataFrame:
    """Splits the change in Nominal Net USD over `weeks_back` weeks into a
    position-driven part (lots changed) and a price-driven part (price
    moved), using the average/midpoint (Bennet) decomposition — the two
    parts sum EXACTLY to the total change, with no leftover interaction
    term: Net Effect = M*(Net_t-Net_0)*(Price_t+Price_0)/2,
    Price Effect = M*(Price_t-Price_0)*(Net_t+Net_0)/2."""
    rows = []
    for c in all_commodities:
        d = df[df["Commodity"] == c].sort_values("Date")
        if len(d) <= weeks_back:
            continue
        t, t0 = d.iloc[-1], d.iloc[-1 - weeks_back]
        m = t["Multiplier"]
        net_t, net_0 = t["Index Net"], t0["Index Net"]
        px_t, px_0 = t["Price"], t0["Price"]
        if pd.isna(px_t) or pd.isna(px_0):
            continue
        net_effect = m * (net_t - net_0) * (px_t + px_0) / 2
        px_effect = m * (px_t - px_0) * (net_t + net_0) / 2
        total = net_effect + px_effect
        denom = abs(net_effect) + abs(px_effect)
        driver = "Position" if abs(net_effect) >= abs(px_effect) else "Price"
        driver_pct = (max(abs(net_effect), abs(px_effect)) / denom * 100) if denom else 50.0
        rows.append({
            "Commodity": c, "Nominal Net USD": t["Nominal Net USD"], "Total Change": total,
            "Net Lots Change": net_t - net_0, "Net Effect": net_effect,
            "Price Change": px_t - px_0, "Price Effect": px_effect,
            "Driver": driver, "Driver Pct": driver_pct,
        })
    return pd.DataFrame(rows)

def build_attribution_table_html(tbl: pd.DataFrame, group_of: dict, colors: dict) -> str:
    css = """<style>
      .idxattr-wrap{overflow-x:auto;border:1px solid #e5e7eb;border-radius:8px}
      table.idxattr{border-collapse:collapse;width:100%;font-size:.8rem;
        font-family:-apple-system,Helvetica Neue,sans-serif}
      table.idxattr th,table.idxattr td{padding:7px 14px;text-align:right;white-space:nowrap}
      table.idxattr th:first-child,table.idxattr td:first-child{text-align:left}
      table.idxattr thead th{background:#0a2463;color:#dde4f0;font-weight:500;letter-spacing:.03em;
        font-size:.68rem;text-transform:uppercase}
      table.idxattr tbody tr:nth-child(even) td{background-color:rgba(0,0,0,.02)}
      .idxattr-dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
      .idxattr-name{font-weight:600;color:#1d1d1f}
      .idxattr-badge{padding:2px 9px;border-radius:9px;font-size:.68rem;font-weight:600}
    </style>"""
    total_vmax = max(tbl["Total Change"].abs().max(), 1)
    net_vmax = max(tbl["Net Effect"].abs().max(), 1)
    px_vmax = max(tbl["Price Effect"].abs().max(), 1)
    rows = []
    for _, r in tbl.sort_values("Total Change", ascending=False).iterrows():
        dot = colors.get(r["Commodity"], "#999")
        tot_color = "#16a34a" if r["Total Change"] >= 0 else "#dc2626"
        net_color = "#16a34a" if r["Net Effect"] >= 0 else "#dc2626"
        px_color = "#16a34a" if r["Price Effect"] >= 0 else "#dc2626"
        badge_bg, badge_fg = ("#dbeafe", "#1e40af") if r["Driver"] == "Position" else ("#fce7d6", "#c2410c")
        lots_color = "#16a34a" if r["Net Lots Change"] >= 0 else "#dc2626"
        pxchg_color = "#16a34a" if r["Price Change"] >= 0 else "#dc2626"
        rows.append(
            "<tr>"
            f"<td><span class='idxattr-dot' style='background:{dot}'></span>"
            f"<span class='idxattr-name'>{r['Commodity']}</span></td>"
            f"<td>${r['Nominal Net USD']:,.0f}</td>"
            f"<td style='{_diverging_cell_style(r['Total Change'], total_vmax)}color:{tot_color};font-weight:600'>"
            f"{r['Total Change']:+,.0f}</td>"
            f"<td style='color:{lots_color}'>{r['Net Lots Change']:+,.0f}</td>"
            f"<td style='{_diverging_cell_style(r['Net Effect'], net_vmax)}color:{net_color}'>{r['Net Effect']:+,.0f}</td>"
            f"<td style='color:{pxchg_color}'>{r['Price Change']:+,.2f}</td>"
            f"<td style='{_diverging_cell_style(r['Price Effect'], px_vmax)}color:{px_color}'>{r['Price Effect']:+,.0f}</td>"
            f"<td><span class='idxattr-badge' style='background:{badge_bg};color:{badge_fg}'>"
            f"{r['Driver']} ({r['Driver Pct']:.0f}%)</span></td>"
            "</tr>"
        )
    header = ("<tr><th>Commodity</th><th>Nominal Net USD</th><th>Total Change ($)</th>"
              "<th>Net Lots Δ</th><th>Net Effect ($)</th><th>Price Δ</th><th>Price Effect ($)</th>"
              "<th>Primary Driver</th></tr>")
    return f"{css}<div class='idxattr-wrap'><table class='idxattr'><thead>{header}</thead><tbody>{''.join(rows)}</tbody></table></div>"

def _diverging_cell_style(v, vmax) -> str:
    if pd.isna(v) or not vmax:
        return ""
    half = min(abs(v) / vmax, 1.0) * 50
    if v >= 0:
        lo, hi, color = 50, 50 + half, "rgba(22,163,74,0.28)"
    else:
        lo, hi, color = 50 - half, 50, "rgba(220,38,38,0.28)"
    return f"background:linear-gradient(to right, transparent {lo:.1f}%, {color} {lo:.1f}%, {color} {hi:.1f}%, transparent {hi:.1f}%);"

def build_weekly_deviation_html(dev_tail: pd.DataFrame, commodities: list, group_of: dict) -> str:
    css = """<style>
      .idxdev-wrap{overflow:auto;max-height:640px;border:1px solid #e5e7eb;border-radius:6px}
      table.idxdev{border-collapse:collapse;width:100%;font-size:.72rem;
        font-family:-apple-system,Helvetica Neue,sans-serif;white-space:nowrap}
      table.idxdev th,table.idxdev td{padding:3px 8px;text-align:center}
      table.idxdev thead th{position:sticky;top:0;background:#0a2463;color:#dde4f0;font-weight:500;
        letter-spacing:.03em;z-index:2;font-size:.66rem;text-transform:uppercase}
      table.idxdev td.date-cell{position:sticky;left:0;background:#fafafa;font-weight:500;
        color:#1d1d1f;text-align:left;z-index:1;box-shadow:1px 0 0 #e5e7eb}
      table.idxdev th.date-head{position:sticky;left:0;top:0;z-index:3;text-align:left;background:#0a2463}
      table.idxdev tbody tr:nth-child(even) td:not(.date-cell){background-color:rgba(0,0,0,.015)}
      table.idxdev td.grp-start,table.idxdev th.grp-start{box-shadow:inset 2px 0 0 #c7cdd6}
      table.idxdev td.date-cell.grp-start{box-shadow:1px 0 0 #e5e7eb}
    </style>"""
    header_cells, prev_grp = ["<th class='date-head'>Date</th>"], None
    for c in commodities:
        grp = group_of.get(c, "")
        cls = " class='grp-start'" if prev_grp is not None and grp != prev_grp else ""
        header_cells.append(f"<th{cls}>{c}</th>")
        prev_grp = grp
    vmax = {c: max(dev_tail[c].abs().max(), 0.01) if c in dev_tail.columns else 0.01 for c in commodities}

    body_rows = []
    for dt, row in dev_tail.sort_index(ascending=False).iterrows():
        cells, prev_grp = [f"<td class='date-cell'>{dt.strftime('%d %b %Y')}</td>"], None
        for c in commodities:
            grp = group_of.get(c, "")
            cls = "grp-start" if prev_grp is not None and grp != prev_grp else ""
            v = row.get(c, np.nan)
            if pd.isna(v):
                cells.append(f"<td class='{cls}'></td>")
            else:
                style = _diverging_cell_style(v, vmax[c])
                color = "#16a34a" if v >= 0 else "#dc2626"
                cells.append(f"<td class='{cls}' style='{style}color:{color};font-weight:600'>{v:+.2f}</td>")
            prev_grp = grp
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    return (f"{css}<div class='idxdev-wrap'><table class='idxdev'><thead><tr>"
            f"{''.join(header_cells)}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>")

def _bar_style(v, vmax, color="rgba(10,36,99,.18)") -> str:
    if pd.isna(v) or not vmax:
        return ""
    pct = min(abs(v) / vmax, 1.0) * 100
    return f"background:linear-gradient(to right, {color} {pct:.1f}%, transparent {pct:.1f}%);"

GROUP_BADGE = {
    "Softs":     ("#fdf0e0", "#b45309"),
    "Grains":    ("#eaf3e0", "#3f6212"),
    "Oilseeds":  ("#fef3c7", "#92722a"),
    "Livestock": ("#f0e6f5", "#7b2d8b"),
}

def build_snapshot_table_html(snap: pd.DataFrame, group_of: dict, colors: dict, group_order: list) -> str:
    snap = snap.copy()
    snap["_grp_rank"] = snap["Commodity"].map(lambda c: group_order.index(group_of.get(c, "")) if group_of.get(c, "") in group_order else 99)
    snap = snap.sort_values(["_grp_rank", "Target Weight Pct"], ascending=[True, False])

    css = """<style>
      .idxsnap-wrap{overflow-x:auto;border:1px solid #e5e7eb;border-radius:8px;
        box-shadow:0 1px 3px rgba(0,0,0,.04)}
      table.idxsnap{border-collapse:collapse;width:100%;font-size:.8rem;
        font-family:-apple-system,Helvetica Neue,sans-serif}
      table.idxsnap th,table.idxsnap td{padding:8px 14px;text-align:right;white-space:nowrap}
      table.idxsnap th:first-child,table.idxsnap td:first-child,
      table.idxsnap th:nth-child(2),table.idxsnap td:nth-child(2){text-align:left}
      table.idxsnap thead th{background:#0a2463;color:#dde4f0;font-weight:500;letter-spacing:.03em;
        font-size:.68rem;text-transform:uppercase}
      table.idxsnap tbody tr:nth-child(even) td{background-color:rgba(0,0,0,.02)}
      table.idxsnap tbody tr:hover td{background-color:rgba(10,36,99,.05)}
      .idxsnap-dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
      .idxsnap-name{font-weight:600;color:#1d1d1f}
      .idxsnap-badge{padding:2px 9px;border-radius:9px;font-size:.68rem;font-weight:600}
    </style>"""

    aw_vmax = max(snap["Actual Weight Pct"].abs().max(), 0.01)
    oi_vmax = max(snap["Net Pct OI"].abs().max(), 0.01)
    dev_vmax = max(snap["Deviation Pp"].abs().max(), 0.01)
    nom_vmax = max(snap["Nominal Net USD"].abs().max(), 1)
    devlots_vmax = max(snap["Deviation Lots"].abs().max(), 1)
    devoi_vmax = max(snap["Deviation Pct OI"].abs().max(), 0.01)

    rows = []
    for _, r in snap.iterrows():
        grp = group_of.get(r["Commodity"], "")
        bg, fg = GROUP_BADGE.get(grp, ("#eee", "#555"))
        dot = colors.get(r["Commodity"], "#999")
        dev_color = "#16a34a" if r["Deviation Pp"] >= 0 else "#dc2626"
        arrow = "&#9650;" if r["Deviation Pp"] >= 0 else "&#9660;"
        nom_color = "#16a34a" if r["Nominal Net USD"] >= 0 else "#dc2626"
        lots_color = "#16a34a" if r["Index Net"] >= 0 else "#dc2626"
        devlots_color = "#16a34a" if r["Deviation Lots"] >= 0 else "#dc2626"
        devoi_color = "#16a34a" if r["Deviation Pct OI"] >= 0 else "#dc2626"
        rows.append(
            "<tr>"
            f"<td><span class='idxsnap-dot' style='background:{dot}'></span>"
            f"<span class='idxsnap-name'>{r['Commodity']}</span></td>"
            f"<td><span class='idxsnap-badge' style='background:{bg};color:{fg}'>{grp}</span></td>"
            f"<td>{r['Target Weight Pct']:.2f}%</td>"
            f"<td style='{_bar_style(r['Actual Weight Pct'], aw_vmax)}'>{r['Actual Weight Pct']:.2f}%</td>"
            f"<td style='{_diverging_cell_style(r['Deviation Pp'], dev_vmax)}color:{dev_color};font-weight:600'>"
            f"{arrow} {r['Deviation Pp']:+.2f}pp</td>"
            f"<td style='{_diverging_cell_style(r['Deviation Lots'], devlots_vmax)}color:{devlots_color};font-weight:600'>"
            f"{r['Deviation Lots']:+,.0f}</td>"
            f"<td style='{_diverging_cell_style(r['Deviation Pct OI'], devoi_vmax)}color:{devoi_color};font-weight:600'>"
            f"{r['Deviation Pct OI']:+.2f}%</td>"
            f"<td style='{_diverging_cell_style(r['Nominal Net USD'], nom_vmax)}color:{nom_color};font-weight:600'>"
            f"${r['Nominal Net USD']:,.0f}</td>"
            f"<td style='color:{lots_color}'>{r['Index Net']:+,.0f}</td>"
            f"<td style='{_bar_style(r['Net Pct OI'], oi_vmax, 'rgba(217,119,6,.20)')}'>{r['Net Pct OI']:+.1f}%</td>"
            "</tr>"
        )
    header = ("<tr><th>Commodity</th><th>Group</th><th>Target %</th><th>Actual %</th>"
              "<th>Deviation (pp)</th><th>Deviation (lots)</th><th>Deviation (% of OI)</th>"
              "<th>Nominal Net USD</th><th>Net Lots</th><th>Net % of OI</th></tr>")
    return f"{css}<div class='idxsnap-wrap'><table class='idxsnap'><thead>{header}</thead><tbody>{''.join(rows)}</tbody></table></div>"

def build_var_table_html(tbl: pd.DataFrame, group_of: dict, colors: dict, group_order: list, vol_window: int) -> str:
    tbl = tbl.copy()
    tbl["_grp_rank"] = tbl["Commodity"].map(lambda c: group_order.index(group_of.get(c, "")) if group_of.get(c, "") in group_order else 99)
    tbl = tbl.sort_values(["_grp_rank", "Net VaR USD"], ascending=[True, False])

    css = """<style>
      .idxvar-wrap{overflow-x:auto;border:1px solid #e5e7eb;border-radius:8px}
      table.idxvar{border-collapse:collapse;width:100%;font-size:.8rem;
        font-family:-apple-system,Helvetica Neue,sans-serif}
      table.idxvar th,table.idxvar td{padding:8px 14px;text-align:right;white-space:nowrap}
      table.idxvar th:first-child,table.idxvar td:first-child,
      table.idxvar th:nth-child(2),table.idxvar td:nth-child(2){text-align:left}
      table.idxvar thead th{background:#0a2463;color:#dde4f0;font-weight:500;letter-spacing:.03em;
        font-size:.68rem;text-transform:uppercase}
      table.idxvar tbody tr:nth-child(even) td{background-color:rgba(0,0,0,.02)}
      .idxvar-dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
      .idxvar-name{font-weight:600;color:#1d1d1f}
      .idxvar-badge{padding:2px 9px;border-radius:9px;font-size:.68rem;font-weight:600}
    </style>"""
    vcol = f"vol_{vol_window}"
    net_vmax = max(tbl["Net VaR USD"].abs().max(), 1)
    rows = []
    for _, r in tbl.iterrows():
        grp = group_of.get(r["Commodity"], "")
        bg, fg = GROUP_BADGE.get(grp, ("#eee", "#555"))
        dot = colors.get(r["Commodity"], "#999")
        net_color = "#16a34a" if r["Net VaR USD"] >= 0 else "#dc2626"
        rows.append(
            "<tr>"
            f"<td><span class='idxvar-dot' style='background:{dot}'></span>"
            f"<span class='idxvar-name'>{r['Commodity']}</span></td>"
            f"<td><span class='idxvar-badge' style='background:{bg};color:{fg}'>{grp}</span></td>"
            f"<td>${r['Price']:,.2f}</td>"
            f"<td>{r[vcol]*100:.2f}%</td>"
            f"<td>${r['VaR Per Lot']:,.0f}</td>"
            f"<td style='{_diverging_cell_style(r['Net VaR USD']/1e6, net_vmax/1e6)}color:{net_color};font-weight:600'>"
            f"${r['Net VaR USD']/1e6:+,.1f}M</td>"
            f"<td style='color:#16a34a'>${r['Long VaR USD']/1e6:,.1f}M</td>"
            f"<td style='color:#dc2626'>${r['Short VaR USD']/1e6:,.1f}M</td>"
            "</tr>"
        )
    header = (f"<tr><th>Commodity</th><th>Group</th><th>Price</th><th>{vol_window}D Vol</th>"
              "<th>VaR / Lot</th><th>Net VaR ($M)</th><th>Long VaR ($M)</th><th>Short VaR ($M)</th></tr>")
    return f"{css}<div class='idxvar-wrap'><table class='idxvar'><thead>{header}</thead><tbody>{''.join(rows)}</tbody></table></div>"

def build_deviation_var_table_html(tbl: pd.DataFrame, group_of: dict, colors: dict, group_order: list, vol_window: int) -> str:
    tbl = tbl.copy()
    tbl["_grp_rank"] = tbl["Commodity"].map(lambda c: group_order.index(group_of.get(c, "")) if group_of.get(c, "") in group_order else 99)
    tbl = tbl.sort_values(["_grp_rank", "Deviation VaR USD"], ascending=[True, False])

    css = """<style>
      .idxdevvar-wrap{overflow-x:auto;border:1px solid #e5e7eb;border-radius:8px}
      table.idxdevvar{border-collapse:collapse;width:100%;font-size:.8rem;
        font-family:-apple-system,Helvetica Neue,sans-serif}
      table.idxdevvar th,table.idxdevvar td{padding:8px 14px;text-align:right;white-space:nowrap}
      table.idxdevvar th:first-child,table.idxdevvar td:first-child,
      table.idxdevvar th:nth-child(2),table.idxdevvar td:nth-child(2){text-align:left}
      table.idxdevvar thead th{background:#0a2463;color:#dde4f0;font-weight:500;letter-spacing:.03em;
        font-size:.68rem;text-transform:uppercase}
      table.idxdevvar tbody tr:nth-child(even) td{background-color:rgba(0,0,0,.02)}
      .idxdevvar-dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
      .idxdevvar-name{font-weight:600;color:#1d1d1f}
      .idxdevvar-badge{padding:2px 9px;border-radius:9px;font-size:.68rem;font-weight:600}
    </style>"""
    vcol = f"vol_{vol_window}"
    devvar_vmax = max(tbl["Deviation VaR USD"].abs().max(), 1)
    rows = []
    for _, r in tbl.iterrows():
        grp = group_of.get(r["Commodity"], "")
        bg, fg = GROUP_BADGE.get(grp, ("#eee", "#555"))
        dot = colors.get(r["Commodity"], "#999")
        dev_color = "#16a34a" if r["Deviation Lots"] >= 0 else "#dc2626"
        devvar_color = "#16a34a" if r["Deviation VaR USD"] >= 0 else "#dc2626"
        rows.append(
            "<tr>"
            f"<td><span class='idxdevvar-dot' style='background:{dot}'></span>"
            f"<span class='idxdevvar-name'>{r['Commodity']}</span></td>"
            f"<td><span class='idxdevvar-badge' style='background:{bg};color:{fg}'>{grp}</span></td>"
            f"<td>${r['Price']:,.2f}</td>"
            f"<td>{r[vcol]*100:.2f}%</td>"
            f"<td style='color:{dev_color}'>{r['Deviation Lots']:+,.0f}</td>"
            f"<td style='{_diverging_cell_style(r['Deviation VaR USD']/1e6, devvar_vmax/1e6)}color:{devvar_color};font-weight:600'>"
            f"${r['Deviation VaR USD']/1e6:+,.1f}M</td>"
            "</tr>"
        )
    header = (f"<tr><th>Commodity</th><th>Group</th><th>Price</th><th>{vol_window}D Vol</th>"
              "<th>Deviation (lots)</th><th>Deviation VaR ($M)</th></tr>")
    return f"{css}<div class='idxdevvar-wrap'><table class='idxdevvar'><thead>{header}</thead><tbody>{''.join(rows)}</tbody></table></div>"

def build_target_weights_table_html(wt_by_year: pd.DataFrame, group_of: dict, colors: dict) -> str:
    """wt_by_year: index=Year, columns=Commodity (already ordered by group),
    values=Target Weight Pct. One row per year, most recent first, plus a
    Total column so the 100%-per-year invariant is checkable at a glance."""
    commodities = list(wt_by_year.columns)
    css = """<style>
      .idxtw-wrap{overflow-x:auto;border:1px solid #e5e7eb;border-radius:8px}
      table.idxtw{border-collapse:collapse;width:100%;font-size:.8rem;
        font-family:-apple-system,Helvetica Neue,sans-serif}
      table.idxtw th,table.idxtw td{padding:8px 14px;text-align:right;white-space:nowrap}
      table.idxtw th:first-child,table.idxtw td:first-child{text-align:left;font-weight:600}
      table.idxtw thead th{background:#0a2463;color:#dde4f0;font-weight:500;letter-spacing:.03em;
        font-size:.68rem;text-transform:uppercase}
      table.idxtw tbody tr:nth-child(even) td{background-color:rgba(0,0,0,.02)}
      table.idxtw td.grp-start,table.idxtw th.grp-start{box-shadow:inset 2px 0 0 #c7cdd6}
      table.idxtw td.total-col,table.idxtw th.total-col{box-shadow:inset 2px 0 0 #c7cdd6;
        font-weight:700;color:#0a2463}
      .idxtw-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
    </style>"""

    header_cells, prev_grp = ["<th>Year</th>"], None
    for c in commodities:
        grp = group_of.get(c, "")
        cls = " class='grp-start'" if prev_grp is not None and grp != prev_grp else ""
        dot = colors.get(c, "#999")
        header_cells.append(f"<th{cls}><span class='idxtw-dot' style='background:{dot}'></span>{c}</th>")
        prev_grp = grp
    header_cells.append("<th class='total-col'>Total</th>")

    body_rows = []
    for y, row in wt_by_year.sort_index(ascending=False).iterrows():
        cells, prev_grp = [f"<td>{y}</td>"], None
        for c in commodities:
            grp = group_of.get(c, "")
            cls = "grp-start" if prev_grp is not None and grp != prev_grp else ""
            v = row.get(c, np.nan)
            cells.append(f"<td class='{cls}'>{v:.2f}%</td>" if pd.notna(v) else f"<td class='{cls}'>—</td>")
            prev_grp = grp
        cells.append(f"<td class='total-col'>{row.sum():.2f}%</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    return (f"{css}<div class='idxtw-wrap'><table class='idxtw'><thead><tr>"
            f"{''.join(header_cells)}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>")

df = load_data()
all_commodities = sorted(df["Commodity"].unique(), key=lambda c: list(GROUP_OF.keys()).index(c) if c in GROUP_OF else 99)
max_date = df["Date"].max()

# ── Total pool per date (all 13 commodities) — used for composition % and
#    for the weight-deviation reference calc below ─────────────────────────
pool = df.pivot_table(index="Date", columns="Commodity", values="Nominal Net USD", aggfunc="last")
total_pool = pool.sum(axis=1, min_count=1)

with st.sidebar:
    st.markdown(
        "<h3 style='font-family:\"Playfair Display\",Georgia,serif;color:#0a2463;"
        "font-weight:400;letter-spacing:-.01em;margin-bottom:1rem'>Index Positioning</h3>",
        unsafe_allow_html=True,
    )
    st.markdown(f"*Data through {max_date.strftime('%d %b %Y')}*")

tab_is, tab_should, tab_weights, tab_snapshot, tab_var, tab_detail = st.tabs(
    ["Index Positioning", "Vs Target", "Target Weights", "Snapshot", "Index in VaR", "Detail"]
)

# ══════════════════════════════════════════════════════════════════════════════
# SNAPSHOT — target vs actual weight (bar) + one master table, every
# commodity's index-leg vitals side by side, at the latest date
# ══════════════════════════════════════════════════════════════════════════════
with tab_snapshot:
    st.markdown(lbl(f"Composition vs Target — {max_date.strftime('%d %b %Y')}"), unsafe_allow_html=True)
    snap = df[df["Date"] == max_date].copy()
    snap_total = snap["Nominal Net USD"].sum(skipna=True)
    snap["Actual Weight Pct"] = snap["Nominal Net USD"] / snap_total * 100
    snap["Deviation Pp"] = snap["Actual Weight Pct"] - snap["Target Weight Pct"]

    bar_level = st.radio("View", ["By Commodity", "By Group"], horizontal=True, key="snap_bar_level")
    if bar_level == "By Group":
        snap["Group"] = snap["Commodity"].map(GROUP_OF)
        bar_src = snap.groupby("Group")[["Target Weight Pct", "Actual Weight Pct"]].sum()
        bar_src = bar_src.reindex(list(GROUPS.keys())).sort_values("Target Weight Pct", ascending=True)
        bar_y = bar_src.index
    else:
        bar_src = snap.sort_values("Target Weight Pct", ascending=True)
        bar_y = bar_src["Commodity"]

    fig_comp = go.Figure()
    fig_comp.add_trace(go.Bar(y=bar_y, x=bar_src["Target Weight Pct"],
                              name="Target", orientation="h", marker_color=GREY, opacity=0.6,
                              hovertemplate="%{y}<br>Target: %{x:.1f}%<extra></extra>"))
    fig_comp.add_trace(go.Bar(y=bar_y, x=bar_src["Actual Weight Pct"],
                              name="Actual", orientation="h", marker_color=NAVY, opacity=0.85,
                              hovertemplate="%{y}<br>Actual: %{x:.1f}%<extra></extra>"))
    fig_comp.update_layout(height=420 if bar_level == "By Commodity" else 260, barmode="group",
                           xaxis=dict(title="% of Total Ags Index Pool", gridcolor="#f0f0f0"),
                           legend=dict(orientation="h", y=1.05, font=dict(size=9)),
                           margin=dict(t=10, b=10, l=4, r=4), **_D)
    st.plotly_chart(fig_comp, use_container_width=True)

    dev_latest = compute_deviation(df, pool, total_pool, all_commodities)
    dev_latest = dev_latest[dev_latest["Date"] == max_date][["Commodity", "Deviation Lots", "Deviation Pct OI"]]
    snap = snap.merge(dev_latest, on="Commodity", how="left")

    st.markdown(lbl("Master Table — every commodity side by side"), unsafe_allow_html=True)
    st.markdown(build_snapshot_table_html(snap, GROUP_OF, COLORS, list(GROUPS.keys())), unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# WHAT IT IS — actual positioning over time, no target comparison
# ══════════════════════════════════════════════════════════════════════════════
with tab_is:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(lbl("Total Ags Net Index — USD"), unsafe_allow_html=True)
        fig_total = base_fig(height=360, yaxis_title="Net Index Notional (USD)")
        fig_total.add_trace(go.Scatter(x=total_pool.index, y=total_pool.values,
                                       line=dict(color=NAVY, width=1.8), name="Total Ags",
                                       hovertemplate="%{x|%d %b %Y}<br>$%{y:,.0f}<extra></extra>"))
        fig_total.update_layout(showlegend=False)
        st.plotly_chart(fig_total, use_container_width=True)
    with c2:
        st.markdown(lbl("By Group"), unsafe_allow_html=True)
        fig_grp = base_fig(height=360, yaxis_title="Net Index Notional (USD)")
        for grp, comms in GROUPS.items():
            cols = [c for c in comms if c in pool.columns]
            if cols:
                s = pool[cols].sum(axis=1, min_count=1)
                fig_grp.add_trace(go.Scatter(x=s.index, y=s.values, name=grp, line=dict(width=1.6),
                                             hovertemplate=f"%{{x|%d %b %Y}}<br>{grp}: $%{{y:,.0f}}<extra></extra>"))
        st.plotly_chart(fig_grp, use_container_width=True)

    st.markdown(lbl("What's Driving the Nominal $ Change — Position vs Price"), unsafe_allow_html=True)
    lookback_opts = {"1 Week": 1, "4 Weeks": 4, "13 Weeks (Quarter)": 13, "52 Weeks (1 Year)": 52}
    lookback_label = st.radio(
        "Lookback", list(lookback_opts.keys()), index=1, horizontal=True, key="attr_lookback",
        help=("Splits the $ change into a Position part (lots changing) and a Price part "
              "(price moving), using the average price and average lots over the period so "
              "both parts always add up exactly to the total. This is the Bennet decomposition."),
    )
    attr_tbl = compute_nominal_attribution(df, all_commodities, lookback_opts[lookback_label])
    if not attr_tbl.empty:
        st.markdown(build_attribution_table_html(attr_tbl, GROUP_OF, COLORS), unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# WHAT IT SHOULD BE — actual positioning vs the GSCI/BCOM target weight
# ══════════════════════════════════════════════════════════════════════════════
with tab_should:
    st.markdown(lbl("Over / Under vs Target Weight (in lots)"), unsafe_allow_html=True)
    default_sel = [c for c in GROUPS["Softs"] if c in all_commodities]
    sel_group = st.radio("Group", ["Softs", "Grains", "Oilseeds", "Livestock", "Custom"],
                         horizontal=True, key="trend_group")
    if sel_group == "Custom":
        sel_commodities = st.multiselect("Commodities", all_commodities, default=default_sel, key="trend_custom")
    else:
        sel_commodities = [c for c in GROUPS[sel_group] if c in all_commodities]

    dev_df = compute_deviation(df, pool, total_pool, all_commodities)
    fig_dev = base_fig(height=420, yaxis_title="Deviation from Target Weight (lots)")
    for comm in sel_commodities:
        s = dev_df[dev_df["Commodity"] == comm].set_index("Date")["Deviation Lots"]
        if not s.empty:
            fig_dev.add_trace(go.Scatter(x=s.index, y=s.values, name=comm,
                                         line=dict(color=COLORS.get(comm), width=1.6),
                                         hovertemplate=f"%{{x|%d %b %Y}}<br>{comm}: %{{y:,.0f}} lots<extra></extra>"))
    fig_dev.add_hline(y=0, line_color="#cccccc", line_width=1)
    _dev_min, _dev_max = dev_df["Date"].min(), dev_df["Date"].max()
    for _yr in range(_dev_min.year, _dev_max.year + 1):
        _jan1 = pd.Timestamp(f"{_yr}-01-01")
        if _dev_min <= _jan1 <= _dev_max:
            fig_dev.add_vline(x=_jan1, line_color="#9ca3af", line_width=1, line_dash="dot")
    st.plotly_chart(fig_dev, use_container_width=True)

    st.markdown(lbl("Over / Under vs Target Weight (in VaR $)"), unsafe_allow_html=True)
    devvar_window = st.radio("Vol Window", [20, 60, 120], horizontal=True,
                             format_func=lambda x: f"{x}D", key="devvar_window")
    daily_px_dev = load_daily_prices()
    vol_df_dev = compute_daily_vol(daily_px_dev)
    var_df_dev = compute_index_var(df, vol_df_dev, all_commodities, devvar_window)
    dev_var_df = compute_deviation_var(dev_df, var_df_dev)

    fig_dev_var = base_fig(height=420, yaxis_title="Deviation from Target Weight (VaR USD)")
    for comm in sel_commodities:
        s = dev_var_df[dev_var_df["Commodity"] == comm].set_index("Date")["Deviation VaR USD"]
        if not s.empty:
            fig_dev_var.add_trace(go.Scatter(x=s.index, y=s.values, name=comm,
                                             line=dict(color=COLORS.get(comm), width=1.6),
                                             hovertemplate=f"%{{x|%d %b %Y}}<br>{comm}: $%{{y:,.0f}}<extra></extra>"))
    fig_dev_var.add_hline(y=0, line_color="#cccccc", line_width=1)
    st.plotly_chart(fig_dev_var, use_container_width=True)

    dev_var_latest = dev_var_df[dev_var_df["Date"] == dev_var_df.groupby("Commodity")["Date"].transform("max")]
    if not dev_var_latest.empty:
        st.markdown(build_deviation_var_table_html(dev_var_latest, GROUP_OF, COLORS, list(GROUPS.keys()), devvar_window),
                   unsafe_allow_html=True)

    st.markdown(lbl("Weekly Deviation vs Target Weight (percentage points)"), unsafe_allow_html=True)
    weekly_dev = compute_weekly_deviation_pct(df, pool, total_pool, all_commodities)
    n_weeks = st.slider("Weeks shown", min_value=8, max_value=min(104, len(weekly_dev)),
                        value=min(52, len(weekly_dev)), step=4, key="trend_weeks")
    st.markdown(build_weekly_deviation_html(weekly_dev.tail(n_weeks), all_commodities, GROUP_OF),
               unsafe_allow_html=True)

    with st.expander("CFTC CIT RIC reference"):
        st.markdown(
            "| Series | RIC pattern | Field |\n|---|---|---|\n"
            "| Index Long | `4<CFTC_CODE>PLNG` | `COMM_LAST` |\n"
            "| Index Short | `4<CFTC_CODE>PSHT` | `COMM_LAST` |\n"
            "| Total OI | `3CFTC<CFTC_CODE>OI` | `COMM_LAST` |\n"
            "| Price | see README (`price_ric` per commodity) | `TRDPRC_1` |"
        )

# ══════════════════════════════════════════════════════════════════════════════
# TARGET WEIGHTS — the full GSCI/BCOM target weight table, one row per year,
# so the per-calendar-year weights driving every deviation calc are visible
# and auditable on their own, not buried in an expander.
# ══════════════════════════════════════════════════════════════════════════════
with tab_weights:
    st.markdown(lbl("GSCI/BCOM Target Weight — by Year"), unsafe_allow_html=True)
    st.caption("60% S&P GSCI RPDW + 40% Bloomberg BCOM Target Weight, each re-weighted to sum "
              "to 100% within just these 13 commodities. Re-derived at each January rebalance — "
              "not a single constant held flat across years (see README for sources and the two "
              "indices' coverage gaps: GSCI has no Soybean Meal/Oil, BCOM has no Feeder Cattle, "
              "Cocoa was out of BCOM 2017-2025).")
    yr_ref = df.assign(Year=df["Date"].dt.year)
    wt_by_year = yr_ref.pivot_table(index="Year", columns="Commodity",
                                    values="Target Weight Pct", aggfunc="last")
    wt_cols = sorted([c for c in all_commodities if c in wt_by_year.columns],
                     key=lambda c: list(GROUPS.keys()).index(GROUP_OF.get(c, "")) if GROUP_OF.get(c, "") in GROUPS else 99)
    wt_by_year = wt_by_year[wt_cols]
    st.markdown(build_target_weights_table_html(wt_by_year, GROUP_OF, COLORS), unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# INDEX IN VAR — Index Traders' lots converted to a 1-day 99% dollar VaR
# (Price x Multiplier x realized Vol x Z), the same methodology COT_ALL's
# "Specs in VaR" tab uses for Managed Money — so passive Index risk and
# active Spec risk can be compared on the same $-risk scale, not just by
# raw lots or notional $ (which ignore how volatile each market is).
# ══════════════════════════════════════════════════════════════════════════════
with tab_var:
    st.latex(r"\text{Net VaR} = \text{Net Lots} \times \text{Price} \times \text{Multiplier} \times \sigma_{\text{daily}} \times Z_{99\%}")
    st.caption("σ = realized daily volatility (rolling 20/60/120D std of daily returns), Z₉₉% = 2.3263 (1-day, 99% one-tailed confidence)")

    daily_px = load_daily_prices()
    vol_df = compute_daily_vol(daily_px)
    vol_window = st.radio("Vol Window", [20, 60, 120], horizontal=True,
                          format_func=lambda x: f"{x}D", key="var_window")
    var_df = compute_index_var(df, vol_df, all_commodities, vol_window)
    var_latest = var_df[var_df["Date"] == var_df.groupby("Commodity")["Date"].transform("max")]

    st.markdown(lbl(f"Net VaR ($M) — all 13 commodities, {vol_window}D vol"), unsafe_allow_html=True)
    var_bar_level = st.radio("View", ["By Commodity", "By Group"], horizontal=True, key="var_bar_level")
    vl = var_latest.copy()
    vl["Net VaR M"] = vl["Net VaR USD"] / 1e6
    if var_bar_level == "By Group":
        vl["Group"] = vl["Commodity"].map(GROUP_OF)
        bar_src = vl.groupby("Group")["Net VaR M"].sum().reindex(list(GROUPS.keys())).sort_values()
        bar_y, bar_x, bar_colors = bar_src.index, bar_src.values, None
    else:
        bar_src = vl.sort_values("Net VaR M")
        bar_y, bar_x = bar_src["Commodity"], bar_src["Net VaR M"]
        bar_colors = [COLORS.get(c, NAVY) for c in bar_y]

    fig_var = go.Figure()
    fig_var.add_trace(go.Bar(y=bar_y, x=bar_x, orientation="h",
                             marker_color=bar_colors if bar_colors else NAVY,
                             hovertemplate="%{y}<br>Net VaR: $%{x:.1f}M<extra></extra>"))
    fig_var.add_vline(x=0, line_color="#cccccc", line_width=1)
    fig_var.update_layout(height=420 if var_bar_level == "By Commodity" else 260,
                          xaxis=dict(title="Net VaR ($M, 1-day 99% confidence)", gridcolor="#f0f0f0"),
                          showlegend=False, margin=dict(t=10, b=10, l=4, r=4), **_D)
    st.plotly_chart(fig_var, use_container_width=True)

    st.markdown(lbl("Master Table — VaR per commodity"), unsafe_allow_html=True)
    if not var_latest.empty:
        st.markdown(build_var_table_html(var_latest, GROUP_OF, COLORS, list(GROUPS.keys()), vol_window),
                   unsafe_allow_html=True)

    st.markdown(lbl("Net VaR ($M) Over Time"), unsafe_allow_html=True)
    default_sel_var = [c for c in GROUPS["Softs"] if c in all_commodities]
    sel_group_var = st.radio("Group", ["Softs", "Grains", "Oilseeds", "Livestock", "Custom"],
                             horizontal=True, key="var_group")
    if sel_group_var == "Custom":
        sel_commodities_var = st.multiselect("Commodities", all_commodities, default=default_sel_var, key="var_custom")
    else:
        sel_commodities_var = [c for c in GROUPS[sel_group_var] if c in all_commodities]

    fig_var_ts = base_fig(height=420, yaxis_title="Net VaR ($M)")
    for comm in sel_commodities_var:
        s = var_df[var_df["Commodity"] == comm].set_index("Date")["Net VaR USD"] / 1e6
        if not s.empty:
            fig_var_ts.add_trace(go.Scatter(x=s.index, y=s.values, name=comm,
                                            line=dict(color=COLORS.get(comm), width=1.6),
                                            hovertemplate=f"%{{x|%d %b %Y}}<br>{comm}: $%{{y:.1f}}M<extra></extra>"))
    fig_var_ts.add_hline(y=0, line_color="#cccccc", line_width=1)
    st.plotly_chart(fig_var_ts, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# PER-COMMODITY DETAIL
# ══════════════════════════════════════════════════════════════════════════════
with tab_detail:
    comm_pick = st.selectbox("Commodity", all_commodities, key="detail_comm")
    d = df[df["Commodity"] == comm_pick].set_index("Date")

    st.markdown(lbl(f"{comm_pick} — Index Long / Short (lots)"), unsafe_allow_html=True)
    fig_ls = base_fig(height=380, yaxis_title="Lots")
    fig_ls.add_trace(go.Scatter(x=d.index, y=d["Index Long"], name="Index Long", line=dict(color=GREEN, width=1.4),
                                hovertemplate="%{x|%d %b %Y}<br>Index Long: %{y:,.0f}<extra></extra>"))
    fig_ls.add_trace(go.Scatter(x=d.index, y=d["Index Short"], name="Index Short", line=dict(color=RED, width=1.4),
                                hovertemplate="%{x|%d %b %Y}<br>Index Short: %{y:,.0f}<extra></extra>"))
    fig_ls.add_trace(go.Scatter(x=d.index, y=d["Index Net"], name="Net", line=dict(color=NAVY, width=2),
                                hovertemplate="%{x|%d %b %Y}<br>Net: %{y:,.0f}<extra></extra>"))
    st.plotly_chart(fig_ls, use_container_width=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(lbl("Net as % of Total OI"), unsafe_allow_html=True)
        fig_pct = base_fig(height=340, yaxis_title="% of Total OI")
        fig_pct.add_trace(go.Scatter(x=d.index, y=d["Net Pct OI"], line=dict(color=AMBER, width=1.6),
                                     hovertemplate="%{x|%d %b %Y}<br>%{y:.1f}%<extra></extra>"))
        fig_pct.add_hline(y=0, line_color="#cccccc", line_width=1)
        st.plotly_chart(fig_pct, use_container_width=True)
    with c2:
        st.markdown(lbl("Nominal Net Notional (USD)"), unsafe_allow_html=True)
        fig_nom = base_fig(height=340, yaxis_title="USD")
        fig_nom.add_trace(go.Scatter(x=d.index, y=d["Nominal Net USD"], line=dict(color=NAVY, width=1.6),
                                     fill="tozeroy", fillcolor="rgba(10,36,99,0.07)",
                                     hovertemplate="%{x|%d %b %Y}<br>$%{y:,.0f}<extra></extra>"))
        fig_nom.add_hline(y=0, line_color="#cccccc", line_width=1)
        st.plotly_chart(fig_nom, use_container_width=True)
