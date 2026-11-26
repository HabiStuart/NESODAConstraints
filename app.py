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
ASSETS = Path(__file__).parent / "assets"
# Map data, all in British National Grid metres (EPSG:27700):
#   etys_boundaries_2025.json  NESO ETYS boundary GIS data (March 2025)
#   land_outline.json          Natural Earth 1:50m coastline (public domain)
#   reference_map_lines.json   boundaries traced from the old reference map image

# Closest ETYS boundary for each day-ahead constraint group.
# Method: substations on each side of a group's line were read from NESO's October 2026
# day-ahead limit diagrams, then tested against the ETYS boundary lines in NESO's March 2025
# GIS data (which side of each line each substation falls). Substation positions are
# approximate. This is our analysis, not a NESO publication.
# Match: strong / good / partial / weak; boundary None = no ETYS boundary matches.
ETYS_MAP: dict[str, tuple[str | None, str, str]] = {
    "SSE_SP2": ("B4", "strong", "All 7 testable substations on matching sides. Formerly SSE_SP."),
    "SCOTEX": ("B6", "strong", "All 6 testable substations on matching sides: Scotland–England."),
    "SSEN_S": ("B2", "strong", "All 9 substations on matching sides."),
    "SSHARN3": ("B7a", "strong", "All 14 substations on matching sides. Formerly SSHARN, SHARN."),
    "FLOWSTH": ("B9", "good", "9 of 10 substations match; Enderby is south of FLOWSTH but north of B9."),
    "SEIMPPR23": ("B14", "partial", "Both enclose London; Iver and Laleham are inside B14 but outside SEIMPPR23. Formerly SEIMP."),
    "HARSPNBLY": ("B7", "partial", "Both cut Harker–Hutton; Spennymoor and Teesside are south of HARSPNBLY but north of B7."),
    "ESTEX": ("B15", "partial", "Same area (Grain, Kemsley, Medway); not tested against the GIS lines."),
    "ESTEX3": ("B15", "partial", "Same area (Grain, Kemsley, Medway); not tested against the GIS lines."),
    "ESTEX4": ("B15", "partial", "Same area (Grain, Kemsley, Medway); not tested against the GIS lines."),
    "SSE_GRMO": ("B5", "weak", "Consistent with B5, but B5 runs within 5 km of most of its substations, so it can't be confirmed."),
    "NKILGRMO": ("B5", "weak", "Consistent with B5, but B5 runs within 5 km of most of its substations, so it can't be confirmed."),
    "NKILGRMO1": ("B5", "weak", "Assumed to follow NKILGRMO; not drawn on NESO's diagram."),
    "DRESHEX1": (None, "", "Encloses the Humber plus Drax and Keadby; EC1 only cuts off the south Humber bank."),
    "GM_SNOW5A": (None, "", "Encloses north-west England and North Wales; no ETYS boundary separates its substations."),
    "CANDUNEX": (None, "", "Local Kent export boundary near the interconnector landings, inside the ETYS SC1–SC3 area."),
    "CANSELEX": (None, "", "Local Kent export boundary near the interconnector landings, inside the ETYS SC1–SC3 area."),
    "DUNSELEX": (None, "", "Local Kent export boundary near the interconnector landings, inside the ETYS SC1–SC3 area."),
    "NINSELEX": (None, "", "Local Kent/Sussex export boundary near the interconnector landings, inside the ETYS SC1–SC3 area."),
    "BOLSELEX1": (None, "", "Local Sussex/Kent export boundary near the interconnector landings, inside the ETYS SC1–SC3 area."),
}
LOCAL_SE = {"CANDUNEX", "CANSELEX", "DUNSELEX", "NINSELEX", "BOLSELEX1"}


def etys_label(group: str) -> str:
    boundary, match, _ = ETYS_MAP.get(group, (None, "", ""))
    if group not in ETYS_MAP:
        return "ETYS boundary: not yet mapped"
    if boundary is None:
        return "ETYS boundary: none (local boundary inside SC1–SC3)" if group in LOCAL_SE else "ETYS boundary: no match"
    return f"ETYS boundary: {boundary} ({match} match)"

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



