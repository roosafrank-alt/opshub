"""ForeFlight logbook CSV (Logbook > Export) -> OpsHub logbook entries.

The export is one file with two tables: an "Aircraft Table" (tail, make, model...) and a
"Flights Table" (one row per flight, 71 columns). parse() reads both and returns entries shaped
like pilot_logbook rows, plus a total of what was skipped and why. Nothing here touches the database.

Not imported on purpose: the Person1..6 columns (other people's names and emails), Hobbs/Tach and the
clock times, and the ForeFlight-only counters. Flight Review / IPC / checkride flags are noted but not turned
into endorsements (an endorsement needs an instructor's signature).
"""
import csv
import hashlib
import io
import re
from datetime import date

HOURS = ("TotalTime", "PIC", "SIC", "Night", "Solo", "CrossCountry", "IFR", "ActualInstrument", "SimulatedInstrument",
         "DualGiven", "DualReceived", "SimulatedFlight", "GroundTraining")


def _num(v, default=0.0):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _int(v):
    return int(round(_num(v)))


def _text(v, limit=500):
    v = (v or "").strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        v = v[1:-1].strip()
    return v[:limit]


def _find(rows, title):
    for i, r in enumerate(rows):
        if r and r[0].strip().lower().startswith(title):
            return i
    return None


def entry_key(e):
    """Same flight imported twice -> same key (so a second upload of the same or a newer export adds only what's new)."""
    raw = "|".join(str(e[k]) for k in ("entry_date", "tail", "route_from", "route_to", "total", "pic", "sic", "dual", "solo"))
    return hashlib.sha1(raw.lower().encode()).hexdigest()[:20]


def parse(text):
    """Returns {"entries": [...], "skipped": [(line, reason)], "aircraft": n, "problem": None | str}."""
    out = {"entries": [], "skipped": [], "aircraft": 0, "problem": None, "flags": 0}
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows or not rows[0] or not rows[0][0].strip().startswith("ForeFlight Logbook Import"):
        out["problem"] = "This doesn't look like a ForeFlight logbook export (it should start with 'ForeFlight Logbook Import')."
        return out
    ai, fi = _find(rows, "aircraft table"), _find(rows, "flights table")
    if fi is None:
        out["problem"] = "No 'Flights Table' found in this file."
        return out
    aircraft = {}
    if ai is not None and ai + 1 < len(rows):
        head = [h.strip() for h in rows[ai + 1]]
        for r in rows[ai + 2:fi]:
            if not r or not r[0].strip():
                continue
            d = dict(zip(head, r))
            tail = d.get("AircraftID", "").strip().upper()
            make = re.sub(r"\s+Aircraft$", "", d.get("Make", "").strip())
            if tail:
                aircraft[tail] = (make + " " + d.get("Model", "").strip()).strip()
    out["aircraft"] = len(aircraft)
    head = [h.strip() for h in rows[fi + 1]]
    if "Date" not in head or "TotalTime" not in head:
        out["problem"] = "The Flights Table is missing its Date / TotalTime columns."
        return out
    for n, r in enumerate(rows[fi + 2:], start=fi + 3):
        if not r or not any(c.strip() for c in r):
            continue
        d = dict(zip(head, r))
        try:
            day = date.fromisoformat(d.get("Date", "").strip()[:10])
        except ValueError:
            out["skipped"].append((n, "bad date '%s'" % d.get("Date", "")[:20]))
            continue
        if day > date.today():
            out["skipped"].append((n, "date is in the future"))
            continue
        h = {k: _num(d.get(k)) for k in HOURS}
        if any(v < 0 for v in h.values()):
            out["skipped"].append((n, "negative hours"))
            continue
        if h["TotalTime"] > 24:
            out["skipped"].append((n, "more than 24 hours in one flight"))
            continue
        if not (h["TotalTime"] or h["SimulatedFlight"] or h["GroundTraining"]):
            out["skipped"].append((n, "no time logged"))
            continue
        tail = d.get("AircraftID", "").strip().upper()
        night_fs = _int(d.get("NightLandingsFullStop"))
        remarks = " - ".join(x for x in (_text(d.get("PilotComments")), _text(d.get("InstructorComments"))) if x)[:500] or None
        e = {
            "entry_date": day.isoformat(), "tail": tail or None, "make_model": aircraft.get(tail) or None,
            "route_from": _text(d.get("From"), 40).upper() or None, "route_to": _text(d.get("To"), 40).upper() or None,
            "route": _text(d.get("Route"), 300).upper() or None,
            "total": h["TotalTime"], "dual": h["DualReceived"], "dual_given": h["DualGiven"], "solo": h["Solo"],
            "pic": h["PIC"], "sic": h["SIC"], "xc": h["CrossCountry"], "night": h["Night"], "ifr": h["IFR"],
            "actual_inst": h["ActualInstrument"], "sim_inst": h["SimulatedInstrument"],
            "sim_flight": h["SimulatedFlight"], "ground": h["GroundTraining"],
            "approaches": _int(d.get("[Counter]Instrument Approach")),
            "night_ldg": night_fs, "night_fs": night_fs,
            "day_ldg": max(_int(d.get("AllLandings")) - night_fs, _int(d.get("DayLandingsFullStop"))),
            "remarks": remarks, "instructor_name": _text(d.get("InstructorName"), 120) or None,
        }
        if any(_text(d.get(k)) not in ("", "0", "false") for k in ("Flight Review (FAA)", "IPC (FAA)", "Checkride (FAA)", "FAA 61.58 (FAA)")):
            out["flags"] += 1
        e["import_key"] = entry_key(e)
        out["entries"].append(e)
    # Two identical-looking flights in one file (e.g. two pattern hops) get #1, #2, ... so each stays separate,
    # while the same flights in a later, overlapping export get the same keys again and are skipped.
    seen = {}
    for e in out["entries"]:
        n = seen[e["import_key"]] = seen.get(e["import_key"], 0) + 1
        e["import_key"] = "%s#%d" % (e["import_key"], n)
    return out


def totals(entries):
    keys = ("total", "pic", "sic", "dual", "dual_given", "solo", "xc", "night", "actual_inst", "sim_inst", "approaches", "day_ldg", "night_ldg")
    return {k: round(sum(e[k] or 0 for e in entries), 1) for k in keys}
