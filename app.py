"""
UK Train Frequency Explorer
Run: streamlit run app.py
"""

import gzip
import hashlib
import io
import traceback
import zipfile
from collections import Counter, defaultdict
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

st.set_page_config(
    page_title="UK Train Frequency Explorer",
    page_icon="🚆",
    layout="wide",
    initial_sidebar_state="expanded",
)

DAYS       = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
DAY_LABELS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
PASSENGER_STATUSES = {"P","1","5"}
BAR_COLOURS = ["#2a78d6"]*5 + ["#eb6834"]*2

# Path to the bundled station reference file (committed to the repo)
import os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
STATIONS_XML_PATH = _os.path.join(_HERE, "StationsRefData.xml")

NR_URL = (
    "https://publicdatafeeds.networkrail.co.uk"
    "/ntrod/CifFileAuthenticate?type=CIF_ALL_FULL_DAILY&day=toc-full.CIF.gz"
)

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _decode(raw):
    try:
        return raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return raw.decode("latin-1").splitlines()

def _open_cif_stream(raw, filename=""):
    """
    Return (cif_iterator, msn_lines_or_None).
    The cif_iterator yields one line at a time without ever holding
    the full decompressed content in memory.
    """
    name = filename.upper()

    # gzip — stream directly, zero full-decompression cost
    if name.endswith(".GZ") or raw[:2] == b"\x1f\x8b":
        print("DEBUG: opening as gzip stream")
        def _gz_iter():
            with gzip.open(io.BytesIO(raw), "rt",
                           encoding="latin-1", errors="replace") as fh:
                for line in fh:
                    yield line.rstrip("\n\r")
        return _gz_iter(), None

    # zip archive — MSN can be small so we decode it fully; MCA is streamed
    if zipfile.is_zipfile(io.BytesIO(raw)):
        print("DEBUG: opening as zip")
        cif_iter = None
        msn_lines = None
        zf = zipfile.ZipFile(io.BytesIO(raw))  # kept open; closed after parse
        for zname in zf.namelist():
            zu = zname.upper()
            if zu.endswith(".MCA") and cif_iter is None:
                mf = zf.open(zname)
                wrapper = io.TextIOWrapper(mf, encoding="latin-1", errors="replace")
                cif_iter = (line.rstrip("\n\r") for line in wrapper)
            elif zu.endswith(".MSN") and msn_lines is None:
                msn_lines = _decode(zf.read(zname))
        return cif_iter, msn_lines

    # XML station reference — handled separately in run_parse, skip here
    if name.endswith(".XML"):
        return None, None

    # plain text already in memory
    print("DEBUG: opening as plain text")
    lines = _decode(raw)
    if name.endswith(".MSN"):
        return iter([]), lines
    return iter(lines), None

def _parse_date(s):
    if not s or len(s) < 6 or not s.strip():
        return None
    try:
        yy, mm, dd = int(s[0:2]), int(s[2:4]), int(s[4:6])
        return date(2000+yy if yy < 60 else 1900+yy, mm, dd)
    except Exception:
        return None

def _active_days(days_run):
    if not days_run or len(days_run) < 7:
        return set()
    return {i for i, ch in enumerate(days_run[:7]) if ch == "1"}

def _public_stop(activity):
    acts = {activity[i:i+2].strip() for i in range(0, min(len(activity),12), 2)}
    return not (acts >= {"-D","-U"})

def parse_msn(lines):
    out = {}
    for line in lines:
        if len(line) < 39 or line[0] != "A":
            continue
        t = line[28:35].strip()
        if t:
            out[t] = {"name": line[1:27].strip(), "crs": line[36:39].strip()}
    return out

