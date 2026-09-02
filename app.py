"""
TTIS CIF Explorer — Streamlit app
Run with:  streamlit run app.py
"""

import io
import zipfile
from collections import defaultdict
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="TTIS Train Frequency Explorer",
    page_icon="🚆",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

PASSENGER_STATUSES = {"P", "1", "5"}

WEEKDAY_COLOUR = "#2a78d6"
WEEKEND_COLOUR = "#eb6834"
BAR_COLOURS = [WEEKDAY_COLOUR] * 5 + [WEEKEND_COLOUR] * 2


# ---------------------------------------------------------------------------
# Core parsing logic (self-contained, no import from parse_ttis.py)
# ---------------------------------------------------------------------------

def _decode(raw: bytes) -> list[str]:
    try:
        return raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return raw.decode("latin-1").splitlines()


def _parse_yymmdd(s: str):
    if not s or len(s) < 6 or not s.strip():
        return None
    try:
        yy, mm, dd = int(s[0:2]), int(s[2:4]), int(s[4:6])
        return date(2000 + yy if yy < 60 else 1900 + yy, mm, dd)
    except (ValueError, TypeError):
        return None


def _days_active(days_run: str) -> set[int]:
    if not days_run or len(days_run) < 7:
        return set()
    return {i for i, ch in enumerate(days_run[:7]) if ch == "1"}


def _is_public_stop(activity: str) -> bool:
    acts = {activity[i:i + 2].strip() for i in range(0, min(len(activity), 12), 2)}
    return not (acts >= {"-D", "-U"})


def load_from_upload(uploaded_file) -> tuple[list[str], list[str] | None]:
    """
    Accept a Streamlit UploadedFile (zip, MCA, or MSN).
    Returns (cif_lines, msn_lines).
    """
    raw = uploaded_file.read()
    cif_lines = None
    msn_lines = None

    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                upper = name.upper()
                if upper.endswith(".MCA") and cif_lines is None:
                    cif_lines = _decode(zf.read(name))
                elif upper.endswith(".MSN") and msn_lines is None:
                    msn_lines = _decode(zf.read(name))
    else:
        name = uploaded_file.name.upper()
        if name.endswith(".MCA") or name.endswith(".CIF"):
            cif_lines = _decode(raw)
        elif name.endswith(".MSN"):
            msn_lines = _decode(raw)
        else:
            # Assume CIF if unknown
            cif_lines = _decode(raw)

    return cif_lines, msn_lines


def parse_msn(lines: list[str]) -> dict:
    stations = {}
    for line in lines:
        if len(line) < 39 or line[0] != "A":
            continue
        tiploc = line[28:35].strip()
        crs = line[36:39].strip()
        name = line[1:27].strip()
        if tiploc:
            stations[tiploc] = {"name": name, "crs": crs}
    return stations


def parse_cif(
    lines: list[str],
    passenger_only: bool = True,
    stp_include: set | None = None,
    progress_callback=None,
) -> tuple[list, dict]:
    if stp_include is None:
        stp_include = {"P", "O", "N"}

    tiploc_map: dict = {}
    schedules: list = []
    current_bs = None
    current_stops: list = []
    in_schedule = False
    total = len(lines)

    for idx, raw in enumerate(lines):
        if progress_callback and idx % 50_000 == 0:
            progress_callback(idx / total)

        line = raw.rstrip("\r\n")
        if len(line) < 2:
            continue
        rt = line[0:2]

        if rt == "TI":
            if len(line) < 56:
                continue
            tiploc = line[2:9].strip()
            name = line[18:44].strip()
            crs = line[53:56].strip()
            if tiploc and tiploc not in tiploc_map:
                tiploc_map[tiploc] = {"name": name, "crs": crs}

        elif rt == "BS":
            if in_schedule and current_bs and current_stops:
                current_bs["stops"] = current_stops
                schedules.append(current_bs)
            in_schedule = False
            current_stops = []
            current_bs = None

            if len(line) < 80 or line[2] == "D":
                continue

            stp = line[79]
            if stp not in stp_include:
                continue
            status = line[29]
            if passenger_only and status not in PASSENGER_STATUSES:
                continue

            current_bs = {
                "uid":       line[3:9].strip(),
                "date_from": _parse_yymmdd(line[9:15]),
                "date_to":   _parse_yymmdd(line[15:21]),
                "days_run":  line[21:28],
                "stp":       stp,
            }
            in_schedule = True

        elif rt == "LO" and in_schedule:
            tiploc = line[2:9].strip()
            if len(line) >= 19 and line[15:19].strip():
                current_stops.append(tiploc)

        elif rt == "LI" and in_schedule:
            tiploc = line[2:9].strip()
            pub_arr = line[25:29].strip() if len(line) >= 29 else ""
            pub_dep = line[29:33].strip() if len(line) >= 33 else ""
            activity = line[42:54] if len(line) >= 54 else ""
            if (pub_arr or pub_dep) and _is_public_stop(activity):
                current_stops.append(tiploc)

        elif rt == "LT" and in_schedule:
            tiploc = line[2:9].strip()
            pub_arr = line[15:19].strip() if len(line) >= 19 else ""
            if pub_arr:
                current_stops.append(tiploc)
            if current_stops:
                current_bs["stops"] = current_stops
                schedules.append(current_bs)
            in_schedule = False
            current_stops = []
            current_bs = None

        elif rt == "ZZ":
            if in_schedule and current_bs and current_stops:
                current_bs["stops"] = current_stops
                schedules.append(current_bs)
            break

    if progress_callback:
        progress_callback(1.0)
    return schedules, tiploc_map


