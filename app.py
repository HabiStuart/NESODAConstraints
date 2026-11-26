"""GB day-ahead constraint flows and limits — Streamlit dashboard.

Reads NESO's "Day Ahead Constraint Flows and Limits" straight from the NESO Data Portal API
(public, no key needed) and shows, per constraint group, forecast flow against limit for
every half-hour from 14 days ago through tomorrow.

Auto-update: every CHECK_EVERY the page re-checks NESO's "last modified" time (a small
request). The full data is only re-downloaded when that time changes, so new data appears
within about CHECK_EVERY of NESO publishing it, without anyone reloading the page.

Run locally:  streamlit run app.py
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------- settings
API = "https://api.neso.energy/api/3/action"
DATASET_ID = "day-ahead-constraint-flows-and-limits"
RESOURCE_ID = "38a18ec1-9e40-465d-93fb-301e80fd1352"
PORTAL_URL = "https://www.neso.energy/data-portal/day-ahead-constraint-flows-and-limits"
CHECK_EVERY = "10m"            # how often an open page re-checks NESO
METADATA_TTL_S = 300           # cache NESO's "last modified" check for 5 minutes
HISTORY_DAYS = 14              # days before today shown on the charts
UK = ZoneInfo("Europe/London")
# Limits NESO publishes that behave like "no limit" placeholders (our inference, not NESO's
# definition): flat values that dominate groups whose real limits otherwise vary.
PLACEHOLDER_LIMITS = {9999, 10000, 99999}
MAP_PATH = Path(__file__).parent / "assets" / "constraint-map.png"
HTTP_TIMEOUT_S = 120

st.set_page_config(page_title="GB day-ahead constraint flows and limits", layout="wide")


# ---------------------------------------------------------------- NESO access
def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "habitat-da-constraints-dashboard"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        payload = json.load(resp)
    if not payload.get("success"):
        raise RuntimeError(f"NESO API returned an error: {payload.get('error')}")
    return payload["result"]


@st.cache_data(ttl=METADATA_TTL_S, show_spinner=False)
def neso_last_modified() -> str:
    """The time NESO last changed the dataset. Cheap; used to decide whether to re-download."""
    result = _get_json(f"{API}/package_show?id={urllib.parse.quote(DATASET_ID)}")
    for res in result.get("resources", []):
        if res.get("id") == RESOURCE_ID:
            return res.get("last_modified") or result.get("metadata_modified") or ""
    return result.get("metadata_modified") or ""


@st.cache_data(show_spinner="Downloading the latest data from NESO…", max_entries=4)
def load_window(start: str, last_modified: str) -> pd.DataFrame:
    """Rows on or after `start` (YYYY-MM-DD), in NESO's own row order.

    `last_modified` is only part of the cache key: a new value forces a fresh download.
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):  # the only value placed into the SQL
        raise ValueError(f"Bad start date: {start!r}")
    sql = (
        'SELECT _id, "Constraint Group" AS g, "Date_ Time GMT_BST" AS t, '
        '"Limit_MW" AS l, "Flow_MW" AS f '
        f'FROM "{RESOURCE_ID}" WHERE "Date_ Time GMT_BST" >= \'{start}\' ORDER BY _id'
    )
    records = _get_json(f"{API}/datastore_search_sql?sql={urllib.parse.quote(sql)}")["records"]
    return clean(pd.DataFrame(records, columns=["_id", "g", "t", "l", "f"]))


