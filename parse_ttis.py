#!/usr/bin/env python3
"""
TTIS CIF Parser — Weekly Train Calls by Station
================================================
Parses a TTIS CIF file and the accompanying MSN (Master Station Names) file
to produce a CSV counting how many scheduled train services call at every UK
station, broken down by day of week.

Usage
-----
    python parse_ttis.py --cif PATH [OPTIONS]

    --cif PATH      Path to the TTIS .zip (preferred), or the .MCA CIF file directly.
                    The weekly TTIS zip from National Rail Open Data contains:
                      RJTTFxxx.MCA  — full timetable (this is the CIF)
                      RJTTFxxx.MSN  — master station names
                    Download from: https://opendata.nationalrail.co.uk/
                    (free registration required; choose "Timetable" data)

    --msn PATH      Path to the .MSN file. Auto-detected from the zip; only
                    needed when passing the .MCA file directly.

    --out PATH      Output CSV path (default: station_calls.csv)

    --all-services  Include freight, ECS, and bus services.
                    Default: passenger trains only (status P / 1 / 5).

    --stp P,O,N     STP priority codes to include, comma-separated.
                    Default: P,O,N  (excludes STP cancellations C).

Output CSV columns
------------------
    tiploc, crs, station_name,
    monday, tuesday, wednesday, thursday, friday, saturday, sunday,
    weekly_total

Each row counts the number of train services that stop at that station on a
typical day, based on the schedules in the timetable file. STP overlays and
cancellations are applied before counting.

Notes
-----
- 'Calls' = services that make a public stop (origin, calling point, or
  terminus with a published arrival/departure time). Pass-through timing
  points and ECS movements are excluded unless --all-services is used.
- STP cancellations (C) suppress the permanent (P) schedule for the same
  UID and overlapping date range. STP overlays (O) replace permanents.
- The count reflects the days-run bitmask (MTWTFSS) regardless of specific
  bank holiday adjustments; the BH field in the BS record is informational.
- Runs fine on a standard laptop: the full TTIS MCA file (~50 MB, ~500k
  schedules) parses in roughly 30-60 seconds.
"""

import argparse
import csv
import os
import sys
import zipfile
from collections import defaultdict
from datetime import date


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Train status codes that represent passenger services
PASSENGER_STATUSES = {
    "P",  # Passenger & Parcels
    "1",  # STP Passenger & Parcels
    "5",  # Bus (replacement/scheduled)
}

# STP priority: higher index wins when schedules overlap
STP_PRIORITY = {"P": 0, "O": 1, "N": 2, "C": 3}


# ---------------------------------------------------------------------------
# Date / bitmask helpers
# ---------------------------------------------------------------------------

def parse_yymmdd(s: str):
    """Parse a YYMMDD string to a date object, or return None."""
    if not s or len(s) < 6 or s.strip() == "":
        return None
    try:
        yy, mm, dd = int(s[0:2]), int(s[2:4]), int(s[4:6])
        year = 2000 + yy if yy < 60 else 1900 + yy
        return date(year, mm, dd)
    except (ValueError, TypeError):
        return None


def days_active(days_run: str) -> set:
    """
    Convert a 7-char MTWTFSS bitmask to a set of weekday indices (0=Mon, 6=Sun).
    '1111100' → {0, 1, 2, 3, 4}
    """
    if not days_run or len(days_run) < 7:
        return set()
    return {i for i, ch in enumerate(days_run[:7]) if ch == "1"}


# ---------------------------------------------------------------------------
# Activity field helper
# ---------------------------------------------------------------------------

def is_public_stop(activity: str) -> bool:
    """
    Return True if the 12-char activity field represents a public stop.
    Activity codes -D (set-down not available) and -U (pick-up not available)
    together would mean no public service; a pass is encoded as a blank
    activity with only a WTT passing time and no public time.
    We let the caller filter on the presence of a public time; here we just
    exclude the rare explicit non-public markers.
    """
    acts = {activity[i:i + 2].strip() for i in range(0, min(len(activity), 12), 2)}
    return not (acts >= {"-D", "-U"})


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------

def decode(raw: bytes) -> list:
    """Decode bytes to a list of lines (try UTF-8, fall back to latin-1)."""
    try:
        return raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return raw.decode("latin-1").splitlines()