def apply_stp(schedules: list) -> list:
    by_uid = defaultdict(list)
    for s in schedules:
        by_uid[s["uid"]].append(s)

    result = []
    for group in by_uid.values():
        cancellations = [s for s in group if s["stp"] == "C"]
        for s in group:
            if s["stp"] == "C":
                continue
            if s["stp"] == "P" and cancellations:
                p_from, p_to = s["date_from"], s["date_to"]
                suppressed = any(
                    c["date_from"] and c["date_to"] and p_from and p_to
                    and c["date_from"] <= p_to and c["date_to"] >= p_from
                    for c in cancellations
                )
                if suppressed:
                    continue
            result.append(s)
    return result


def count_calls(schedules: list) -> dict:
    counts = defaultdict(lambda: [0] * 7)
    for s in schedules:
        active = _days_active(s["days_run"])
        if not active:
            continue
        for tiploc in s.get("stops", []):
            for day_idx in active:
                counts[tiploc][day_idx] += 1
    return counts


def build_dataframe(counts: dict, tiploc_map: dict) -> pd.DataFrame:
    rows = []
    for tiploc, day_counts in counts.items():
        info = tiploc_map.get(tiploc, {"name": "", "crs": ""})
        total = sum(day_counts)
        rows.append({
            "tiploc":       tiploc,
            "crs":          info.get("crs", ""),
            "station_name": info.get("name", ""),
            **{DAYS[i]: day_counts[i] for i in range(7)},
            "weekly_total": total,
        })
    df = pd.DataFrame(rows)
    df = df.sort_values("weekly_total", ascending=False).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Chart helper
# ---------------------------------------------------------------------------

def make_bar_chart(row: pd.Series, station_label: str) -> go.Figure:
    values = [row[d] for d in DAYS]
    fig = go.Figure(go.Bar(
        x=DAY_LABELS,
        y=values,
        marker_color=BAR_COLOURS,
        text=values,
        textposition="outside",
        cliponaxis=False,
    ))
    fig.update_layout(
        title=dict(text=station_label, font=dict(size=15)),
        yaxis_title="Train calls",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(t=48, b=20, l=20, r=20),
        height=280,
        showlegend=False,
        yaxis=dict(gridcolor="rgba(128,128,128,0.15)", zeroline=False),
        xaxis=dict(fixedrange=True),
    )
    return fig


# ---------------------------------------------------------------------------
# Sidebar — upload & options
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🚆 TTIS Explorer")
    st.caption(
        "Upload a weekly TTIS zip from "
        "[National Rail Open Data](https://opendata.nationalrail.co.uk/) "
        "to explore train call frequencies across every UK station."
    )

    st.divider()
    st.subheader("Upload file")

    uploaded = st.file_uploader(
        "TTIS zip, MCA, or MSN file",
        type=["zip", "mca", "msn", "cif"],
        help="The weekly TTIS zip contains both the .MCA timetable and .MSN station names. "
             "You can also upload an .MCA and .MSN separately.",
        accept_multiple_files=True,
    )

    st.divider()
    st.subheader("Options")

    passenger_only = st.toggle("Passenger services only", value=True,
        help="When on, counts only train status P/1/5 (passenger & bus replacement). "
             "Turn off to include freight and ECS movements.")

    stp_options = st.multiselect(
        "STP indicators to include",
        options=["P", "O", "N"],
        default=["P", "O", "N"],
        help="P = Permanent, O = STP Overlay, N = New STP. "
             "Cancellations (C) are never counted.",
    )

    st.divider()
    st.subheader("Filter results")

    crs_only = st.toggle("Stations with CRS code only", value=True,
        help="Hides non-passenger TIPLOCs (junctions, depots, etc.) that have no 3-alpha code.")

    name_filter = st.text_input("Search station name / CRS", placeholder="e.g. Manchester")

    sort_col = st.selectbox("Sort by", ["weekly_total"] + DAYS,
        format_func=lambda c: c.replace("_", " ").title())

    top_n = st.slider("Show top N stations", min_value=10, max_value=500, value=100, step=10)


