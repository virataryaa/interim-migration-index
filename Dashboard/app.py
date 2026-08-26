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
    "Grains":    ["SRW", "HRW", "CORN", "SOYBEAN", "BEAN OIL", "MEAL"],
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

@st.cache_data(ttl=1800)
def compute_deviation(df: pd.DataFrame, pool: pd.DataFrame, all_commodities: list) -> pd.DataFrame:
    """Static Jan-reference lot count implied by each commodity's target
    weight vs the actual pool size that year, held constant for the year,
    compared against actual reported net lots. See tab_dev's markdown for
    the full explanation — this is the expensive part, so it's cached."""
    df = df.copy()
    df["Year"] = df["Date"].dt.year
    dev_frames = []
    for year, g in df.groupby("Year"):
        ref_date = g["Date"].min()
        if ref_date not in pool.index:
            continue
        ref_pool_total = pool.loc[ref_date].sum(skipna=True)
        if not ref_pool_total or pd.isna(ref_pool_total):
            continue
        for comm in all_commodities:
            gc = g[g["Commodity"] == comm]
            if gc.empty:
                continue
            ref_price_row = gc[gc["Date"] == ref_date]
            if ref_price_row.empty or pd.isna(ref_price_row["Price"].iloc[0]):
                continue
            ref_price = ref_price_row["Price"].iloc[0]
            multiplier = ref_price_row["Multiplier"].iloc[0]
            target_pct = ref_price_row["Target Weight Pct"].iloc[0]
            ref_lots = (target_pct / 100 * ref_pool_total) / (ref_price * multiplier)
            gc = gc.copy()
            gc["Deviation Lots"] = gc["Index Net"] - ref_lots
            dev_frames.append(gc[["Commodity", "Date", "Deviation Lots"]])
    return pd.concat(dev_frames, ignore_index=True) if dev_frames else pd.DataFrame()

@st.cache_data(ttl=1800)
def compute_weekly_deviation_pct(df: pd.DataFrame, pool: pd.DataFrame, total_pool: pd.Series,
                                  all_commodities: list) -> pd.DataFrame:
    """Actual $-share of the pool minus each commodity's (constant) GSCI/BCOM
    target weight, at every date — the simple %-of-weight deviation (distinct
    from the static Jan-reference *lots* deviation in compute_deviation())."""
    target_pct = df.drop_duplicates("Commodity").set_index("Commodity")["Target Weight Pct"]
    actual_pct = pool.div(total_pool, axis=0) * 100
    cols = [c for c in all_commodities if c in actual_pct.columns]
    dev = actual_pct[cols].sub(target_pct.reindex(cols), axis=1)
    return dev

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

def build_composition_table_html(tbl: pd.DataFrame) -> str:
    css = """<style>
      .idxcomp-wrap{overflow-x:auto;border:1px solid #e5e7eb;border-radius:6px}
      table.idxcomp{border-collapse:collapse;width:100%;font-size:.8rem;
        font-family:-apple-system,Helvetica Neue,sans-serif}
      table.idxcomp th,table.idxcomp td{padding:6px 14px;text-align:right}
      table.idxcomp th:first-child,table.idxcomp td:first-child{text-align:left}
      table.idxcomp thead th{background:#0a2463;color:#dde4f0;font-weight:500;letter-spacing:.03em;
        font-size:.7rem;text-transform:uppercase}
      table.idxcomp tbody tr:nth-child(even) td{background-color:rgba(0,0,0,.02)}
      table.idxcomp td.name-cell{font-weight:600;color:#1d1d1f}
    </style>"""
    dev_vmax = max(tbl["Deviation Pp"].abs().max(), 0.01)
    nom_vmax = max(tbl["Nominal Net USD"].abs().max(), 1)
    rows = []
    for _, r in tbl.iterrows():
        dev_style = _diverging_cell_style(r["Deviation Pp"], dev_vmax)
        dev_color = "#16a34a" if r["Deviation Pp"] >= 0 else "#dc2626"
        nom_style = _diverging_cell_style(r["Nominal Net USD"], nom_vmax)
        nom_color = "#16a34a" if r["Nominal Net USD"] >= 0 else "#dc2626"
        rows.append(
            "<tr>"
            f"<td class='name-cell'>{r['Commodity']}</td>"
            f"<td>{r['Target Weight Pct']:.1f}%</td>"
            f"<td>{r['Actual Weight Pct']:.1f}%</td>"
            f"<td style='{dev_style}color:{dev_color};font-weight:600'>{r['Deviation Pp']:+.2f}pp</td>"
            f"<td style='{nom_style}color:{nom_color};font-weight:600'>${r['Nominal Net USD']:,.0f}</td>"
            "</tr>"
        )
    header = ("<tr><th>Commodity</th><th>Target Weight</th><th>Actual Weight</th>"
              "<th>Deviation</th><th>Nominal Net USD</th></tr>")
    return f"{css}<div class='idxcomp-wrap'><table class='idxcomp'><thead>{header}</thead><tbody>{''.join(rows)}</tbody></table></div>"

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