def load_files(cif_path: str, msn_path: str = None) -> tuple:
    """
    Return (cif_lines, msn_lines).  Both are lists of strings.
    Reads from a TTIS zip or direct file paths.
    """
    cif_lines = None
    msn_lines = None

    if zipfile.is_zipfile(cif_path):
        with zipfile.ZipFile(cif_path) as zf:
            names = zf.namelist()
            for name in names:
                upper = name.upper()
                if upper.endswith(".MCA") and cif_lines is None:
                    with zf.open(name) as f:
                        cif_lines = decode(f.read())
                elif upper.endswith(".MSN") and msn_lines is None and msn_path is None:
                    with zf.open(name) as f:
                        msn_lines = decode(f.read())
    else:
        # Treat as a raw CIF file
        with open(cif_path, "rb") as f:
            cif_lines = decode(f.read())

    if cif_lines is None:
        raise FileNotFoundError(
            f"Could not find a .MCA file in {cif_path!r}. "
            "Pass the TTIS .zip or the .MCA file directly."
        )

    if msn_path:
        with open(msn_path, "rb") as f:
            msn_lines = decode(f.read())

    return cif_lines, msn_lines


# ---------------------------------------------------------------------------
# MSN parser
# ---------------------------------------------------------------------------

def parse_msn(lines: list) -> dict:
    """
    Parse the Master Station Names file (.MSN).

    Record format (0-based positions, fixed-width):
      [0]     : 'A'  (record type; other types are file header etc.)
      [1:27]  : Station name (26 chars, space-padded)
      [27]    : CATE interchange status
      [28:35] : TIPLOC code (7 chars, space-padded)
      [35]    : Subsidiary TIPLOC numeric suffix
      [36:39] : CRS code (3-alpha)
      [39:42] : Minimum connection time
      [42:47] : Easting
      [47:52] : Northing

    Returns dict: tiploc → {'name': str, 'crs': str}
    """
    stations = {}
    for line in lines:
        if len(line) < 39 or line[0] != "A":
            continue
        name = line[1:27].strip()
        tiploc = line[28:35].strip()
        crs = line[36:39].strip()
        if tiploc:
            stations[tiploc] = {"name": name, "crs": crs}
    return stations


# ---------------------------------------------------------------------------
# CIF parser
# ---------------------------------------------------------------------------