# ---------------------------------------------------------------------------
# Main area
# ---------------------------------------------------------------------------

if not uploaded:
    st.markdown("## Weekly train calls by station")
    st.info(
        "👈 Upload your TTIS zip in the sidebar to get started.\n\n"
        "Download the latest timetable from "
        "**[opendata.nationalrail.co.uk](https://opendata.nationalrail.co.uk/)** "
        "(free account required — look for the **Timetable** feed, weekly zip).",
        icon="🚉",
    )

    with st.expander("What does this tool do?"):
        st.markdown("""
The TTIS (Timetable Information Service) CIF file is published weekly by Network Rail
and contains the complete GB rail timetable for the next period.

This app parses it and counts how many **scheduled train services call at every station**,
broken down by day of week — across all 2,500+ UK stations in a single pass, with no API
rate limits.

**Output includes:**
- Mon–Sun call counts per station
- Weekly total
- Sortable, searchable table
- Per-station bar chart
- CSV download

**STP rules applied:** STP cancellations suppress their permanent counterparts;
overlays and new STP services are counted as independent services.
        """)
    st.stop()


# ---------------------------------------------------------------------------
# Parse uploaded files (cached on the file contents)
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def run_parse(file_bytes_map: dict, passenger_only: bool, stp_include_tuple: tuple) -> pd.DataFrame:
    """Cache key is the file bytes + options."""
    cif_lines = None
    msn_lines = None

    for name, raw in file_bytes_map.items():
        upper = name.upper()
        if upper.endswith(".ZIP"):
            if zipfile.is_zipfile(io.BytesIO(raw)):
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    for zname in zf.namelist():
                        zu = zname.upper()
                        if zu.endswith(".MCA") and cif_lines is None:
                            cif_lines = _decode(zf.read(zname))
                        elif zu.endswith(".MSN") and msn_lines is None:
                            msn_lines = _decode(zf.read(zname))
        elif upper.endswith(".MCA") or upper.endswith(".CIF"):
            cif_lines = _decode(raw)
        elif upper.endswith(".MSN"):
            msn_lines = _decode(raw)

    if cif_lines is None:
        raise ValueError("No .MCA CIF file found in the upload.")

    stp_include = set(stp_include_tuple)

    schedules, tiploc_map = parse_cif(cif_lines, passenger_only, stp_include)

    if msn_lines:
        msn_stations = parse_msn(msn_lines)
        for tiploc, info in msn_stations.items():
            if tiploc not in tiploc_map:
                tiploc_map[tiploc] = info
            else:
                if info["name"]:
                    tiploc_map[tiploc]["name"] = info["name"]
                if info["crs"]:
                    tiploc_map[tiploc]["crs"] = info["crs"]

    schedules = apply_stp(schedules)
    counts = count_calls(schedules)
    return build_dataframe(counts, tiploc_map)


# Collect uploaded files
file_bytes_map = {f.name: f.read() for f in uploaded}

with st.spinner("Parsing timetable… this takes 30–60 s for a full TTIS file"):
    try:
        df_full = run_parse(
            file_bytes_map,
            passenger_only,
            tuple(sorted(stp_options)) if stp_options else ("P",),
        )
    except ValueError as e:
        st.error(str(e))
        st.stop()


# ---------------------------------------------------------------------------
# Apply sidebar filters
# ---------------------------------------------------------------------------

df = df_full.copy()

if crs_only:
    df = df[df["crs"].str.strip() != ""]

if name_filter.strip():
    q = name_filter.strip().upper()
    df = df[
        df["station_name"].str.upper().str.contains(q, na=False)
        | df["crs"].str.upper().str.contains(q, na=False)
        | df["tiploc"].str.upper().str.contains(q, na=False)
    ]

