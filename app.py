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

import altair as alt
import pandas as pd
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


def group_chart(g: pd.DataFrame, x_start: pd.Timestamp, x_end: pd.Timestamp, tomorrow: pd.Timestamp) -> alt.LayerChart:
    g = g.copy()
    # Break the lines where half-hours are missing instead of joining across the gap.
    gap = g["t"].diff().dt.total_seconds().fillna(0) > 1800
    g["segment"] = gap.cumsum()
    g["utilisation"] = (g["flow_mw"] / g["limit_mw"] * 100).round(0)
    x = alt.X("t:T", title=None, scale=alt.Scale(domain=[x_start.isoformat(), x_end.isoformat()], type="utc"),
              axis=alt.Axis(format="%-d %b", tickCount="day", labelOverlap=True))
    tooltip = [
        alt.Tooltip("date:N", title="Settlement day"),
        alt.Tooltip("sp:Q", title="Settlement period"),
        alt.Tooltip("time_local:N", title="UK time"),
        alt.Tooltip("flow_mw:Q", title="Flow (MW)", format=",.0f"),
        alt.Tooltip("limit_mw:Q", title="Limit (MW)", format=",.0f"),
        alt.Tooltip("utilisation:Q", title="Utilisation (%)", format=",.0f"),
    ]
    band = alt.Chart(pd.DataFrame({"a": [tomorrow.isoformat()], "b": [x_end.isoformat()]})).mark_rect(
        color="#8a94a6", opacity=0.18).encode(x=alt.X("a:T", scale=alt.Scale(type="utc")), x2="b:T")
    flow = alt.Chart(g).mark_line(strokeWidth=1.5, color="#2f6fdf").encode(
        x=x, y=alt.Y("flow_mw:Q", title="MW", axis=alt.Axis(format="~s")), detail="segment:N")
    limit = alt.Chart(g[g["limit_mw"].notna()]).mark_line(
        strokeWidth=1.3, strokeDash=[4, 3], color="#d64545", interpolate="step-after").encode(
        x=x, y="limit_mw:Q", detail="segment:N")
    hover = alt.selection_point(fields=["t"], nearest=True, on="pointerover", empty=False)
    points = alt.Chart(g).mark_circle(size=40, color="#2f6fdf").encode(
        x=x, y="flow_mw:Q", opacity=alt.condition(hover, alt.value(1), alt.value(0)), tooltip=tooltip
    ).add_params(hover)
    return alt.layer(band, flow, limit, points).properties(height=180)


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
            st.altair_chart(group_chart(g, x_start, x_end, pd.Timestamp(tomorrow)), use_container_width=True)


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
