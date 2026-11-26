# GB day-ahead constraint flows and limits (Streamlit)

Shows NESO's day-ahead forecast flow against limit for each constraint group, from 14 days ago
through tomorrow. Groups over their limit tomorrow are listed first.

Data: NESO Data Portal, "Day Ahead Constraint Flows and Limits" (public API, no key).

## How it stays up to date
- An open page re-checks NESO every 10 minutes (`CHECK_EVERY` in `app.py`).
- It only re-downloads the data when NESO's "last modified" time changes.
- Until NESO publishes tomorrow (typically around 16:00 UK time), the page says so and orders
  the charts by the latest published day instead.

## Run it on your own machine
1. Install Python 3.11 or newer.
2. Open a terminal in this folder.
3. Create a virtual environment: `python -m venv .venv`
4. Activate it:
   - macOS/Linux: `source .venv/bin/activate`
   - Windows: `.venv\Scripts\activate`
5. Install the pinned packages: `pip install -r requirements.txt`
6. Start the app: `streamlit run app.py`
7. Open the address it prints (usually http://localhost:8501).

## Hosting for colleagues
Hosting it where colleagues can reach it is a Tech decision (an always-on service).
It needs outbound HTTPS to `api.neso.energy` only, holds no credentials, and stores nothing on disk.

## Assumptions (also shown in the app)
- Limits of exactly 9,999, 10,000 or 99,999 MW are treated as "no limit published" (inferred).
- Autumn clock change: NESO lists the repeated hour as adjacent pairs; the first is taken as BST.
- Exact duplicate rows are dropped; missing half-hours are shown as gaps.

## Boundary map data (in `assets/`)
- `etys_boundaries_2025.json`: NESO ETYS boundary GIS data (March 2025), converted to British National Grid.
- `land_outline.json`: Natural Earth 1:50m coastline (public domain).
- `reference_map_lines.json`: boundaries traced from the old reference map image (alignment about 3 km; the lines were hand-drawn).
- The map draws only ETYS boundaries that closely match a day-ahead constraint group (`ETYS_MAP` in `app.py`).
  This matching is our own analysis, not a NESO publication.