df = df.sort_values(sort_col, ascending=False).head(top_n).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Summary metrics
# ---------------------------------------------------------------------------

st.markdown("## Weekly train calls by station")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Stations in results", f"{len(df):,}")
c2.metric("Total stations parsed", f"{len(df_full[df_full['crs'] != '']):,}")
c3.metric("Busiest station (weekly)", df.iloc[0]["station_name"] or df.iloc[0]["tiploc"] if len(df) else "—")
c4.metric("Busiest weekly total", f"{int(df.iloc[0]['weekly_total']):,}" if len(df) else "—")

st.divider()

# ---------------------------------------------------------------------------
# Main table
# ---------------------------------------------------------------------------

col_left, col_right = st.columns([3, 2], gap="large")

with col_left:
    st.subheader(f"Station table — top {min(top_n, len(df))} by {sort_col.replace('_', ' ')}")

    display_cols = ["crs", "station_name"] + DAYS + ["weekly_total"]
    rename_map = {
        "crs": "CRS",
        "station_name": "Station",
        "weekly_total": "Weekly",
        **{d: d[:3].title() for d in DAYS},
    }

    # Colour the day columns with a light heatmap
    styled = (
        df[display_cols]
        .rename(columns=rename_map)
        .style
        .background_gradient(
            subset=["Mon", "Tue", "Wed", "Thu", "Fri"],
            cmap="Blues", vmin=0,
        )
        .background_gradient(
            subset=["Sat", "Sun"],
            cmap="Oranges", vmin=0,
        )
        .background_gradient(
            subset=["Weekly"],
            cmap="Purples", vmin=0,
        )
        .format("{:,.0f}", subset=["Mon","Tue","Wed","Thu","Fri","Sat","Sun","Weekly"])
    )

    st.dataframe(styled, use_container_width=True, height=520)

    # CSV download
    csv_bytes = df_full.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="⬇ Download full CSV",
        data=csv_bytes,
        file_name="station_calls.csv",
        mime="text/csv",
    )

with col_right:
    st.subheader("Station detail")

    if len(df) == 0:
        st.info("No stations match the current filter.")
    else:
        options = df.apply(
            lambda r: f"{r['crs'] or r['tiploc']}  —  {r['station_name'] or r['tiploc']}",
            axis=1,
        ).tolist()
        selected_label = st.selectbox("Select a station", options, index=0)
        selected_idx = options.index(selected_label)
        row = df.iloc[selected_idx]

        station_label = f"{row['station_name'] or row['tiploc']}  ({row['crs'] or row['tiploc']})"
        st.plotly_chart(make_bar_chart(row, station_label), use_container_width=True)

        # Day breakdown table
        day_df = pd.DataFrame({
            "Day": DAY_LABELS,
            "Calls": [int(row[d]) for d in DAYS],
            "Type": ["Weekday"] * 5 + ["Weekend"] * 2,
        })
        max_calls = day_df["Calls"].max() or 1
        day_df["% of busiest day"] = (day_df["Calls"] / max_calls * 100).round(1)

        st.dataframe(
            day_df.style.format({"Calls": "{:,}", "% of busiest day": "{:.1f}%"}),
            use_container_width=True,
            hide_index=True,
        )

        # Mini stats
        m1, m2, m3 = st.columns(3)
        weekday_avg = int(sum(row[d] for d in DAYS[:5]) / 5)
        weekend_avg = int(sum(row[d] for d in DAYS[5:]) / 2)
        m1.metric("Weekday avg", f"{weekday_avg:,}")
        m2.metric("Weekend avg", f"{weekend_avg:,}")
        m3.metric("TIPLOC", row["tiploc"])


# ---------------------------------------------------------------------------
# Top 10 bar chart overview
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Busiest stations overview")

top10 = df.head(10)
if len(top10) > 0:
    labels = top10.apply(
        lambda r: r["crs"] or r["tiploc"], axis=1
    ).tolist()

    fig_overview = go.Figure()
    for i, day in enumerate(DAYS):
        fig_overview.add_trace(go.Bar(
            name=DAY_LABELS[i],
            x=labels,
            y=top10[day].tolist(),
            marker_color=BAR_COLOURS[i],
        ))

    fig_overview.update_layout(
        barmode="group",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        yaxis=dict(title="Train calls", gridcolor="rgba(128,128,128,0.15)"),
        margin=dict(t=32, b=20, l=20, r=20),
        height=380,
    )
    st.plotly_chart(fig_overview, use_container_width=True)