# ---------------------------------------------------------------- cleaning
def _periods_in_day(d: pd.Timestamp) -> int:
    a = pd.Timestamp(d).tz_localize(UK)
    b = (pd.Timestamp(d) + pd.Timedelta(days=1)).tz_localize(UK)
    return int((b - a) / pd.Timedelta(minutes=30))


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Settlement date + period from NESO's UK local times, duplicates removed.

    - NESO repeats some whole days: exact duplicate rows are dropped (not on clock-change days).
    - On the autumn clock-change day NESO lists the repeated hour as adjacent pairs
      (01:00, 01:00, 01:30, 01:30). Assumption: in NESO's row order the first of each pair is
      BST and the second GMT (it joins smoothly to the hours either side).
    """
    if df.empty:
        return pd.DataFrame(columns=["group", "date", "sp", "time_local", "limit_mw", "flow_mw", "t"])
    df = df.copy()
    df["t"] = pd.to_datetime(df["t"], format="ISO8601")
    df["l"] = pd.to_numeric(df["l"], errors="coerce")
    df["f"] = pd.to_numeric(df["f"], errors="coerce")
    df["date"] = df["t"].dt.normalize()
    day_len = {d: _periods_in_day(d) for d in df["date"].unique()}
    normal_day = df["date"].map(day_len) == 48
    df = df[~(normal_day & df.duplicated(["g", "t", "l", "f"], keep="first"))]

    df = df.sort_values("_id")
    first_seen = df.groupby(["g", "t"]).cumcount() == 0
    df["utc"] = df["t"].dt.tz_localize(UK, ambiguous=first_seen.to_numpy(), nonexistent="NaT")
    df = df[df["utc"].notna()].copy()
    midnight = df["date"].dt.tz_localize(UK)
    df["sp"] = ((df["utc"] - midnight).dt.total_seconds() // 1800).astype(int) + 1
    df = df.drop_duplicates(["g", "date", "sp"])

    out = pd.DataFrame({
        "group": df["g"],
        "date": df["date"].dt.date,
        "sp": df["sp"],
        "time_local": df["t"].dt.strftime("%H:%M"),
        "limit_mw": df["l"].where(~df["l"].isin(PLACEHOLDER_LIMITS) & (df["l"] > 0)),
        "flow_mw": df["f"],
    })
    # Plot position: settlement day start + (SP-1) half-hours, kept timezone-naive so the
    # axis reads in settlement terms rather than the viewer's browser time zone.
    out["t"] = pd.to_datetime(out["date"]) + pd.to_timedelta((out["sp"] - 1) * 30, unit="min")
    return out.sort_values(["group", "t"]).reset_index(drop=True)


# ---------------------------------------------------------------- charts
def over_limit_counts(df: pd.DataFrame, day: date) -> pd.Series:
    d = df[(df["date"] == day) & df["limit_mw"].notna() & df["flow_mw"].notna()]
    return (d["flow_mw"] > d["limit_mw"]).groupby(d["group"]).sum().astype(int)


def group_chart(g: pd.DataFrame, x_start: pd.Timestamp, x_end: pd.Timestamp, tomorrow: pd.Timestamp) -> go.Figure:
    """Flow (solid) and limit (dashed step) by half-hour, with tomorrow shaded."""
    g = g.sort_values("t").copy()
    # Insert an empty row wherever half-hours are missing, so lines break at gaps
    # instead of being drawn straight across them.
    gap_after = g["t"].diff().shift(-1) > pd.Timedelta(minutes=30)
    blanks = g.loc[gap_after, ["t"]].copy()
    blanks["t"] = blanks["t"] + pd.Timedelta(minutes=15)
    g = pd.concat([g, blanks]).sort_values("t")
    util = (g["flow_mw"] / g["limit_mw"] * 100).round(0)
    hover = [
        f"{d} SP{int(sp)} ({tl})<br>Flow {f:,.0f} MW<br>Limit {('%s MW' % format(l, ',.0f')) if pd.notna(l) else 'none published'}"
        f"<br>Utilisation {('%d%%' % u) if pd.notna(u) else '—'}"
        if pd.notna(sp) else ""
        for d, sp, tl, f, l, u in zip(g["date"], g["sp"], g["time_local"], g["flow_mw"], g["limit_mw"], util)
    ]
    fig = go.Figure()
    fig.add_vrect(x0=tomorrow, x1=x_end, fillcolor="#8a94a6", opacity=0.18, line_width=0, layer="below")
    fig.add_trace(go.Scatter(x=g["t"], y=g["limit_mw"], mode="lines", line=dict(color="#d64545", width=1.3, dash="dash", shape="hv"),
                             hoverinfo="skip", connectgaps=False, name="Limit"))
    fig.add_trace(go.Scatter(x=g["t"], y=g["flow_mw"], mode="lines", line=dict(color="#2f6fdf", width=1.5),
                             text=hover, hovertemplate="%{text}<extra></extra>", connectgaps=False, name="Flow"))
    fig.update_layout(height=220, margin=dict(l=50, r=10, t=10, b=30), showlegend=False,
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", hovermode="closest")
    fig.update_xaxes(range=[x_start, x_end], tickformat="%-d %b", dtick=2 * 86400000, showgrid=False)
    fig.update_yaxes(title_text="MW", tickformat="~s", gridcolor="rgba(128,128,128,0.2)", zeroline=True,
                     zerolinecolor="rgba(128,128,128,0.5)")
    return fig


# ---------------------------------------------------------------- page
st.title("GB day-ahead constraint flows and limits")

if MAP_PATH.exists():
    st.image(str(MAP_PATH), width=560,
             caption="Reference map. NESO's data now uses newer names for some boundaries "
                     "(for example SSE_SP2, SSHARN3, SEIMPPR23) and groups not drawn here.")


@st.fragment(run_every=CHECK_EVERY)
def charts() -> None:
    today = datetime.now(UK).date()
    tomorrow = today + timedelta(days=1)
    start = today - timedelta(days=HISTORY_DAYS)
    try:
        modified = neso_last_modified()
        df = load_window(start.isoformat(), modified)
    except Exception as exc:  # network or API problem: say what happened, don't show stale guesses
        st.error(f"Couldn't load data from NESO ({type(exc).__name__}: {exc}). "
                 f"The page retries automatically every {CHECK_EVERY}.")
        return

    df = df[(df["date"] >= start) & (df["date"] <= tomorrow)]
    if df.empty:
        st.warning("NESO returned no data for this period.")
        return

    last_day = max(df["date"])
    # Order: groups over their limit tomorrow first; if tomorrow isn't published yet, use the
    # latest day that is, and say so.
    rank_day = tomorrow if last_day >= tomorrow else last_day
    counts = over_limit_counts(df, rank_day)
    groups = sorted(df["group"].unique(), key=lambda g: (-int(counts.get(g, 0)), g))

    pulled = datetime.now(timezone.utc).astimezone(UK).strftime("%H:%M on %-d %b")
    st.subheader(f"Forecast flow against limit, {start:%-d %b} to {tomorrow:%-d %b %Y}")
    st.caption(
        f"Source: [NESO Data Portal]({PORTAL_URL}), last updated by NESO {modified[:16].replace('T', ' ')} UTC. "
        f"Checked {pulled} (UK time); this page re-checks every {CHECK_EVERY}. "
        "Blue: forecast flow (MW). Red dashed: limit (MW, where published). Grey band: tomorrow."
    )
    if last_day < tomorrow:
        st.info(f"NESO hasn't published tomorrow ({tomorrow:%-d %b}) yet; the data stops at {last_day:%-d %b}. "
                f"Charts are ordered by over-limit half-hours on {rank_day:%-d %b} until it arrives. "
                "NESO has typically published around 16:00 UK time.")

    x_start = pd.Timestamp(start)
    x_end = pd.Timestamp(tomorrow + timedelta(days=1))
    cols = st.columns(2)
    for i, grp in enumerate(groups):
        g = df[df["group"] == grp]
        n_over = int(counts.get(grp, 0))
        with cols[i % 2]:
            if n_over:
                note = f":red[over limit in {n_over} half-hour{'s' if n_over != 1 else ''} on {rank_day:%-d %b}]"
            elif g["limit_mw"].notna().any():
                note = ""
            else:
                note = "no limit published"
            st.markdown(f"**{grp}** {note}")
            st.plotly_chart(group_chart(g, x_start, x_end, pd.Timestamp(tomorrow)), use_container_width=True,
                            config={"displayModeBar": False}, key=f"chart-{grp}")


charts()

with st.expander("How the numbers are worked out"):
    st.markdown(
        "- **Flows** are NESO's day-ahead modelled forecast, not outturn. Units are MW.\n"
        "- **Over limit** means forecast flow is above the published limit in that half-hour.\n"
        "- **Placeholder limits:** limits of exactly 9,999, 10,000 or 99,999 MW are treated as "
        "\"no limit published\". This is an inference from the data, not a NESO definition.\n"
        "- **Times:** NESO publishes UK local time. Rows are converted to settlement period, so "
        "clock-change days have 46 or 50 periods. On the autumn change, the first of each repeated "
        "time is taken as BST (an assumption about NESO's row order).\n"
        "- **Sign:** negative flow is flow against the boundary's direction.\n"
        "- **Cleaning:** exact duplicate rows are removed; missing half-hours are left as gaps."
    )