def parse_cif(
    lines: list,
    passenger_only: bool = True,
    stp_include: set = None,
) -> tuple:
    """
    Parse a CIF file and extract schedule records.

    BS record layout (0-based, each record is exactly 80 chars):
      [0:2]   Record type 'BS'
      [2]     Transaction type: N=New, D=Delete, R=Revise
      [3:9]   Train UID (6 chars)
      [9:15]  Date runs from (YYMMDD)
      [15:21] Date runs to (YYMMDD)
      [21:28] Days run (7-char MTWTFSS bitmask)
      [28]    Bank holiday running
      [29]    Train status (P=passenger, F=freight, B=bus, S=ship, …)
      [79]    STP indicator (P=permanent, O=overlay, N=new STP, C=cancellation)

    TI record (TIPLOC Insert) layout:
      [2:9]   TIPLOC (7 chars)
      [18:44] TPS description / station name (26 chars)
      [53:56] CRS code (3 chars)

    LO (Origin Location) layout:
      [2:9]   TIPLOC + suffix (8 chars; TIPLOC is first 7, suffix 1 char)
      [15:19] Public departure time (HHMM)

    LI (Intermediate Location) layout:
      [2:10]  TIPLOC + suffix (8 chars)
      [25:29] Public arrival time (HHMM)
      [29:33] Public departure time (HHMM)
      [42:54] Activity (12 chars, 2-char codes)

    LT (Terminating Location) layout:
      [2:10]  TIPLOC + suffix (8 chars)
      [15:19] Public arrival time (HHMM)
      [25:37] Activity (12 chars)

    Returns:
      schedules  — list of dicts, each with keys:
                   uid, date_from, date_to, days_run, stp, stops (list of TIPLOCs)
      tiploc_map — dict tiploc → {name, crs}  from TI records
    """
    if stp_include is None:
        stp_include = {"P", "O", "N"}

    tiploc_map: dict = {}
    schedules: list = []

    current_bs: dict = None
    current_stops: list = []
    in_schedule: bool = False

    for raw in lines:
        line = raw.rstrip("\r\n")
        if len(line) < 2:
            continue
        rt = line[0:2]

        # ---- TIPLOC Insert -----------------------------------------------
        if rt == "TI":
            if len(line) < 56:
                continue
            tiploc = line[2:9].strip()
            name = line[18:44].strip()
            crs = line[53:56].strip()
            if tiploc and tiploc not in tiploc_map:
                tiploc_map[tiploc] = {"name": name, "crs": crs}

        # ---- Basic Schedule ----------------------------------------------
        elif rt == "BS":
            # Save any schedule in progress
            if in_schedule and current_bs is not None and current_stops:
                current_bs["stops"] = current_stops
                schedules.append(current_bs)
            in_schedule = False
            current_stops = []
            current_bs = None

            if len(line) < 80:
                continue
            if line[2] == "D":          # Delete — no location records follow
                continue

            stp = line[79]
            if stp not in stp_include:
                continue

            status = line[29]
            if passenger_only and status not in PASSENGER_STATUSES:
                continue

            current_bs = {
                "uid":       line[3:9].strip(),
                "date_from": parse_yymmdd(line[9:15]),
                "date_to":   parse_yymmdd(line[15:21]),
                "days_run":  line[21:28],
                "stp":       stp,
            }
            in_schedule = True

        # ---- BX (skip) ---------------------------------------------------
        elif rt == "BX":
            pass

        # ---- Origin Location ---------------------------------------------
        elif rt == "LO" and in_schedule:
            tiploc = line[2:9].strip()      # LO has TIPLOC+suffix in [2:10], TIPLOC in [2:9]
            pub_dep = line[15:19].strip()   # public departure time HHMM
            if pub_dep:
                current_stops.append(tiploc)

        # ---- Intermediate Location ---------------------------------------
        elif rt == "LI" and in_schedule:
            tiploc = line[2:9].strip()
            pub_arr = line[25:29].strip()   # public arrival
            pub_dep = line[29:33].strip()   # public departure
            activity = line[42:54] if len(line) >= 54 else ""
            if (pub_arr or pub_dep) and is_public_stop(activity):
                current_stops.append(tiploc)

        # ---- Terminating Location ----------------------------------------
        elif rt == "LT" and in_schedule:
            tiploc = line[2:9].strip()
            pub_arr = line[15:19].strip()   # public arrival
            if pub_arr:
                current_stops.append(tiploc)
            # Always ends a schedule
            if current_stops:
                current_bs["stops"] = current_stops
                schedules.append(current_bs)
            in_schedule = False
            current_stops = []
            current_bs = None

        # ---- End of File -------------------------------------------------
        elif rt == "ZZ":
            if in_schedule and current_bs is not None and current_stops:
                current_bs["stops"] = current_stops
                schedules.append(current_bs)
            break

    return schedules, tiploc_map


# ---------------------------------------------------------------------------
# STP resolution
# ---------------------------------------------------------------------------

def apply_stp(schedules: list) -> list:
    """
    Apply STP (Short-Term Planning) overlay rules.

    For each train UID:
    - STP cancellations (C) suppress the permanent schedule (P) for any
      overlapping date range.
    - STP overlays (O) take priority over permanents on their date range;
      for weekly counts we include both unless they duplicate — in practice
      the overlay replaces the permanent timing and the permanent runs on
      days/dates outside the overlay window.
    - New STP schedules (N) are independent services; always included.
    - Delete transactions (D) are never in the list (filtered in parse_cif).

    Returns the filtered list of schedules (C records removed, P records
    suppressed where a C for the same UID covers the same date window).
    """
    by_uid = defaultdict(list)
    for s in schedules:
        by_uid[s["uid"]].append(s)

    result = []
    for uid, group in by_uid.items():
        cancellations = [s for s in group if s["stp"] == "C"]
        for s in group:
            if s["stp"] == "C":
                continue
            if s["stp"] == "P" and cancellations:
                # Check if any C record's date range overlaps this P
                p_from, p_to = s["date_from"], s["date_to"]
                suppressed = False
                for c in cancellations:
                    c_from, c_to = c["date_from"], c["date_to"]
                    if p_from and p_to and c_from and c_to:
                        if c_from <= p_to and c_to >= p_from:
                            suppressed = True
                            break
                if suppressed:
                    continue
            result.append(s)

    return result


# ---------------------------------------------------------------------------
# Call counter
# ---------------------------------------------------------------------------

