"""
UK Train Frequency Explorer — Streamlit app
Parses Network Rail CIF timetable data and shows weekly call counts per station.

Run with:  streamlit run app.py
"""

import gzip
import io
import zipfile
from collections import defaultdict
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="UK Train Frequency Explorer",
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

# Network Rail CIF download endpoint
# type=CIF_ALL_FULL_DAILY / day=toc-full  → full weekly extract (gzipped CIF)
NR_CIF_URL = (
    "https://publicdatafeeds.networkrail.co.uk"
    "/ntrod/CifFileAuthenticate?type=CIF_ALL_FULL_DAILY&day=toc-full"
)


# ---------------------------------------------------------------------------
# Network Rail downloader
# ---------------------------------------------------------------------------

def fetch_nr_cif(username: str, password: str) -> bytes:
    """
    Download the full CIF from the Network Rail open data feed.
    Returns raw bytes (gzip-compressed CIF).
    Raises requests.HTTPError on auth failure or server error.
    """
    resp = requests.get(
        NR_CIF_URL,
        auth=(username, password),
        allow_redirects=True,
        timeout=300,   # large file — allow 5 min
        stream=False,
    )
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# CIF parsing helpers
# ---------------------------------------------------------------------------

def _decode(raw: bytes) -> list:
    try:
        return raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return raw.decode("latin-1").splitlines()


def _load_bytes(raw: bytes, filename: str = "") -> tuple:
    """
    Accept raw bytes that may be:
      - a gzip-compressed CIF (.gz)
      - a zip archive (containing .MCA + .MSN)
      - a plain CIF / MSN text file
    Returns (cif_lines, msn_lines).
    """
    cif_lines = None
    msn_lines = None
    name_upper = filename.upper()

    # gzip → unwrap first
    if name_upper.endswith(".GZ") or (raw[:2] == b"\x1f\x8b"):
        raw = gzip.decompress(raw)
        name_upper = name_upper.removesuffix(".GZ")

    # zip archive
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for zname in zf.namelist():
                zu = zname.upper()
                if zu.endswith(".MCA") and cif_lines is None:
                    cif_lines = _decode(zf.read(zname))
                elif zu.endswith(".MSN") and msn_lines is None:
                    msn_lines = _decode(zf.read(zname))
        return cif_lines, msn_lines

    # plain text — decide by extension
    text_lines = _decode(raw)
    if name_upper.endswith(".MSN"):
        return None, text_lines
    # default: treat as CIF / MCA
    return text_lines, None


def _parse_yymmdd(s: str):
    if not s or len(s) < 6 or not s.strip():
        return None
    try:
        yy, mm, dd = int(s[0:2]), int(s[2:4]), int(s[4:6])
        return date(2000 + yy if yy < 60 else 1900 + yy, mm, dd)
    except (ValueError, TypeError):
        return None


def _days_active(days_run: str) -> set:
    if not days_run or len(days_run) < 7:
        return set()
    return {i for i, ch in enumerate(days_run[:7]) if ch == "1"}


def _is_public_stop(activity: str) -> bool:
    acts = {activity[i:i + 2].strip() for i in range(0, min(len(activity), 12), 2)}
    return not (acts >= {"-D", "-U"})


def parse_msn(lines: list) -> dict:
    stations = {}
    for line in lines:
        if len(line) < 39 or line[0] != "A":
            continue
        tiploc = line[28:35].strip()
        crs    = line[36:39].strip()
        name   = line[1:27].strip()
        if tiploc:
            stations[tiploc] = {"name": name, "crs": crs}
    return stations


