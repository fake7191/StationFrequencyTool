"""
UK Train Frequency Explorer
Run: streamlit run app.py
"""

import gzip
import hashlib
import io
import traceback
import zipfile
from collections import defaultdict
from datetime import date

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

NR_URL = (
    "https://publicdatafeeds.networkrail.co.uk"
    "/ntrod/CifFileAuthenticate?type=CIF_ALL_FULL_DAILY&day=toc-full"
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

def parse_cif(line_iter, passenger_only=True, stp_include=None):
    if stp_include is None:
        stp_include = {"P","O","N"}
    tiploc_map, schedules = {}, []
    cur, stops, active = None, [], False
    for raw in line_iter:
        line = raw.rstrip("\r\n")
        if len(line) < 2:
            continue
        rt = line[0:2]
        if rt == "TI":
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
            # STP indicator is at pos 79 on a full 80-char line
            # NR CIF lines can occasionally be shorter — default to P
            stp    = line[79].strip() if len(line) >= 80 else "P"
            status = line[29].strip()
            if stp not in stp_include:
                continue
            if passenger_only and status not in PASSENGER_STATUSES:
                continue
            cur = {"uid": line[3:9].strip(),
                   "date_from": _parse_date(line[9:15]),
                   "date_to":   _parse_date(line[15:21]),
                   "days_run":  line[21:28],
                   "stp": stp}
            active = True
        elif rt == "LO" and active:
            t = line[2:9].strip()
            if len(line) >= 19 and line[15:19].strip():
                stops.append(t)
        elif rt == "LI" and active:
            t = line[2:9].strip()
            pa = line[25:29].strip() if len(line) >= 29 else ""
            pd_ = line[29:33].strip() if len(line) >= 33 else ""
            act = line[42:54] if len(line) >= 54 else ""
            if (pa or pd_) and _public_stop(act):
                stops.append(t)
        elif rt == "LT" and active:
            t = line[2:9].strip()
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
    return schedules, tiploc_map

def apply_stp(schedules):
    by_uid = defaultdict(list)
    for s in schedules:
        by_uid[s["uid"]].append(s)
    result = []
    for group in by_uid.values():
        cancels = [s for s in group if s["stp"] == "C"]
        for s in group:
            if s["stp"] == "C":
                continue
            if s["stp"] == "P" and cancels:
                pf, pt = s["date_from"], s["date_to"]
                if any(c["date_from"] and c["date_to"] and pf and pt
                       and c["date_from"] <= pt and c["date_to"] >= pf
                       for c in cancels):
                    continue
            result.append(s)
    return result

def count_calls(schedules):
    counts = defaultdict(lambda: [0]*7)
    for s in schedules:
        active = _active_days(s["days_run"])
        for t in s.get("stops", []):
            for d in active:
                counts[t][d] += 1
    return counts

def build_df(counts, tiploc_map):
    rows = []
    for t, dc in counts.items():
        info = tiploc_map.get(t, {"name":"","crs":""})
        rows.append({"tiploc": t,
                     "crs": info.get("crs",""),
                     "station_name": info.get("name",""),
                     **{DAYS[i]: dc[i] for i in range(7)},
                     "weekly_total": sum(dc)})
    if not rows:
        # Return empty DataFrame with correct columns so the app can report cleanly
        cols = ["tiploc","crs","station_name"] + DAYS + ["weekly_total"]
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows).sort_values("weekly_total", ascending=False).reset_index(drop=True)

def _hash_file_map(file_map):
    """Hash file contents so we can cache without storing raw bytes."""
    h = hashlib.md5()
    for fname in sorted(file_map.keys()):
        h.update(fname.encode())
        h.update(file_map[fname][:65536])   # first 64KB is enough to detect changes
        h.update(str(len(file_map[fname])).encode())
    return h.hexdigest()