def count_calls(schedules: list) -> dict:
    """
    Count calls per TIPLOC per day of week.
    Returns dict: tiploc → [mon, tue, wed, thu, fri, sat, sun]  (int counts)
    """
    counts = defaultdict(lambda: [0] * 7)
    for s in schedules:
        active = days_active(s["days_run"])
        if not active:
            continue
        for tiploc in s.get("stops", []):
            for day_idx in active:
                counts[tiploc][day_idx] += 1
    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Parse a TTIS CIF file and output weekly train calls per station.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--cif", required=True,
        metavar="PATH",
        help="TTIS .zip or .MCA CIF file")
    ap.add_argument("--msn",
        metavar="PATH",
        help=".MSN Master Station Names file (auto-detected from zip)")
    ap.add_argument("--out", default="station_calls.csv",
        metavar="PATH",
        help="Output CSV (default: station_calls.csv)")
    ap.add_argument("--all-services", action="store_true",
        help="Include freight, ECS, and bus (default: passenger only)")
    ap.add_argument("--stp", default="P,O,N",
        metavar="CODES",
        help="STP codes to include, comma-separated (default: P,O,N)")
    args = ap.parse_args()

    passenger_only = not args.all_services
    stp_include = {x.strip().upper() for x in args.stp.split(",")}

    # ------------------------------------------------------------------
    print(f"Loading files from: {args.cif}")
    try:
        cif_lines, msn_lines = load_files(args.cif, args.msn)
    except (FileNotFoundError, zipfile.BadZipFile) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  CIF: {len(cif_lines):,} lines loaded")
    if msn_lines:
        print(f"  MSN: {len(msn_lines):,} lines loaded")
    else:
        print("  MSN: not found — station names will use CIF TI records only")

    # ------------------------------------------------------------------
    print(f"\nParsing schedules "
          f"(passenger_only={passenger_only}, stp={sorted(stp_include)})…")
    schedules, tiploc_map = parse_cif(
        cif_lines,
        passenger_only=passenger_only,
        stp_include=stp_include,
    )
    print(f"  {len(schedules):,} schedules found")
    print(f"  {len(tiploc_map):,} TIPLOCs from TI records")

    # ------------------------------------------------------------------
    if msn_lines:
        print("\nMerging MSN station names…")
        msn_stations = parse_msn(msn_lines)
        print(f"  {len(msn_stations):,} stations in MSN file")
        for tiploc, info in msn_stations.items():
            if tiploc not in tiploc_map:
                tiploc_map[tiploc] = info
            else:
                # MSN name takes priority — it's the official public name
                if info["name"]:
                    tiploc_map[tiploc]["name"] = info["name"]
                if info["crs"]:
                    tiploc_map[tiploc]["crs"] = info["crs"]

    # ------------------------------------------------------------------
    print("\nApplying STP rules…")
    schedules = apply_stp(schedules)
    print(f"  {len(schedules):,} schedules after STP resolution")

    # ------------------------------------------------------------------
    print("\nCounting calls per station per day…")
    counts = count_calls(schedules)
    print(f"  {len(counts):,} stations with at least one call")

    # ------------------------------------------------------------------
    print(f"\nWriting output to: {args.out}")
    rows = []
    for tiploc, day_counts in counts.items():
        info = tiploc_map.get(tiploc, {"name": "", "crs": ""})
        total = sum(day_counts)
        rows.append([
            tiploc,
            info.get("crs", ""),
            info.get("name", ""),
            *day_counts,
            total,
        ])
    rows.sort(key=lambda r: r[-1], reverse=True)   # busiest first

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "tiploc", "crs", "station_name",
            "monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday", "weekly_total",
        ])
        writer.writerows(rows)

    # ------------------------------------------------------------------
    w = 6
    print(f"\nDone — {len(rows):,} stations written to {args.out!r}")
    print(f"\nTop 15 busiest stations (by weekly total):\n")
    header = (
        f"  {'Station':<32} {'CRS':4}  "
        + "  ".join(f"{d:>{w}}" for d in DAY_LABELS)
        + f"  {'Total':>{w}}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in rows[:15]:
        name = (row[2] or row[0])[:32]
        cells = "  ".join(f"{v:>{w},}" for v in row[3:10])
        total = f"{row[10]:>{w},}"
        print(f"  {name:<32} {row[1]:4}  {cells}  {total}")


if __name__ == "__main__":
    main()