# ---------------------------------------------------------------- boundary map
# Colours for the ETYS boundaries that match a constraint group well (strong or good).
MAP_HEIGHT = 880
MAP_WIDTH = round(MAP_HEIGHT * 810 / 1060)
MAP_COLOURS = {"B2": "#1f77b4", "B4": "#2ca02c", "B6": "#d62728", "B7a": "#9467bd", "B9": "#e377c2"}
# Old-map lines left off the map (they duplicate the current SSE_SP2 and SCOTEX lines)
OLD_MAP_HIDDEN = {"SSE-SP", "SCOTEX"}
# Pixel offsets that keep the old-map labels off their own lines
OLD_LABEL_SHIFT = {"SSE-SP": (-60, 22), "SCOTEX": (-62, 0), "SSHARN": (62, -8), "SWALEX": (-20, 28), "SEIMP": (-58, -30), "ESTEX": (58, -40)}
OLD_MAP_NOTES = {
    "SSE-SP": "Now SSE_SP2. Follows ETYS B4 through the central belt.",
    "SCOTEX": "Still SCOTEX. Runs beside ETYS B6 on the east side.",
    "SSHARN": "Now SSHARN3, which today follows ETYS B7a, well south of this line. Drawn before the April 2024 rename.",
    "SWALEX": "No longer published (data stops May 2023). Close to ETYS SW1.",
    "SEIMP": "Now SEIMPPR23, which today encloses London rather than following this line.",
    "ESTEX": "Still ESTEX (also ESTEX3, ESTEX4). Around the Thames estuary, near ETYS B15.",
}


@st.cache_data(show_spinner=False)
def load_map_data() -> tuple[dict, list, dict]:
    etys = json.loads((ASSETS / "etys_boundaries_2025.json").read_text())["boundaries"]
    land = json.loads((ASSETS / "land_outline.json").read_text())["rings"]
    old = json.loads((ASSETS / "reference_map_lines.json").read_text())["lines"]
    return etys, land, old


def boundary_map(show_all_etys: bool, show_old_map: bool) -> go.Figure:
    etys, land, old = load_map_data()
    # Reverse lookup: ETYS boundary -> constraint groups mapped to it
    by_boundary: dict[str, list[tuple[str, str, str]]] = {}
    for grp, (b, match, why) in ETYS_MAP.items():
        if b in etys:
            by_boundary.setdefault(b, []).append((grp, match, why))
    fig = go.Figure()
    for ring in land:
        xs, ys = zip(*ring)
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", fill="toself", fillcolor="rgba(140,150,165,0.16)",
                                 line=dict(color="rgba(120,130,145,0.55)", width=0.6), hoverinfo="skip", showlegend=False))
    if show_all_etys:
        for b, pts in etys.items():
            if b in MAP_COLOURS:
                continue
            xs, ys = zip(*pts)
            linked = by_boundary.get(b, [])
            hover = f"<b>ETYS {b}</b><br>" + ("<br>".join(f"{g}: {m} match" for g, m, _ in linked)
                                             if linked else "No day-ahead constraint group matches it")
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color="#8f99a6", width=1, dash="dot"),
                                     hovertemplate=hover + "<extra></extra>", showlegend=False))
            ex, ey = max(pts, key=lambda q: q[0])
            fig.add_annotation(x=ex + 6000, y=ey, text=b, showarrow=False, xanchor="left",
                               font=dict(size=9, color="#8f99a6"))
    for b, colour in MAP_COLOURS.items():
        pts = etys[b]
        xs, ys = zip(*pts)
        grp, match, why = by_boundary[b][0]
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color=colour, width=3),
                                 hovertemplate=f"<b>{grp}</b> ≈ ETYS {b} ({match} match)<br>{why}<extra></extra>",
                                 showlegend=False))
        ex, ey = max(pts, key=lambda q: q[0])
        nudge = {"B2": 30000, "B4": 0, "B6": -45000}.get(b, 0)
        fig.add_annotation(x=ex + 15000, y=ey + nudge, text=f"<b>{grp}</b><br>≈ ETYS {b}", showarrow=False,
                           xanchor="left", align="left", font=dict(size=12, color=colour))
    if show_old_map:
        for name, pts in old.items():
            if name in OLD_MAP_HIDDEN:
                continue
            xs, ys = zip(*pts)
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers", marker=dict(color="#e8710a", size=3),
                                     hovertemplate=f"<b>{name}</b> (old reference map)<br>{OLD_MAP_NOTES[name]}<extra></extra>",
                                     showlegend=False))
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            dx, dy = OLD_LABEL_SHIFT.get(name, (0, -12))
            fig.add_annotation(x=cx, y=cy, text=f"{name} (old)", showarrow=False, font=dict(size=10, color="#e8710a"),
                               bgcolor="rgba(255,255,255,0.8)", xshift=dx, yshift=dy)
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), showlegend=False, dragmode="pan",
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", hoverlabel=dict(align="left"))
    # Fixed size in the same proportions as the map extent (810 km x 1,060 km), so nothing is cropped
    # or stretched. Zoom and pan still work.
    fig.update_xaxes(range=[-30000, 780000], visible=False, fixedrange=False)
    fig.update_yaxes(range=[0, 1060000], visible=False, scaleanchor="x", scaleratio=1)
    fig.update_layout(width=MAP_WIDTH, height=MAP_HEIGHT)
    return fig