def parse_stations_xml(raw):
    """
    Parse the National Rail StationsRefData.xml.
    Returns a set of TIPLOCs that are real passenger stations
    (have both a TIPLOC and a CRS code in the reference data).
    Also returns a dict of tiploc -> {name, crs} for name overrides.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        print("DEBUG parse_stations_xml: failed to parse XML: {}".format(e))
        return set(), {}
    valid = set()
    names = {}
    for station in root.findall("Station"):
        tiploc = (station.findtext("Tiploc") or "").strip()
        crs    = (station.findtext("CRS") or "").strip()
        name   = (station.findtext("Name") or "").strip()
        if tiploc and crs:
            valid.add(tiploc)
            names[tiploc] = {"name": name, "crs": crs}
    print("DEBUG parse_stations_xml: {} valid station TIPLOCs".format(len(valid)))
    return valid, names


def parse_cif(line_iter, passenger_only=True, stp_include=None):
    if stp_include is None:
        stp_include = {"P","O","N"}
    tiploc_map, schedules = {}, []
    cur, stops, active = None, [], False
    file_date = None   # extracted from HD record

    for raw in line_iter:
        line = raw.rstrip("\r\n")
        if len(line) < 2:
            continue
        rt = line[0:2]

        # HD record: file header — extract the file reference date
        # HD layout: [0:2]=HD, [2:22]=file mainframe ID, [22:28]=date (DDMMYY), [28:34]=time
        if rt == "HD":
            if len(line) >= 28 and file_date is None:
                raw_date = line[22:28].strip()
                if len(raw_date) == 6:
                    try:
                        dd, mm, yy = int(raw_date[0:2]), int(raw_date[2:4]), int(raw_date[4:6])
                        year = 2000 + yy if yy < 60 else 1900 + yy
                        file_date = date(year, mm, dd)
                        print("DEBUG HD: file_date={}".format(file_date))
                    except Exception:
                        pass

        elif rt == "TI":
            if len(line) >= 56:
                t = line[2:9].strip()
                if t and t not in tiploc_map:
                    tiploc_map[t] = {"name": line[18:44].strip(), "crs": line[53:56].strip()}

        elif rt == "BS":
            if active and cur and stops:
                cur["stops"] = stops; schedules.append(cur)
            cur, stops, active = None, [], False
            if len(line) < 30 or line[2] == "D":
                continue
            stp    = line[79].strip() if len(line) >= 80 else "P"
            status = line[29].strip()
            if stp not in stp_include:
                continue
            if passenger_only and status not in PASSENGER_STATUSES:
                continue
            cur = {"uid":       line[3:9].strip(),
                   "date_from": _parse_date(line[9:15]),
                   "date_to":   _parse_date(line[15:21]),
                   "days_run":  line[21:28],
                   "stp":       stp}
            active = True

        elif rt == "LO" and active:
            t = line[2:9].strip()
            if len(line) >= 19 and line[15:19].strip():
                stops.append(t)

        elif rt == "LI" and active:
            t = line[2:9].strip()
            pa  = line[25:29].strip() if len(line) >= 29 else ""
            pd_ = line[29:33].strip() if len(line) >= 33 else ""
            act = line[42:54]         if len(line) >= 54 else ""
            if (pa or pd_) and _public_stop(act):
                stops.append(t)

        elif rt == "LT" and active:
            t  = line[2:9].strip()
            pa = line[15:19].strip() if len(line) >= 19 else ""
            if pa:
                stops.append(t)
            if stops:
                cur["stops"] = stops; schedules.append(cur)
            cur, stops, active = None, [], False

        elif rt == "ZZ":
            if active and cur and stops:
                cur["stops"] = stops; schedules.append(cur)
            break

    return schedules, tiploc_map, file_date

def _week_dates(ref_date):
    monday = ref_date - timedelta(days=ref_date.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def apply_stp(schedules):
    # Remove STP cancellations; keep P, O, N.
    # Overlay suppression is handled day-by-day in count_calls so a
    # P running Mon-Fri is not dropped just because an O covers Wednesday.
    return [s for s in schedules if s["stp"] != "C"]


def count_calls(schedules, ref_date=None):
    # No date context: simple bitmask count, no STP resolution
    if ref_date is None:
        counts = defaultdict(lambda: [0]*7)
        for s in schedules:
            for day_idx in _active_days(s["days_run"]):
                for t in s.get("stops", []):
                    counts[t][day_idx] += 1
        return counts

    week = _week_dates(ref_date)

    # Group by UID for per-day STP resolution
    by_uid = defaultdict(list)
    for s in schedules:
        by_uid[s["uid"]].append(s)

    counts = defaultdict(lambda: [0]*7)

    for uid, group in by_uid.items():
        p_scheds = [s for s in group if s["stp"] == "P"]
        o_scheds = [s for s in group if s["stp"] in ("O", "N")]

        for day_idx, day_date in enumerate(week):

            def runs_on(s, day_idx=day_idx, day_date=day_date):
                return (
                    day_idx in _active_days(s["days_run"])
                    and (s["date_from"] is None or s["date_from"] <= day_date)
                    and (s["date_to"]   is None or s["date_to"]   >= day_date)
                )

            # Which O/N schedules actually run on this date?
            active_o = [s for s in o_scheds if runs_on(s)]

            # Count O/N calls
            for s in active_o:
                for t in s.get("stops", []):
                    counts[t][day_idx] += 1

            # Count P calls only where no O/N is running for this UID/day
            if not active_o:
                for s in p_scheds:
                    if runs_on(s):
                        for t in s.get("stops", []):
                            counts[t][day_idx] += 1

    return counts


def build_df(counts, tiploc_map, station_tiplocs=None):
    rows = []
    for t, dc in counts.items():
        # If an XML station list was provided, skip TIPLOCs not in it
        if station_tiplocs is not None and t not in station_tiplocs:
            continue
        info = tiploc_map.get(t, {"name":"","crs":""})
        rows.append({"tiploc": t,
                     "crs": info.get("crs",""),
                     "station_name": info.get("name",""),
                     **{DAYS[i]: dc[i] for i in range(7)},
                     "weekly_total": sum(dc)})
    if not rows:
        cols = ["tiploc","crs","station_name"] + DAYS + ["weekly_total"]
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows).sort_values("weekly_total", ascending=False).reset_index(drop=True)

def _hash_file_map(file_map):
    h = hashlib.md5()
    for fname in sorted(file_map.keys()):
        h.update(fname.encode())
        h.update(file_map[fname][:65536])
        h.update(str(len(file_map[fname])).encode())
    return h.hexdigest()

def run_parse(file_hash, file_map, passenger_only, stp_tuple, debug_lookup=""):
    print("DEBUG run_parse: start hash={} files={}".format(
        file_hash, list(file_map.keys())))
    cif_iter = None
    msn_lines = None

    for fname, raw in file_map.items():
        print("DEBUG run_parse: opening stream for {}, size={}, first_bytes={}".format(
            fname, len(raw), raw[:16].hex()))
        # Peek at content to confirm format
        try:
            import gzip as _gz
            sample = _gz.decompress(raw[:4096]) if raw[:2] == b"\x1f\x8b" else raw[:4096]
            print("DEBUG first 200 chars of content: {!r}".format(sample[:200].decode("latin-1")))
        except Exception as pe:
            print("DEBUG peek failed: {}".format(pe))
        ci, ml = _open_cif_stream(raw, fname)
        if ci is not None and cif_iter is None:
            cif_iter = ci
        if ml is not None and msn_lines is None:
            msn_lines = ml

    # Extract XML station reference if provided; fall back to bundled file
    station_tiplocs = None
    station_names_xml = {}
    for fname, raw in file_map.items():
        if fname.upper().endswith(".XML"):
            station_tiplocs, station_names_xml = parse_stations_xml(raw)
            print("DEBUG: loaded {} station TIPLOCs from uploaded XML".format(len(station_tiplocs)))
            break
    if station_tiplocs is None and _bundled_station_tiplocs is not None:
        station_tiplocs  = _bundled_station_tiplocs
        station_names_xml = _bundled_station_names
        print("DEBUG: using {} station TIPLOCs from bundled XML".format(len(station_tiplocs)))

    if cif_iter is None:
        raise ValueError("No CIF data found in uploaded file.")

    import sys, itertools
    print("DEBUG run_parse: parse_cif streaming start", flush=True, file=sys.stderr)
    # Peek at first few lines to confirm CIF format
    cif_iter, peek_iter = itertools.tee(cif_iter)
    for i, ln in enumerate(peek_iter):
        print("DEBUG CIF line {}: {!r}".format(i, ln[:80]), flush=True, file=sys.stderr)
        if i >= 4:
            break
    schedules, tiploc_map, file_date = parse_cif(cif_iter, passenger_only, set(stp_tuple))
    print("DEBUG file_date={}".format(file_date))
    import sys
    print("DEBUG run_parse: parse_cif done, schedules={} tiplocs={}".format(
        len(schedules), len(tiploc_map)), flush=True, file=sys.stderr)
    # Show STP distribution so we can diagnose filtering issues
    stp_counts = Counter(s["stp"] for s in schedules)
    status_counts = Counter(s.get("status","?") for s in schedules)
    print("DEBUG STP distribution: {}".format(dict(stp_counts)), flush=True, file=sys.stderr)
    print("DEBUG Status distribution: {}".format(dict(status_counts)), flush=True, file=sys.stderr)
    if schedules:
        sample = schedules[0]
        print("DEBUG First schedule: uid={} days={} stp={} stops_count={}".format(
            sample.get("uid"), sample.get("days_run"), sample.get("stp"),
            len(sample.get("stops",[]))), flush=True, file=sys.stderr)
    else:
        print("DEBUG WARNING: zero schedules parsed!", flush=True, file=sys.stderr)

    # --- Manual station diagnostic -----------------------------------
    if debug_lookup.strip():
        q = debug_lookup.strip().upper()
        matched_tiplocs = set()
        for t, info in tiploc_map.items():
            if q == t.upper() or q == (info.get("crs") or "").upper() \
               or q in (info.get("name") or "").upper():
                matched_tiplocs.add(t)
        # Also catch it even if not in tiploc_map at all (raw TIPLOC match)
        matched_tiplocs.add(q)

        print("=" * 70)
        print("DIAGNOSTIC LOOKUP: '{}' matched TIPLOCs: {}".format(
            debug_lookup, matched_tiplocs))
        print("=" * 70)

        relevant = [s for s in schedules
                    if any(t in matched_tiplocs for t in s.get("stops", []))]
        print("Found {} raw schedules (pre-STP) calling at matched TIPLOCs".format(
            len(relevant)))

        stp_dist = Counter(s["stp"] for s in relevant)
        print("STP distribution among these: {}".format(dict(stp_dist)))

        for s in sorted(relevant, key=lambda x: (x["uid"], x["stp"]))[:60]:
            print("  uid={:8s} stp={} days={} from={} to={} n_stops={} "
                  "has_target={}".format(
                s["uid"], s["stp"], s["days_run"],
                s["date_from"], s["date_to"],
                len(s.get("stops", [])),
                any(t in matched_tiplocs for t in s.get("stops", []))
            ))
        if len(relevant) > 60:
            print("  ... and {} more".format(len(relevant) - 60))
        print("=" * 70)
    # --------------------------------------------------------------------

    if msn_lines:
        print("DEBUG run_parse: merging MSN")
        for t, info in parse_msn(msn_lines).items():
            if t not in tiploc_map:
                tiploc_map[t] = info
            else:
                if info["name"]: tiploc_map[t]["name"] = info["name"]
                if info["crs"]:  tiploc_map[t]["crs"]  = info["crs"]

    # XML names take highest priority — they're the official public-facing names
    if station_names_xml:
        for t, info in station_names_xml.items():
            if t not in tiploc_map:
                tiploc_map[t] = info
            else:
                if info["name"]: tiploc_map[t]["name"] = info["name"]
                if info["crs"]:  tiploc_map[t]["crs"]  = info["crs"]

    print("DEBUG run_parse: apply_stp + count_calls + build_df")
    result = build_df(
        count_calls(apply_stp(schedules), ref_date=file_date),
        tiploc_map,
        station_tiplocs=station_tiplocs,
    )
    print("DEBUG run_parse: done, rows={} file_date={}".format(len(result), file_date))
    return result, file_date

def bar_chart(row, label):
    fig = go.Figure(go.Bar(
        x=DAY_LABELS, y=[row[d] for d in DAYS],
        marker_color=BAR_COLOURS,
        text=[row[d] for d in DAYS],
        textposition="outside", cliponaxis=False,
    ))
    fig.update_layout(
        title=dict(text=label, font=dict(size=15)),
        yaxis_title="Train calls",
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(t=48,b=20,l=20,r=20), height=280, showlegend=False,
        yaxis=dict(gridcolor="rgba(128,128,128,0.15)", zeroline=False),
    )
    return fig

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

# Load bundled StationsRefData.xml if present in repo
_bundled_station_tiplocs = None
_bundled_station_names   = {}
try:
    if _os.path.exists(STATIONS_XML_PATH):
        with open(STATIONS_XML_PATH, "rb") as _f:
            _bundled_station_tiplocs, _bundled_station_names = parse_stations_xml(_f.read())
        print("Loaded {} station TIPLOCs from bundled XML".format(len(_bundled_station_tiplocs)))
except Exception as _e:
    print("Could not load bundled StationsRefData.xml: {}".format(_e))

# Read secrets safely — must be the very first thing that touches st.secrets
try:
    _secret_user = st.secrets["network_rail"]["username"]
    _secret_pass = st.secrets["network_rail"]["password"]
except Exception:
    _secret_user = ""
    _secret_pass = ""

file_map       = {}
passenger_only = True
stp_options    = ["P","O","N"]
crs_only       = True
xml_filter     = True
jn_filter      = True
name_filter    = ""
sort_col       = "weekly_total"
top_n          = 100

with st.sidebar:
    st.title("Train Frequency Explorer")
    st.divider()

    st.subheader("Data source")
    use_upload = st.radio("source", ["Download from Network Rail", "Upload file"],
                          label_visibility="collapsed")

    if use_upload == "Download from Network Rail":
        if _secret_user and _secret_pass:
            st.caption("Credentials loaded from secrets.")
            _u, _p = _secret_user, _secret_pass
        else:
            _u = st.text_input("Username (email)", value="")
            _p = st.text_input("Password", type="password", value="")

        ready = bool(_u and _p)

        if "cif_fetched_v2" in st.session_state:
            st.success("CIF ready ({:.1f} MB).".format(
                st.session_state["cif_fetched_mb_v2"]))
            file_map["CIF_ALL_FULL_DAILY.gz"] = st.session_state["cif_fetched_v2"]
            if st.button("Fetch fresh copy"):
                del st.session_state["cif_fetched_v2"]
                del st.session_state["cif_fetched_mb_v2"]
        else:
            if st.button("Fetch & parse CIF", disabled=not ready, type="primary"):
                with st.spinner("Downloading from Network Rail (~50 MB)..."):
                    try:
                        resp = requests.get(NR_URL, auth=(_u, _p),
                                            allow_redirects=True, timeout=300)
                        resp.raise_for_status()
                        raw = resp.content
                        st.session_state["cif_fetched_v2"] = raw
                        st.session_state["cif_fetched_mb_v2"] = len(raw) / 1e6
                        file_map["CIF_ALL_FULL_DAILY.gz"] = raw
                        st.success("Fetched {:.1f} MB.".format(len(raw) / 1e6))
                    except Exception as ex:
                        st.error("Fetch failed: {}".format(ex))

        xml_up = st.file_uploader(
            "Optional: upload StationsRefData.xml to filter to passenger stations",
            type=["xml"], key="xml_nr",
        )
        if xml_up is not None:
            file_map[xml_up.name] = xml_up.read()


    else:
        st.caption("Upload the CIF (.gz or .zip) and optionally the StationsRefData.xml to filter to passenger stations only.")
        up = st.file_uploader("Upload CIF file",
                              type=["gz","zip","mca","cif","msn","xml"],
                              accept_multiple_files=True, key="up2")
        if up:
            for f in up:
                file_map[f.name] = f.read()

    st.divider()
    st.subheader("Options")
    passenger_only = st.checkbox("Passenger services only", value=True)
    stp_options    = st.multiselect("STP indicators",
                                    options=["P","O","N"], default=["P","O","N"])


    st.divider()
    st.subheader("Filter")
    crs_only    = st.checkbox("CRS code stations only", value=True,
        help="Hides entries with no 3-letter CRS code (junctions, depots etc.)")
    xml_filter  = st.checkbox("Station reference filter", value=True,
        help="Only show TIPLOCs in StationsRefData.xml — removes non-passenger locations definitively.")
    jn_filter   = st.checkbox("Exclude junction / non-station names", value=True,
        help="Hides names containing Jn, Junction, Jct, Sidings, Depot, Loop, CS, TMD etc.")
    name_filter = st.text_input("Search name / CRS", placeholder="e.g. Edinburgh")
    sort_col    = st.selectbox("Sort by",
                               ["weekly_total"]+DAYS,
                               format_func=lambda c: c.replace("_"," ").title())
    top_n       = st.slider("Top N", 10, 500, 100, step=10)

    st.divider()
    st.subheader("Debug")
    debug_lookup = st.text_input(
        "Diagnose a station (TIPLOC / CRS / name)",
        placeholder="e.g. Albany Park, AYP, ALBYPK",
        help="Dumps every raw schedule found for this station to the logs, "
             "before STP resolution — useful for tracking down undercounts.",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

print("DEBUG: file_map keys =", list(file_map.keys()))
for fname, raw in file_map.items():
    print("DEBUG: file={} size={} first4={}".format(fname, len(raw), raw[:4].hex()))

if not file_map:
    st.markdown("## UK Train Frequency Explorer")
    st.info(
        "Use the sidebar to load a CIF timetable file.\n\n"
        "**Download from Network Rail** — click Fetch & parse to download "
        "and process the timetable automatically. No upload needed.\n\n"
        "**Upload file** — upload a TTIS zip, Network Rail .gz, or .MCA directly."
    )
    st.stop()

# Parse — use session_state as cache (avoids st.cache_data suppressing stdout)
_cache_key = "parsed_v5_{}_{}_{}_{}".format(
    _hash_file_map(file_map),
    passenger_only,
    "_".join(sorted(stp_options)) if stp_options else "P",
    debug_lookup.strip(),
)
print("DEBUG: cache_key={}".format(_cache_key))

if _cache_key in st.session_state:
    print("DEBUG: using cached result")
    df_full, _file_date = st.session_state[_cache_key]
else:
    print("DEBUG: starting parse, passenger_only={}, stp={}".format(passenger_only, stp_options))
    try:
        df_full, _file_date = run_parse(
            _cache_key,
            file_map,
            passenger_only,
            tuple(sorted(stp_options)) if stp_options else ("P",),
            debug_lookup=debug_lookup,
        )
        print("DEBUG: run_parse done, rows={}, file_date={}".format(len(df_full), _file_date))
        st.session_state[_cache_key] = (df_full, _file_date)
    except Exception as e:
        print("DEBUG: parse exception: {}".format(traceback.format_exc()))
        st.error("Parse error: {} — {}".format(type(e).__name__, e))
        st.code(traceback.format_exc())
        st.stop()

# Non-station name patterns
_JN_PATTERN = (
    r"(?i)(?:\bJn\b|\bJct\b|Junction|Sidings|\bSiding\b|\bDepot\b|"
    r"\bLoop\b|\bChord\b|Crossover|\bTMD\b|Carriage Works|Stabling|"
    r"Headshunt|Engineers|Ground Frame|Signal Box|\bGF\b|"
    r"\bNorth Jn\b|\bSouth Jn\b|\bEast Jn\b|\bWest Jn\b)"
)

# Filter
print("DEBUG: starting filter")
try:
    df = df_full.copy()
    print("DEBUG: df copied, shape={}".format(df.shape))
    if crs_only:
        df = df[df["crs"].str.strip() != ""]
        print("DEBUG: crs filter done, rows={}".format(len(df)))
    if xml_filter and _bundled_station_tiplocs:
        df = df[df["tiploc"].isin(_bundled_station_tiplocs)]
        print("DEBUG: xml filter done, rows={}".format(len(df)))
    if jn_filter:
        df = df[~df["station_name"].str.contains(_JN_PATTERN, regex=True, na=False)]
        print("DEBUG: jn filter done, rows={}".format(len(df)))
    if name_filter.strip():
        q = name_filter.strip().upper()
        mask = (df["station_name"].str.upper().str.contains(q, na=False)
              | df["crs"].str.upper().str.contains(q, na=False)
              | df["tiploc"].str.upper().str.contains(q, na=False))
        df = df[mask]
        print("DEBUG: name filter done, rows={}".format(len(df)))
    df = df.sort_values(sort_col, ascending=False).head(top_n).reset_index(drop=True)
    print("DEBUG: filter complete, final rows={}".format(len(df)))
except Exception as e:
    print("DEBUG: filter exception: {}".format(traceback.format_exc()))
    st.error("Filter error: {} — {}".format(type(e).__name__, e))
    st.code(traceback.format_exc())
    st.stop()

# Metrics
st.markdown("## Weekly train calls by station")

if _file_date is not None:
    _monday = _file_date - timedelta(days=_file_date.weekday())
    _sunday = _monday + timedelta(days=6)
    st.caption("Timetable file dated **{}** — showing services valid in the week "
               "**{} – {}**".format(
                   _file_date.strftime("%d %b %Y"),
                   _monday.strftime("%d %b %Y"),
                   _sunday.strftime("%d %b %Y")))
else:
    st.caption("File date not found in CIF header — counts include all schedules "
               "regardless of validity period.")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Stations shown",        "{:,}".format(len(df)))
c2.metric("Total stations parsed", "{:,}".format(len(df_full[df_full["crs"] != ""])))
if len(df_full) == 0:
    st.error("No schedules found in the CIF file. Check the debug prints in the logs.")
    st.stop()

c3.metric("Busiest station",
          (df.iloc[0]["station_name"] or df.iloc[0]["tiploc"]) if len(df) else "—")
c4.metric("Busiest weekly total",
          "{:,}".format(int(df.iloc[0]["weekly_total"])) if len(df) else "—")

st.divider()

# Table + detail
col_l, col_r = st.columns([3,2], gap="large")

with col_l:
    st.subheader("Top {} — {}".format(min(top_n,len(df)), sort_col.replace("_"," ")))
    dcols = ["crs","station_name"]+DAYS+["weekly_total"]
    rmap  = {"crs":"CRS","station_name":"Station","weekly_total":"Weekly",
             **{d: d[:3].title() for d in DAYS}}
    _max = lambda col: int(df[col].max()) if len(df) else 1
    st.dataframe(
        df[dcols].rename(columns=rmap),
        use_container_width=True,
        height=520,
        column_config={
            "Mon":    st.column_config.ProgressColumn("Mon",    min_value=0, max_value=_max("monday"),       format="%d"),
            "Tue":    st.column_config.ProgressColumn("Tue",    min_value=0, max_value=_max("tuesday"),      format="%d"),
            "Wed":    st.column_config.ProgressColumn("Wed",    min_value=0, max_value=_max("wednesday"),    format="%d"),
            "Thu":    st.column_config.ProgressColumn("Thu",    min_value=0, max_value=_max("thursday"),     format="%d"),
            "Fri":    st.column_config.ProgressColumn("Fri",    min_value=0, max_value=_max("friday"),       format="%d"),
            "Sat":    st.column_config.ProgressColumn("Sat",    min_value=0, max_value=_max("saturday"),     format="%d"),
            "Sun":    st.column_config.ProgressColumn("Sun",    min_value=0, max_value=_max("sunday"),       format="%d"),
            "Weekly": st.column_config.ProgressColumn("Weekly", min_value=0, max_value=_max("weekly_total"), format="%d"),
        }
    )
    st.download_button("Download full CSV",
                       df_full.to_csv(index=False).encode("utf-8"),
                       "station_calls.csv", "text/csv")

with col_r:
    st.subheader("Station detail")
    if len(df) == 0:
        st.info("No stations match.")
    else:
        opts = df.apply(
            lambda r: "{} — {}".format(r["crs"] or r["tiploc"],
                                       r["station_name"] or r["tiploc"]),
            axis=1,
        ).tolist()
        sel = st.selectbox("Select station", opts, index=0)
        row = df.iloc[opts.index(sel)]
        lbl = "{} ({})".format(row["station_name"] or row["tiploc"],
                               row["crs"] or row["tiploc"])
        st.plotly_chart(bar_chart(row, lbl), use_container_width=True)

        ddf = pd.DataFrame({
            "Day":   DAY_LABELS,
            "Calls": [int(row[d]) for d in DAYS],
            "Type":  ["Weekday"]*5+["Weekend"]*2,
        })
        ddf["% of peak"] = (ddf["Calls"] / (ddf["Calls"].max() or 1) * 100).round(1)
        st.dataframe(ddf, use_container_width=True, hide_index=True)

        m1,m2,m3 = st.columns(3)
        m1.metric("Weekday avg", "{:,}".format(int(sum(row[d] for d in DAYS[:5])/5)))
        m2.metric("Weekend avg", "{:,}".format(int(sum(row[d] for d in DAYS[5:])/2)))
        m3.metric("TIPLOC", row["tiploc"])

# Overview
st.divider()
st.subheader("Busiest stations overview")
top10 = df.head(10)
if len(top10):
    lbls = top10.apply(lambda r: r["crs"] or r["tiploc"], axis=1).tolist()
    fig  = go.Figure()
    for i, day in enumerate(DAYS):
        fig.add_trace(go.Bar(name=DAY_LABELS[i], x=lbls,
                             y=top10[day].tolist(),
                             marker_color=BAR_COLOURS[i]))
    fig.update_layout(
        barmode="group",
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
        yaxis=dict(title="Train calls", gridcolor="rgba(128,128,128,0.15)"),
        margin=dict(t=32,b=20,l=20,r=20), height=380,
    )
    st.plotly_chart(fig, use_container_width=True)