tab_overview, tab_total, tab_dev, tab_comp, tab_detail = st.tabs(
    ["Overview", "Total Ags Index", "Weight Deviation (Lots)", "Composition vs Target", "Per-Commodity Detail"]
)

# ══════════════════════════════════════════════════════════════════════════════
# OVERVIEW — the original brief's visuals in one place: Total Ags Net Index,
# Under/Over vs Start-of-Year Weights, and the weekly %-of-weight deviation
# table across all 13 commodities, plus the target-weight / RIC reference.
# ══════════════════════════════════════════════════════════════════════════════
with tab_overview:
    st.markdown(lbl("Total Ags Net Index — CFTC Index Traders, USD"), unsafe_allow_html=True)
    fig_ov_total = base_fig(height=380, yaxis_title="Net Index Notional (USD)")
    fig_ov_total.add_trace(go.Scatter(x=total_pool.index, y=total_pool.values,
                                      line=dict(color=NAVY, width=1.8), name="Total Ags"))
    fig_ov_total.update_layout(showlegend=False)
    st.plotly_chart(fig_ov_total, use_container_width=True)

    st.markdown(lbl("Under / Over vs Start-of-Year Weights (in lots)"), unsafe_allow_html=True)
    default_sel_ov = [c for c in GROUPS["Softs"] if c in all_commodities]
    sel_group_ov = st.radio("Group", ["Softs", "Grains", "Livestock", "Custom"], horizontal=True, key="ov_group")
    if sel_group_ov == "Custom":
        sel_commodities_ov = st.multiselect("Commodities", all_commodities, default=default_sel_ov, key="ov_custom")
    else:
        sel_commodities_ov = [c for c in GROUPS[sel_group_ov] if c in all_commodities]

    dev_df_ov = compute_deviation(df, pool, all_commodities)
    fig_ov_dev = base_fig(height=420, yaxis_title="Deviation from Start-of-Year Target (lots)")
    for comm in sel_commodities_ov:
        s = dev_df_ov[dev_df_ov["Commodity"] == comm].set_index("Date")["Deviation Lots"]
        if not s.empty:
            fig_ov_dev.add_trace(go.Scatter(x=s.index, y=s.values, name=comm,
                                            line=dict(color=COLORS.get(comm), width=1.6)))
    fig_ov_dev.add_hline(y=0, line_color="#cccccc", line_width=1)
    st.plotly_chart(fig_ov_dev, use_container_width=True)

    st.markdown(lbl("Weekly Deviation vs Target Weight (percentage points)"), unsafe_allow_html=True)
    weekly_dev = compute_weekly_deviation_pct(df, pool, total_pool, all_commodities)
    n_weeks = st.slider("Weeks shown", min_value=8, max_value=min(104, len(weekly_dev)),
                        value=min(52, len(weekly_dev)), step=4, key="ov_weeks")
    st.markdown(build_weekly_deviation_html(weekly_dev.tail(n_weeks), all_commodities, GROUP_OF),
               unsafe_allow_html=True)

    with st.expander("Target weights & CFTC RIC reference"):
        rc1, rc2 = st.columns(2)
        with rc1:
            st.markdown("**GSCI/BCOM target weight (ag-only, re-weighted)**")
            wt_ref = df.drop_duplicates("Commodity")[["Commodity", "Target Weight Pct"]].copy()
            wt_ref["Target Weight Pct"] = wt_ref["Target Weight Pct"].map(lambda v: f"{v:.1f}%")
            st.dataframe(wt_ref, use_container_width=True, hide_index=True)
        with rc2:
            st.markdown("**CFTC CIT RIC scheme**")
            st.markdown(
                "| Series | RIC pattern | Field |\n|---|---|---|\n"
                "| Index Long | `4<CFTC_CODE>PLNG` | `COMM_LAST` |\n"
                "| Index Short | `4<CFTC_CODE>PSHT` | `COMM_LAST` |\n"
                "| Total OI | `3CFTC<CFTC_CODE>OI` | `COMM_LAST` |\n"
                "| Price | `<ROOT>c2` (2nd-month) | `TRDPRC_1` |"
            )