def parse_cif(lines: list, passenger_only: bool = True,
              stp_include: set = None) -> tuple:
    if stp_include is None:
        stp_include = {"P", "O", "N"}

    tiploc_map: dict = {}
    schedules: list = []
    current_bs = None
    current_stops: list = []
    in_schedule = False

    for raw in lines:
        line = raw.rstrip("\r\n")
        if len(line) < 2:
            continue
        rt = line[0:2]

        if rt == "TI":
            if len(line) < 56:
                continue
            tiploc = line[2:9].strip()
            name   = line[18:44].strip()
            crs    = line[53:56].strip()
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
            stp    = line[79]
            status = line[29]
            if stp not in stp_include:
                continue
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
            tiploc  = line[2:9].strip()
            pub_arr = line[25:29].strip() if len(line) >= 29 else ""
            pub_dep = line[29:33].strip() if len(line) >= 33 else ""
            activity = line[42:54] if len(line) >= 54 else ""
            if (pub_arr or pub_dep) and _is_public_stop(activity):
                current_stops.append(tiploc)

        elif rt == "LT" and in_schedule:
            tiploc  = line[2:9].strip()
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
        info  = tiploc_map.get(tiploc, {"name": "", "crs": ""})
        total = sum(day_counts)
        rows.append({
            "tiploc":       tiploc,
            "crs":          info.get("crs", ""),
            "station_name": info.get("name", ""),
            **{DAYS[i]: day_counts[i] for i in range(7)},
            "weekly_total": total,
        })
    df = pd.DataFrame(rows)
    return df.sort_values("weekly_total", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Cached parse (keyed on raw bytes + options)
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def run_parse(file_map: dict, passenger_only: bool,
              stp_tuple: tuple) -> pd.DataFrame:
    cif_lines = None
    msn_lines = None

    for fname, raw in file_map.items():
        cl, ml = _load_bytes(raw, fname)
        if cl is not None and cif_lines is None:
            cif_lines = cl
        if ml is not None and msn_lines is None:
            msn_lines = ml

    if cif_lines is None:
        raise ValueError("No CIF data found. Expected a .gz, .zip (.MCA inside), or .MCA file.")

    schedules, tiploc_map = parse_cif(cif_lines, passenger_only, set(stp_tuple))

    if msn_lines:
        for tiploc, info in parse_msn(msn_lines).items():
            if tiploc not in tiploc_map:
                tiploc_map[tiploc] = info
            else:
                if info["name"]: tiploc_map[tiploc]["name"] = info["name"]
                if info["crs"]:  tiploc_map[tiploc]["crs"]  = info["crs"]

    schedules = apply_stp(schedules)
    return build_dataframe(count_calls(schedules), tiploc_map)


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------

def make_bar_chart(row: pd.Series, label: str) -> go.Figure:
    values = [row[d] for d in DAYS]
    fig = go.Figure(go.Bar(
        x=DAY_LABELS, y=values,
        marker_color=BAR_COLOURS,
        text=values, textposition="outside", cliponaxis=False,
    ))
    fig.update_layout(
        title=dict(text=label, font=dict(size=15)),
        yaxis_title="Train calls",
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(t=48, b=20, l=20, r=20), height=280,
        showlegend=False,
        yaxis=dict(gridcolor="rgba(128,128,128,0.15)", zeroline=False),
        xaxis=dict(fixedrange=True),
    )
    return fig


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🚆 Train Frequency Explorer")

    # ---- Data source tabs ----
    src_tab, opt_tab, filter_tab = st.tabs(["📥 Data", "⚙️ Options", "🔍 Filter"])

    with src_tab:
        st.markdown("**Source**")
        source = st.radio(
            "Get data from",
            ["Network Rail (auto-download)", "Upload file"],
            label_visibility="collapsed",
        )

        file_map = {}   # fname → bytes, populated by whichever source is chosen

        if source == "Network Rail (auto-download)":
            st.caption(
                "Downloads the full CIF directly from "
                "[publicdatafeeds.networkrail.co.uk](https://publicdatafeeds.networkrail.co.uk). "
                "Use your existing Network Rail open data account."
            )

            # Pull from secrets.toml if present, otherwise show input fields
            _secrets = st.secrets.get("network_rail", {})
            _user_from_secret = _secrets.get("username", "")
            _pass_from_secret = _secrets.get("password", "")

            if _user_from_secret and _pass_from_secret:
                nr_user = _user_from_secret
                nr_pass = _pass_from_secret
                st.success("Credentials loaded from secrets.toml", icon="🔑")
            else:
                nr_user = st.text_input("Username (email)", key="nr_user")
                nr_pass = st.text_input("Password", type="password", key="nr_pass")

            do_download = st.button("Download & parse", type="primary",
                                    disabled=not (nr_user and nr_pass))

            if do_download:
                with st.spinner("Downloading CIF from Network Rail (~50 MB)…"):
                    try:
                        raw = fetch_nr_cif(nr_user, nr_pass)
                        st.session_state["nr_raw"] = raw
                        st.session_state["nr_fname"] = "CIF_ALL_FULL_DAILY.gz"
                        st.success(f"Downloaded {len(raw)/1e6:.1f} MB")
                    except requests.HTTPError as e:
                        if e.response is not None and e.response.status_code == 401:
                            st.error("Authentication failed — check your username and password.")
                        else:
                            st.error(f"Download failed: {e}")
                    except Exception as e:
                        st.error(f"Download failed: {e}")

            if "nr_raw" in st.session_state:
                file_map[st.session_state["nr_fname"]] = st.session_state["nr_raw"]
                st.info(f"Using downloaded CIF ({len(st.session_state['nr_raw'])/1e6:.1f} MB)", icon="✅")

        else:
            st.caption(
                "Upload the TTIS zip from NRDP, the Network Rail .gz, "
                "or a raw .MCA file. A separate .MSN can be added too."
            )
            uploaded = st.file_uploader(
                "CIF file(s)",
                type=["zip", "gz", "mca", "cif", "msn"],
                accept_multiple_files=True,
            )
            if uploaded:
                for f in uploaded:
                    file_map[f.name] = f.read()

    with opt_tab:
        st.markdown("**Parse options**")
        passenger_only = st.toggle("Passenger services only", value=True,
            help="Status P/1/5 only. Off = include freight and ECS.")
        stp_options = st.multiselect(
            "STP indicators",
            options=["P", "O", "N"],
            default=["P", "O", "N"],
            help="P=Permanent, O=Overlay, N=New STP. Cancellations (C) never counted.",
        )

    with filter_tab:
        st.markdown("**Filter & sort**")
        crs_only    = st.toggle("Stations with CRS code only", value=True,
            help="Hides junctions, depots and other non-passenger TIPLOCs.")
        name_filter = st.text_input("Search name / CRS / TIPLOC",
                                    placeholder="e.g. Edinburgh, EDB")
        sort_col    = st.selectbox("Sort by",
                                   ["weekly_total"] + DAYS,
                                   format_func=lambda c: c.replace("_"," ").title())
        top_n       = st.slider("Show top N", 10, 500, 100, step=10)


# ---------------------------------------------------------------------------
# Main area
# ---------------------------------------------------------------------------

if not file_map:
    st.markdown("## UK Train Frequency Explorer")
    st.info(
        "👈 Choose a data source in the sidebar to get started.\n\n"
        "**Auto-download** uses your [Network Rail open data](https://publicdatafeeds.networkrail.co.uk) "
        "credentials to fetch the latest full CIF automatically.\n\n"
        "**Upload** accepts the weekly TTIS zip from NRDP, a Network Rail `.gz`, or a raw `.MCA` file.",
        icon="🚉",
    )
    with st.expander("What does this app do?"):
        st.markdown("""
Parses the GB national rail timetable (CIF format) and counts how many
**scheduled train services call at every station**, broken down by day of week —
across all 2,500+ UK stations in a single pass, with no API rate limits.

| Column | Meaning |
|---|---|
| Mon – Sun | Scheduled services calling that day |
| Weekly total | Sum across all 7 days |

STP cancellations suppress their permanent counterparts; overlays and new STP
services are counted independently. Only public passenger stops are counted
by default (origin, calling point, or terminus with a published time).
        """)
    st.stop()

# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

with st.spinner("Parsing timetable… 30–60 s for a full CIF"):
    try:
        df_full = run_parse(
            file_map,
            passenger_only,
            tuple(sorted(stp_options)) if stp_options else ("P",),
        )
    except ValueError as e:
        st.error(str(e))
        st.stop()

# ---------------------------------------------------------------------------
# Apply filters
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
# Metrics
# ---------------------------------------------------------------------------

st.markdown("## Weekly train calls by station")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Stations shown",        f"{len(df):,}")
c2.metric("Total stations parsed", f"{len(df_full[df_full['crs'] != '']):,}")
c3.metric("Busiest station",
          df.iloc[0]["station_name"] or df.iloc[0]["tiploc"] if len(df) else "—")
c4.metric("Busiest weekly total",
          f"{int(df.iloc[0]['weekly_total']):,}" if len(df) else "—")

st.divider()

# ---------------------------------------------------------------------------
# Table + station detail
# ---------------------------------------------------------------------------

col_left, col_right = st.columns([3, 2], gap="large")

with col_left:
    st.subheader(f"Top {min(top_n, len(df))} stations — sorted by {sort_col.replace('_',' ')}")

    display_cols = ["crs", "station_name"] + DAYS + ["weekly_total"]
    rename_map   = {"crs": "CRS", "station_name": "Station", "weekly_total": "Weekly",
                    **{d: d[:3].title() for d in DAYS}}

    styled = (
        df[display_cols].rename(columns=rename_map).style
        .background_gradient(subset=["Mon","Tue","Wed","Thu","Fri"],
                             cmap="Blues", vmin=0)
        .background_gradient(subset=["Sat","Sun"],
                             cmap="Oranges", vmin=0)
        .background_gradient(subset=["Weekly"],
                             cmap="Purples", vmin=0)
        .format("{:,.0f}",
                subset=["Mon","Tue","Wed","Thu","Fri","Sat","Sun","Weekly"])
    )
    st.dataframe(styled, use_container_width=True, height=520)

    csv_bytes = df_full.to_csv(index=False).encode("utf-8")
    st.download_button("⬇ Download full CSV", csv_bytes,
                       "station_calls.csv", "text/csv")

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
        row = df.iloc[options.index(selected_label)]

        label = f"{row['station_name'] or row['tiploc']}  ({row['crs'] or row['tiploc']})"
        st.plotly_chart(make_bar_chart(row, label), use_container_width=True)

        day_df = pd.DataFrame({
            "Day":   DAY_LABELS,
            "Calls": [int(row[d]) for d in DAYS],
            "Type":  ["Weekday"] * 5 + ["Weekend"] * 2,
        })
        max_calls = day_df["Calls"].max() or 1
        day_df["% of peak"] = (day_df["Calls"] / max_calls * 100).round(1)
        st.dataframe(
            day_df.style.format({"Calls": "{:,}", "% of peak": "{:.1f}%"}),
            use_container_width=True, hide_index=True,
        )

        m1, m2, m3 = st.columns(3)
        m1.metric("Weekday avg", f"{int(sum(row[d] for d in DAYS[:5]) / 5):,}")
        m2.metric("Weekend avg", f"{int(sum(row[d] for d in DAYS[5:]) / 2):,}")
        m3.metric("TIPLOC", row["tiploc"])

# ---------------------------------------------------------------------------
# Overview chart — top 10
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Busiest stations overview")

top10 = df.head(10)
if len(top10) > 0:
    labels = top10.apply(lambda r: r["crs"] or r["tiploc"], axis=1).tolist()
    fig = go.Figure()
    for i, day in enumerate(DAYS):
        fig.add_trace(go.Bar(
            name=DAY_LABELS[i], x=labels,
            y=top10[day].tolist(),
            marker_color=BAR_COLOURS[i],
        ))
    fig.update_layout(
        barmode="group",
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
        yaxis=dict(title="Train calls", gridcolor="rgba(128,128,128,0.15)"),
        margin=dict(t=32, b=20, l=20, r=20), height=380,
    )
    st.plotly_chart(fig, use_container_width=True)