@st.cache_data(show_spinner=False)
def run_parse(file_hash, file_map, passenger_only, stp_tuple):
    """file_hash is only used as the cache key; file_map holds the data."""
    print("DEBUG run_parse: start hash={} files={}".format(
        file_hash, list(file_map.keys())))
    cif_iter = None
    msn_lines = None

    for fname, raw in file_map.items():
        print("DEBUG run_parse: opening stream for {}".format(fname))
        ci, ml = _open_cif_stream(raw, fname)
        if ci is not None and cif_iter is None:
            cif_iter = ci
        if ml is not None and msn_lines is None:
            msn_lines = ml

    if cif_iter is None:
        raise ValueError("No CIF data found in uploaded file.")

    print("DEBUG run_parse: parse_cif streaming start")
    schedules, tiploc_map = parse_cif(cif_iter, passenger_only, set(stp_tuple))
    print("DEBUG run_parse: parse_cif done, schedules={} tiplocs={}".format(
        len(schedules), len(tiploc_map)))
    # Show STP distribution so we can diagnose filtering issues
    from collections import Counter
    stp_counts = Counter(s["stp"] for s in schedules)
    status_counts = Counter(s.get("status","?") for s in schedules)
    print("DEBUG STP distribution: {}".format(dict(stp_counts)))
    print("DEBUG Status distribution: {}".format(dict(status_counts)))
    if schedules:
        sample = schedules[0]
        print("DEBUG First schedule: uid={} days={} stp={} stops_count={}".format(
            sample.get("uid"), sample.get("days_run"), sample.get("stp"),
            len(sample.get("stops",[]))))

    if msn_lines:
        print("DEBUG run_parse: merging MSN")
        for t, info in parse_msn(msn_lines).items():
            if t not in tiploc_map:
                tiploc_map[t] = info
            else:
                if info["name"]: tiploc_map[t]["name"] = info["name"]
                if info["crs"]:  tiploc_map[t]["crs"]  = info["crs"]

    print("DEBUG run_parse: apply_stp + count_calls + build_df")
    result = build_df(count_calls(apply_stp(schedules)), tiploc_map)
    print("DEBUG run_parse: done, rows={}".format(len(result)))
    return result

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

        if "cif_fetched" in st.session_state:
            st.success("CIF ready ({:.1f} MB).".format(
                st.session_state["cif_fetched_mb"]))
            file_map["CIF_ALL_FULL_DAILY.gz"] = st.session_state["cif_fetched"]
            if st.button("Fetch fresh copy"):
                del st.session_state["cif_fetched"]
                del st.session_state["cif_fetched_mb"]
        else:
            if st.button("Fetch & parse CIF", disabled=not ready, type="primary"):
                with st.spinner("Downloading from Network Rail (~50 MB)..."):
                    try:
                        resp = requests.get(NR_URL, auth=(_u, _p),
                                            allow_redirects=True, timeout=300)
                        resp.raise_for_status()
                        raw = resp.content
                        st.session_state["cif_fetched"] = raw
                        st.session_state["cif_fetched_mb"] = len(raw) / 1e6
                        file_map["CIF_ALL_FULL_DAILY.gz"] = raw
                        st.success("Fetched {:.1f} MB.".format(len(raw) / 1e6))
                    except Exception as ex:
                        st.error("Fetch failed: {}".format(ex))


    else:
        up = st.file_uploader("Upload CIF file",
                              type=["gz","zip","mca","cif","msn"],
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
    crs_only    = st.checkbox("CRS code stations only", value=True)
    name_filter = st.text_input("Search name / CRS", placeholder="e.g. Edinburgh")
    sort_col    = st.selectbox("Sort by",
                               ["weekly_total"]+DAYS,
                               format_func=lambda c: c.replace("_"," ").title())
    top_n       = st.slider("Top N", 10, 500, 100, step=10)

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

# Parse
print("DEBUG: starting parse, passenger_only={}, stp={}".format(passenger_only, stp_options))
try:
    print("DEBUG: calling run_parse")
    df_full = run_parse(
        _hash_file_map(file_map),
        file_map,
        passenger_only,
        tuple(sorted(stp_options)) if stp_options else ("P",),
    )
    print("DEBUG: run_parse done, rows={}".format(len(df_full)))
except Exception as e:
    print("DEBUG: parse exception: {}".format(traceback.format_exc()))
    st.error("Parse error: {} — {}".format(type(e).__name__, e))
    st.code(traceback.format_exc())
    st.stop()

# Filter
print("DEBUG: starting filter")
try:
    df = df_full.copy()
    print("DEBUG: df copied, shape={}".format(df.shape))
    if crs_only:
        df = df[df["crs"].str.strip() != ""]
        print("DEBUG: crs filter done, rows={}".format(len(df)))
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
    styled = (df[dcols].rename(columns=rmap).style
              .background_gradient(subset=["Mon","Tue","Wed","Thu","Fri"],
                                   cmap="Blues", vmin=0)
              .background_gradient(subset=["Sat","Sun"], cmap="Oranges", vmin=0)
              .background_gradient(subset=["Weekly"],    cmap="Purples", vmin=0)
              .format("{:,.0f}", subset=["Mon","Tue","Wed","Thu","Fri","Sat","Sun","Weekly"]))
    st.dataframe(styled, use_container_width=True, height=520)
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
        st.dataframe(ddf.style.format({"Calls":"{:,}","% of peak":"{:.1f}%"}),
                     use_container_width=True, hide_index=True)

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