# ══════════════════════════════════════════════════════════════════════════════
# TOTAL AGS INDEX — aggregate passive-money notional, all 13 commodities
# ══════════════════════════════════════════════════════════════════════════════
with tab_total:
    st.markdown(lbl("Total Ags Net Index — CFTC Index Traders, USD"), unsafe_allow_html=True)
    fig_total = base_fig(height=460, yaxis_title="Net Index Notional (USD)")
    fig_total.add_trace(go.Scatter(x=total_pool.index, y=total_pool.values,
                                   line=dict(color=NAVY, width=1.8), name="Total Ags"))
    fig_total.update_layout(showlegend=False)
    st.plotly_chart(fig_total, use_container_width=True)

    st.markdown(lbl("By Group"), unsafe_allow_html=True)
    fig_grp = base_fig(height=420, yaxis_title="Net Index Notional (USD)")
    for grp, comms in GROUPS.items():
        cols = [c for c in comms if c in pool.columns]
        if cols:
            s = pool[cols].sum(axis=1, min_count=1)
            fig_grp.add_trace(go.Scatter(x=s.index, y=s.values, name=grp, line=dict(width=1.6)))
    st.plotly_chart(fig_grp, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# WEIGHT DEVIATION (LOTS) — actual net lots vs a static Jan-reference lot
# count implied by each commodity's target weight
# ══════════════════════════════════════════════════════════════════════════════
with tab_dev:
    st.markdown(lbl("Under / Over vs Start-of-Year Weights (in lots)"), unsafe_allow_html=True)
    st.markdown(
        "Methodology: at the first available date each year, the total actual "
        "Index-Trader pool ($) across all 13 commodities is taken as the "
        "reference pool size. Each commodity's *target* lot count is back-solved "
        "from its GSCI/BCOM target weight against that reference pool and that "
        "date's price — then held static for the rest of the year (no further "
        "rebalancing assumed until the next January). The gap between that "
        "static reference and the commodity's *actual* reported net lots is "
        "what's plotted — growing more negative means real positioning has "
        "fallen further below what the target weight implied; growing positive "
        "means it's run further above."
    )

    default_sel = [c for c in GROUPS["Softs"] if c in all_commodities]
    sel_group = st.radio("Group", ["Softs", "Grains", "Livestock", "Custom"], horizontal=True, key="dev_group")
    if sel_group == "Custom":
        sel_commodities = st.multiselect("Commodities", all_commodities, default=default_sel, key="dev_custom")
    else:
        sel_commodities = [c for c in GROUPS[sel_group] if c in all_commodities]

    dev_df = compute_deviation(df, pool, all_commodities)

    fig_dev = base_fig(height=480, yaxis_title="Deviation from Start-of-Year Target (lots)")
    for comm in sel_commodities:
        s = dev_df[dev_df["Commodity"] == comm].set_index("Date")["Deviation Lots"]
        if not s.empty:
            fig_dev.add_trace(go.Scatter(x=s.index, y=s.values, name=comm,
                                         line=dict(color=COLORS.get(comm), width=1.6)))
    fig_dev.add_hline(y=0, line_color="#cccccc", line_width=1)
    st.plotly_chart(fig_dev, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# COMPOSITION VS TARGET — latest $ share of the pool per commodity vs its
# GSCI/BCOM target weight
# ══════════════════════════════════════════════════════════════════════════════
with tab_comp:
    st.markdown(lbl(f"Composition vs Target — {max_date.strftime('%d %b %Y')}"), unsafe_allow_html=True)
    latest = df[df["Date"] == max_date].copy()
    latest_total = latest["Nominal Net USD"].sum(skipna=True)
    latest["Actual Weight Pct"] = latest["Nominal Net USD"] / latest_total * 100
    latest["Deviation Pp"] = latest["Actual Weight Pct"] - latest["Target Weight Pct"]
    latest = latest.sort_values("Target Weight Pct", ascending=True)

    fig_comp = go.Figure()
    fig_comp.add_trace(go.Bar(y=latest["Commodity"], x=latest["Target Weight Pct"],
                              name="Target", orientation="h", marker_color=GREY, opacity=0.6))
    fig_comp.add_trace(go.Bar(y=latest["Commodity"], x=latest["Actual Weight Pct"],
                              name="Actual", orientation="h", marker_color=NAVY, opacity=0.85))
    fig_comp.update_layout(height=520, barmode="group",
                           xaxis=dict(title="% of Total Ags Index Pool", gridcolor="#f0f0f0"),
                           legend=dict(orientation="h", y=1.05, font=dict(size=9)),
                           margin=dict(t=10, b=10, l=4, r=4), **_D)
    st.plotly_chart(fig_comp, use_container_width=True)

    st.markdown(lbl("Deviation Table (percentage points)"), unsafe_allow_html=True)
    tbl = latest[["Commodity", "Target Weight Pct", "Actual Weight Pct", "Deviation Pp", "Nominal Net USD"]].copy()
    tbl = tbl.sort_values("Deviation Pp", ascending=False)
    st.markdown(build_composition_table_html(tbl), unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# PER-COMMODITY DETAIL
# ══════════════════════════════════════════════════════════════════════════════
with tab_detail:
    comm_pick = st.selectbox("Commodity", all_commodities, key="detail_comm")
    d = df[df["Commodity"] == comm_pick].set_index("Date")

    st.markdown(lbl(f"{comm_pick} — Index Long / Short (lots)"), unsafe_allow_html=True)
    fig_ls = base_fig(height=380, yaxis_title="Lots")
    fig_ls.add_trace(go.Scatter(x=d.index, y=d["Index Long"], name="Index Long", line=dict(color=GREEN, width=1.4)))
    fig_ls.add_trace(go.Scatter(x=d.index, y=d["Index Short"], name="Index Short", line=dict(color=RED, width=1.4)))
    fig_ls.add_trace(go.Scatter(x=d.index, y=d["Index Net"], name="Net", line=dict(color=NAVY, width=2)))
    st.plotly_chart(fig_ls, use_container_width=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(lbl("Net as % of Total OI"), unsafe_allow_html=True)
        fig_pct = base_fig(height=340, yaxis_title="% of Total OI")
        fig_pct.add_trace(go.Scatter(x=d.index, y=d["Net Pct OI"], line=dict(color=AMBER, width=1.6)))
        fig_pct.add_hline(y=0, line_color="#cccccc", line_width=1)
        st.plotly_chart(fig_pct, use_container_width=True)
    with c2:
        st.markdown(lbl("Nominal Net Notional (USD)"), unsafe_allow_html=True)
        fig_nom = base_fig(height=340, yaxis_title="USD")
        fig_nom.add_trace(go.Scatter(x=d.index, y=d["Nominal Net USD"], line=dict(color=NAVY, width=1.6),
                                     fill="tozeroy", fillcolor="rgba(10,36,99,0.07)"))
        fig_nom.add_hline(y=0, line_color="#cccccc", line_width=1)
        st.plotly_chart(fig_nom, use_container_width=True)