# ---------------------------------------------------------------- page
st.title("GB day-ahead constraint flows and limits")

map_col, notes_col = st.columns([3, 2], gap="large")
with notes_col:
    st.subheader("Where the constraints are")
    st.markdown(
        "Coloured lines are ETYS boundaries (NESO, March 2025) that closely match a day-ahead constraint group. "
        "They show roughly where each constraint sits; NESO's own day-ahead boundaries may differ in detail.\n\n"
        "Hover over a line for details. Scroll to zoom, drag to move, double-click to reset."
    )
    show_all = st.checkbox("Show all ETYS boundaries", value=False,
                           help="Planning boundaries from NESO's Electricity Ten Year Statement. Most have no matching day-ahead group.")
    show_old = st.checkbox("Show boundaries from the old reference map", value=False,
                           help="Traced from the map previously on this page. Some lines are out of date.")
    unmapped = sorted(g for g, (b, m, _) in ETYS_MAP.items() if b not in MAP_COLOURS or m not in ("strong", "good"))
    st.caption("Not drawn (no close ETYS match): " + ", ".join(unmapped) + ". "
               f"See NESO's [day-ahead network diagrams]({PORTAL_URL}) for their exact boundaries.")
with map_col:
    try:
        st.plotly_chart(boundary_map(show_all, show_old), use_container_width=False, theme=None,
                        config={"scrollZoom": True, "displaylogo": False,
                                "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d"]},
                        key="boundary-map")
    except (OSError, KeyError, ValueError) as exc:
        st.warning(f"The boundary map couldn't be drawn ({type(exc).__name__}: {exc}). Check the files in the assets folder.")


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
            st.caption(etys_label(grp), help=ETYS_MAP.get(grp, (None, "", "Not on NESO's day-ahead diagrams."))[2])
            st.plotly_chart(group_chart(g, x_start, x_end, pd.Timestamp(tomorrow)), use_container_width=True,
                            config={"displayModeBar": False}, key=f"chart-{grp}")


charts()

with st.expander("How the constraint groups relate to ETYS boundaries"):
    st.markdown(
        "Each group is matched to an ETYS boundary by reading which substations sit either side of its line "
        "on NESO's October 2026 day-ahead limit diagrams, then testing which side of each ETYS boundary "
        "(NESO's March 2025 GIS data) those substations fall. Substation positions are approximate, and "
        "substations within 5 km of a line are not counted. This is our analysis, not something NESO publishes.\n\n"
        "- **Strong:** every testable substation is on the matching side.\n"
        "- **Good:** all but one substation match.\n"
        "- **Partial:** a large part matches, but a named area falls on the other side.\n"
        "- **Weak:** consistent, but too few substations far enough from the line to confirm."
    )
    st.dataframe(
        pd.DataFrame(
            [{"Constraint group": g, "ETYS boundary": b or ("None (inside SC1–SC3)" if g in LOCAL_SE else "No match"), "Match": m.capitalize() or "—", "Why": why}
             for g, (b, m, why) in ETYS_MAP.items()]
        ),
        hide_index=True, use_container_width=True,
    )

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
