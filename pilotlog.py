"""Flight Academy: the student's pilot logbook and progress toward each
certificate/rating.

Logbook: every logged flight (flights table) gets a logbook entry for its
student, pre-filled from the flight - date, plane, total time (Hobbs, else
Hobbs), dual received / solo / PIC (a flight can be part dual, part solo:
flights.solo_hours), landings, and the instructor's name + CFI certificate
number/expiration from their CFI profile. It stays "pending" until the
student fills in the rest (route, cross-country, night, instrument time,
remarks) and approves it. Exportable as CSV.

Progress: the 14 CFR Part 61 aeronautical experience for each certificate
and rating, as a checklist. Hour and landing items are totalled from the
logbook automatically (with the flights that count toward each); the rest
(knowledge test passed, the long cross-country, endorsements...) are ticked
off by an instructor.
"""
import csv
import re
import io
from datetime import date

from flask import jsonify, Blueprint, render_template, request, redirect, url_for, session, flash, abort, Response

from db import get_db, now_iso

pilotlog_bp = Blueprint("pilotlog", __name__, url_prefix="/academy")

# Columns a student fills in / can correct when completing an entry.
STUDENT_FIELDS = [
    # (field, label, kind)
    ("route_from", "From", "text"), ("route_to", "To / via", "text"),
    ("route", "Route (optional): waypoints, navaids, airways", "text"),
    ("xc", "Cross-country", "hrs"), ("night", "Night", "hrs"),
    ("actual_inst", "Actual instrument", "hrs"), ("sim_inst", "Simulated instrument (hood)", "hrs"),
    ("approaches", "Instrument approaches", "int"),
    ("day_ldg", "Day landings", "int"), ("night_ldg", "Night landings", "int"),
    ("night_fs", "Night full-stop landings", "int"),
    ("pic", "PIC", "hrs"), ("remarks", "Remarks / maneuvers", "text"),
]

EXPORT_COLUMNS = [
    ("entry_date", "Date"), ("make_model", "Aircraft Make/Model"), ("tail", "Ident"),
    ("route_from", "From"), ("route_to", "To"), ("total", "Total"), ("dual", "Dual Received"),
    ("solo", "Solo"), ("pic", "PIC"), ("xc", "Cross-Country"), ("night", "Night"),
    ("actual_inst", "Actual Instrument"), ("sim_inst", "Simulated Instrument"), ("approaches", "Approaches"),
    ("day_ldg", "Day Landings"), ("night_ldg", "Night Landings"), ("night_fs", "Night Full-Stop Landings"),
    ("remarks", "Remarks"), ("instructor_name", "Instructor"), ("instructor_cert", "Instructor Cert #"),
    ("instructor_cert_exp", "Instructor Cert Exp"), ("status", "Status"),
    ("sic", "SIC"), ("dual_given", "Dual Given"), ("ifr", "IFR"), ("sim_flight", "Simulator"), ("ground", "Ground Training"),
    ("route", "Route"), ("source", "Source"),
]

# ---------------------------------------------------------------------------
# Requirements (14 CFR Part 61 aeronautical experience, airplane single-
# engine land unless noted). kind:
#   hours / count - totalled from the logbook column `col` (see _entry_values)
#   manual        - ticked off by an instructor (academy_milestones)
#   cert          - the student's certificate/ratings on their profile
#   solo_signoff  - a solo endorsement date on their profile
# window "2cal" = only flights in the current and preceding 2 calendar
# months count (practical test prep).
# ---------------------------------------------------------------------------
TRACKS = [
    {"code": "solo", "name": "First Solo", "ref": "14 CFR 61.87", "icon": "bi-person-fill-up", "items": [
        {"code": "presolo_test", "label": "Pre-solo aeronautical knowledge test passed (given by your CFI)", "ref": "61.87(b)", "kind": "manual"},
        {"code": "presolo_training", "label": "Pre-solo flight training on the maneuvers and procedures", "ref": "61.87(c)-(d)", "kind": "manual"},
        {"code": "dual_before_solo", "label": "Flight training received before solo (no fixed minimum - typically 10-20 hrs)", "ref": "61.87(c)", "kind": "hours", "col": "dual", "need": 10},
        {"code": "solo_endorsement", "label": "Solo endorsement in your logbook (within the last 90 days)", "ref": "61.87(n)", "kind": "solo_signoff"},
    ]},
    {"code": "sport", "name": "Sport Pilot", "ref": "14 CFR 61.313(a)", "icon": "bi-wind", "items": [
        {"code": "total", "label": "20 hours of flight time", "ref": "61.313(a)(1)", "kind": "hours", "col": "total", "need": 20},
        {"code": "dual", "label": "15 hours of flight training from an authorized instructor", "ref": "61.313(a)(1)", "kind": "hours", "col": "dual", "need": 15},
        {"code": "solo", "label": "5 hours of solo flight training", "ref": "61.313(a)(1)", "kind": "hours", "col": "solo", "need": 5},
        {"code": "dual_xc", "label": "2 hours of cross-country flight training", "ref": "61.313(a)(1)(i)", "kind": "hours", "col": "dual_xc", "need": 2},
        {"code": "ldg", "label": "10 takeoffs and landings to a full stop", "ref": "61.313(a)(1)(ii)", "kind": "count", "col": "all_ldg", "need": 10},
        {"code": "solo_xc", "label": "One solo cross-country of 75 nm total, full-stop landings at 2 points, one leg of 25 nm or more", "ref": "61.313(a)(1)(iii)", "kind": "manual"},
        {"code": "prep", "label": "2 hours of practical test prep training within 2 calendar months of the test", "ref": "61.313(a)(1)(iv)", "kind": "hours", "col": "dual", "need": 2, "window": "2cal"},
        {"code": "knowledge", "label": "Knowledge test passed", "ref": "61.305", "kind": "manual"},
        {"code": "endorse", "label": "Endorsement for the practical test", "ref": "61.307", "kind": "manual"},
    ]},
    {"code": "recreational", "name": "Recreational Pilot", "ref": "14 CFR 61.99", "icon": "bi-sun", "items": [
        {"code": "total", "label": "30 hours of flight time", "ref": "61.99(a)", "kind": "hours", "col": "total", "need": 30},
        {"code": "dual", "label": "15 hours of flight training from an authorized instructor", "ref": "61.99(a)(1)", "kind": "hours", "col": "dual", "need": 15},
        {"code": "enroute", "label": "2 hours of en route training to an airport more than 25 nm away, with a takeoff, landing and pattern", "ref": "61.99(a)(1)(i)", "kind": "manual"},
        {"code": "prep", "label": "3 hours of practical test prep training within 2 calendar months of the test", "ref": "61.99(a)(1)(ii)", "kind": "hours", "col": "dual", "need": 3, "window": "2cal"},
        {"code": "solo", "label": "3 hours of solo flight", "ref": "61.99(a)(2)", "kind": "hours", "col": "solo", "need": 3},
        {"code": "knowledge", "label": "Knowledge test passed", "ref": "61.95", "kind": "manual"},
        {"code": "endorse", "label": "Endorsement for the practical test", "ref": "61.96(b)", "kind": "manual"},
    ]},
    {"code": "private", "name": "Private Pilot", "ref": "14 CFR 61.109(a)", "icon": "bi-airplane", "items": [
        {"code": "total", "label": "40 hours of flight time", "ref": "61.109(a)", "kind": "hours", "col": "total", "need": 40},
        {"code": "dual", "label": "20 hours of flight training from an authorized instructor", "ref": "61.109(a)", "kind": "hours", "col": "dual", "need": 20},
        {"code": "dual_xc", "label": "3 hours of cross-country flight training", "ref": "61.109(a)(1)", "kind": "hours", "col": "dual_xc", "need": 3},
        {"code": "dual_night", "label": "3 hours of night flight training", "ref": "61.109(a)(2)", "kind": "hours", "col": "dual_night", "need": 3},
        {"code": "night_xc", "label": "One night cross-country of more than 100 nm total distance", "ref": "61.109(a)(2)(i)", "kind": "manual"},
        {"code": "night_ldg", "label": "10 night takeoffs and landings to a full stop", "ref": "61.109(a)(2)(ii)", "kind": "count", "col": "night_fs", "need": 10},
        {"code": "dual_inst", "label": "3 hours of flight training by reference to instruments", "ref": "61.109(a)(3)", "kind": "hours", "col": "dual_inst", "need": 3},
        {"code": "prep", "label": "3 hours of practical test prep training within 2 calendar months of the test", "ref": "61.109(a)(4)", "kind": "hours", "col": "dual", "need": 3, "window": "2cal"},
        {"code": "solo", "label": "10 hours of solo flight time", "ref": "61.109(a)(5)", "kind": "hours", "col": "solo", "need": 10},
        {"code": "solo_xc", "label": "5 hours of solo cross-country time", "ref": "61.109(a)(5)(i)", "kind": "hours", "col": "solo_xc", "need": 5},
        {"code": "long_xc", "label": "One solo cross-country of 150 nm total, full-stop landings at 3 points, one leg over 50 nm", "ref": "61.109(a)(5)(ii)", "kind": "manual"},
        {"code": "towered", "label": "3 solo takeoffs and full-stop landings at an airport with an operating control tower", "ref": "61.109(a)(5)(iii)", "kind": "manual"},
        {"code": "knowledge", "label": "Knowledge test passed", "ref": "61.103(d)", "kind": "manual"},
        {"code": "endorse", "label": "Endorsement for the practical test", "ref": "61.103(f), 61.39", "kind": "manual"},
    ]},
    {"code": "instrument", "name": "Instrument Rating", "ref": "14 CFR 61.65(d)", "icon": "bi-cloud-fog2", "items": [
        {"code": "pic_xc", "label": "50 hours of cross-country time as PIC (10 in airplanes)", "ref": "61.65(d)(1)", "kind": "hours", "col": "pic_xc", "need": 50},
        {"code": "inst", "label": "40 hours of actual or simulated instrument time", "ref": "61.65(d)(2)", "kind": "hours", "col": "inst", "need": 40},
        {"code": "dual_inst", "label": "15 hours of instrument flight training from an authorized instructor", "ref": "61.65(d)(2)(i)", "kind": "hours", "col": "dual_inst", "need": 15},
        {"code": "prep", "label": "3 hours of instrument training within 2 calendar months of the test", "ref": "61.65(d)(2)(ii)", "kind": "hours", "col": "dual_inst", "need": 3, "window": "2cal"},
        {"code": "ifr_xc", "label": "IFR cross-country of 250 nm with an approach at each airport and 3 different kinds of approaches", "ref": "61.65(d)(2)(iii)", "kind": "manual"},
        {"code": "knowledge", "label": "Knowledge test passed", "ref": "61.65(a)(5)", "kind": "manual"},
        {"code": "endorse", "label": "Endorsement for the practical test", "ref": "61.65(a)(6)", "kind": "manual"},
    ]},
    {"code": "commercial", "name": "Commercial Pilot", "ref": "14 CFR 61.129(a)", "icon": "bi-briefcase", "items": [
        {"code": "total", "label": "250 hours of flight time", "ref": "61.129(a)", "kind": "hours", "col": "total", "need": 250},
        {"code": "powered", "label": "100 hours in powered aircraft (50 in airplanes)", "ref": "61.129(a)(1)", "kind": "hours", "col": "total", "need": 100},
        {"code": "pic", "label": "100 hours as pilot in command", "ref": "61.129(a)(2)", "kind": "hours", "col": "pic", "need": 100},
        {"code": "pic_xc", "label": "50 hours of cross-country as PIC (10 in airplanes)", "ref": "61.129(a)(2)(ii)", "kind": "hours", "col": "pic_xc", "need": 50},
        {"code": "dual", "label": "20 hours of training on the commercial areas of operation", "ref": "61.129(a)(3)", "kind": "hours", "col": "dual", "need": 20},
        {"code": "dual_inst", "label": "10 hours of instrument training (5 in a single-engine airplane)", "ref": "61.129(a)(3)(i)", "kind": "hours", "col": "dual_inst", "need": 10},
        {"code": "complex", "label": "10 hours of training in a complex, turbine or technically advanced airplane", "ref": "61.129(a)(3)(ii)", "kind": "manual"},
        {"code": "day_xc", "label": "2-hour day VFR cross-country with an instructor, more than 100 nm straight-line", "ref": "61.129(a)(3)(iii)", "kind": "manual"},
        {"code": "night_xc", "label": "2-hour night VFR cross-country with an instructor, more than 100 nm straight-line", "ref": "61.129(a)(3)(iv)", "kind": "manual"},
        {"code": "prep", "label": "3 hours of practical test prep training within 2 calendar months of the test", "ref": "61.129(a)(3)(v)", "kind": "hours", "col": "dual", "need": 3, "window": "2cal"},
        {"code": "solo", "label": "10 hours of solo (or PIC performing the duties with an instructor aboard)", "ref": "61.129(a)(4)", "kind": "hours", "col": "solo", "need": 10},
        {"code": "long_xc", "label": "One cross-country of 300 nm total, landings at 3 points, one leg of 250 nm straight-line", "ref": "61.129(a)(4)(i)", "kind": "manual"},
        {"code": "night_towered", "label": "5 hours of night VFR with 10 takeoffs and landings at a towered airport", "ref": "61.129(a)(4)(ii)", "kind": "manual"},
        {"code": "knowledge", "label": "Knowledge test passed", "ref": "61.123(c)", "kind": "manual"},
        {"code": "endorse", "label": "Endorsement for the practical test", "ref": "61.123(d)", "kind": "manual"},
    ]},
    {"code": "multi", "name": "Multi-Engine Rating (add-on)", "ref": "14 CFR 61.63(c)", "icon": "bi-fan", "items": [
        {"code": "training", "label": "Training in a multi-engine airplane on the areas of operation", "ref": "61.63(c)(1)", "kind": "manual"},
        {"code": "endorse", "label": "Endorsement that you're prepared for the practical test", "ref": "61.63(c)(2)", "kind": "manual"},
        {"code": "checkride", "label": "Practical test passed", "ref": "61.63(c)(3)", "kind": "manual"},
    ]},
    {"code": "cfi", "name": "Flight Instructor (CFI)", "ref": "14 CFR 61.183", "icon": "bi-mortarboard", "items": [
        {"code": "commercial", "label": "Commercial or ATP certificate", "ref": "61.183(c)", "kind": "cert", "cert": ("commercial", "atp")},
        {"code": "instrument", "label": "Instrument rating", "ref": "61.183(c)", "kind": "cert", "rating": "instrument"},
        {"code": "foi", "label": "Fundamentals of instructing: endorsement and knowledge test", "ref": "61.183(d)-(e)", "kind": "manual"},
        {"code": "knowledge", "label": "CFI aeronautical knowledge test passed", "ref": "61.183(f)", "kind": "manual"},
        {"code": "proficiency", "label": "Endorsement on the flight proficiency areas of operation", "ref": "61.183(g)", "kind": "manual"},
        {"code": "spins", "label": "Spin training endorsement (stall awareness, spin entry, spins, recovery)", "ref": "61.183(i)", "kind": "manual"},
    ]},
    {"code": "tailwheel", "name": "Tailwheel Endorsement", "ref": "14 CFR 61.31(i)", "icon": "bi-record-circle", "items": [
        {"code": "normal_xwind", "label": "Flight training in a tailwheel airplane on normal and crosswind takeoffs and landings", "ref": "61.31(i)(1)(i)", "kind": "manual"},
        {"code": "wheel_landings", "label": "Wheel landings (unless the manufacturer recommends against them)", "ref": "61.31(i)(1)(ii)", "kind": "manual"},
        {"code": "go_around", "label": "Go-around procedures", "ref": "61.31(i)(1)(iii)", "kind": "manual"},
        {"code": "endorse", "label": "One-time tailwheel endorsement in your logbook", "ref": "61.31(i)(1)", "kind": "manual"},
    ]},
    {"code": "complex", "name": "Complex Endorsement", "ref": "14 CFR 61.31(e)", "icon": "bi-gear-wide-connected", "items": [
        {"code": "training", "label": "Ground and flight training in a complex airplane (or sim/FTD)", "ref": "61.31(e)(1)(i)", "kind": "manual"},
        {"code": "endorse", "label": "One-time complex endorsement in your logbook", "ref": "61.31(e)(1)(ii)", "kind": "manual"},
    ]},
    {"code": "high_performance", "name": "High Performance Endorsement", "ref": "14 CFR 61.31(f)", "icon": "bi-speedometer2", "items": [
        {"code": "training", "label": "Ground and flight training in a high-performance airplane (or sim/FTD)", "ref": "61.31(f)(1)(i)", "kind": "manual"},
        {"code": "endorse", "label": "One-time high-performance endorsement in your logbook", "ref": "61.31(f)(1)(ii)", "kind": "manual"},
    ]},
    {"code": "cfii", "name": "Instrument Instructor (CFII)", "ref": "14 CFR 61.183", "icon": "bi-cloud-check", "items": [
        {"code": "cfi", "label": "Flight instructor certificate", "ref": "61.183", "kind": "cert", "rating": "cfi"},
        {"code": "instrument", "label": "Instrument rating", "ref": "61.183(c)", "kind": "cert", "rating": "instrument"},
        {"code": "knowledge", "label": "Instrument instructor knowledge test passed", "ref": "61.183(f)", "kind": "manual"},
        {"code": "endorse", "label": "Endorsement for the practical test", "ref": "61.183(g)", "kind": "manual"},
        {"code": "checkride", "label": "Practical test passed", "ref": "61.183(h)", "kind": "manual"},
    ]},
    {"code": "mei", "name": "Multi-Engine Instructor (MEI)", "ref": "14 CFR 61.183", "icon": "bi-people", "items": [
        {"code": "cfi", "label": "Flight instructor certificate", "ref": "61.183", "kind": "cert", "rating": "cfi"},
        {"code": "multi", "label": "Multi-engine rating", "ref": "61.183(c)", "kind": "cert", "rating": "multi_engine"},
        {"code": "endorse", "label": "Endorsement for the practical test", "ref": "61.183(g)", "kind": "manual"},
        {"code": "checkride", "label": "Practical test passed", "ref": "61.183(h)", "kind": "manual"},
    ]},
    {"code": "atp", "name": "Airline Transport Pilot", "ref": "14 CFR 61.159(a)", "icon": "bi-trophy", "items": [
        {"code": "total", "label": "1,500 hours of total time as a pilot", "ref": "61.159(a)", "kind": "hours", "col": "total", "need": 1500},
        {"code": "xc", "label": "500 hours of cross-country time", "ref": "61.159(a)(1)", "kind": "hours", "col": "xc", "need": 500},
        {"code": "night", "label": "100 hours of night time", "ref": "61.159(a)(2)", "kind": "hours", "col": "night", "need": 100},
        {"code": "class", "label": "50 hours in the class of airplane", "ref": "61.159(a)(3)", "kind": "hours", "col": "total", "need": 50},
        {"code": "inst", "label": "75 hours of actual or simulated instrument time", "ref": "61.159(a)(4)", "kind": "hours", "col": "inst", "need": 75},
        {"code": "pic", "label": "250 hours as pilot in command", "ref": "61.159(a)(5)", "kind": "hours", "col": "pic", "need": 250},
        {"code": "pic_xc", "label": "100 hours of cross-country PIC", "ref": "61.159(a)(5)", "kind": "hours", "col": "pic_xc", "need": 100},
        {"code": "pic_night", "label": "25 hours of night PIC", "ref": "61.159(a)(5)", "kind": "hours", "col": "pic_night", "need": 25},
    ]},
]
TRACK_MAP = {t["code"]: t for t in TRACKS}
# Which track to open first for a student holding this certificate.
NEXT_TRACK = {None: "private", "": "private", "student": "private", "sport": "private", "recreational": "private",
              "private": "instrument", "commercial": "cfi", "atp": "atp"}
# Profile "Training toward" (flight.TRAINING_GOALS code) -> track code.
GOAL_TRACK = {"multi_engine": "multi"}
_CERT_ORDER = ["student", "sport", "recreational", "private", "commercial", "atp"]


def _row_get(row, key):
    try:
        return row[key] if key in row.keys() else None
    except Exception:
        return None


def goal_track_code(student):
    """The track this student is working toward. The "Training toward" goal on
    their profile wins (e.g. Tailwheel); without one it's the next step after
    their certificate. Never a certificate or rating they already hold, and
    None for an ATP holder with no goal set (nothing left to work toward)."""
    if student is None:
        return "private"
    cert = _row_get(student, "pilot_certificate") or ""
    ratings = {r.strip() for r in (_row_get(student, "pilot_ratings") or "").split(",") if r.strip()}
    held = set(ratings)
    if cert in _CERT_ORDER:
        held.update(_CERT_ORDER[:_CERT_ORDER.index(cert) + 1])
    goal = _row_get(student, "training_goal")
    if goal and goal not in held:
        code = GOAL_TRACK.get(goal, goal)
        if code in TRACK_MAP:
            return code
    code = NEXT_TRACK.get(cert or None, "private")
    if code == "instrument" and "instrument" in held:
        code = "commercial"
    if code == "cfi" and "cfi" in held:
        code = "atp"
    if code == "atp" and cert == "atp":
        return None
    return code


# ---------------------------------------------------------------- helpers

def _access_ok():
    return bool(session.get("is_master_admin") or session.get("academy_access"))


def _is_staff():
    return bool(session.get("is_master_admin") or session.get("cfi_id"))


def _can_edit(entry):
    """Only the student who owns the entry, or a Flight Admin, may change a logbook entry.
    An instructor (CFI) can read their students' logbooks but never edit them."""
    from auth import is_flight_admin
    return bool(entry["student_id"] == session.get("student_id") or is_flight_admin())


def _can_import(student):
    """A ForeFlight import goes only into your OWN logbook: the signed-in account's own student
    profile. Not an admin (or CFI) importing for somebody else, and not while looking at
    someone's view."""
    from auth import real_user_id
    return bool(student and student["user_id"] and student["user_id"] == real_user_id()
                and not session.get("_view_as_readonly"))


def _require_access(what="your logbook"):
    """SCHOOL-36: the redirect to send back when this page can't be shown,
    or None. Nobody signed in (an old notification link after the session
    expired) goes to the login page with a plain "please sign in" and comes
    back here afterwards (session['login_next'], honoured by the login
    form in app.home_launcher) - only a signed-in account without Academy
    access is told to ask an admin."""
    if not session.get("user_id"):
        session["login_next"] = request.full_path.rstrip("?")
        flash(f"Please sign in to see {what}.", "info")
        return redirect(url_for("home_launcher"))
    if not _access_ok():
        flash("You don't have access to Flight Academy yet. Ask an admin.", "danger")
        return redirect(url_for("home_launcher"))
    return None


def _num(v, kind="hrs"):
    v = (v or "").strip() if isinstance(v, str) else v
    if v in (None, ""):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n < 0 or n > 100000:
        return None
    return int(round(n)) if kind == "int" else round(n, 1)


def flight_times(f):
    """(total, dual, solo, pic) for a flight row. Total is Hobbs time (else
    Tach, when no Hobbs readings were entered). A solo flight is all solo/PIC; otherwise flights.solo_hours is
    the part the student flew alone and the rest is dual received."""
    if f["hobbs_start"] is not None and f["hobbs_end"] is not None:
        total = max(0.0, f["hobbs_end"] - f["hobbs_start"])
    elif f["tach_start"] is not None and f["tach_end"] is not None:
        total = max(0.0, f["tach_end"] - f["tach_start"])
    else:
        total = 0.0
    total = round(total, 1)
    keys = f.keys() if hasattr(f, "keys") else f
    solo_part = f["solo_hours"] if "solo_hours" in keys and f["solo_hours"] else 0.0
    if f["solo"]:
        solo = total
    else:
        solo = round(min(max(solo_part, 0.0), total), 1)
    dual = round(total - solo, 1)
    return total, dual, solo, solo


def ensure_entry(conn, flight_id):
    """Create (or, while still pending, refresh) the logbook entry for one
    finished flight. Called from flight.py wherever a flight is logged,
    ended or edited. A completed (student-approved) entry is left alone."""
    f = conn.execute("""SELECT f.*, a.tag, a.make, a.model, c.name as cfi_name,
                               c.cfi_cert_number, c.cfi_cert_expires, c.signature as cfi_signature
                        FROM flights f JOIN assets a ON a.id = f.asset_id
                        LEFT JOIN cfis c ON c.id = f.cfi_id WHERE f.id = ?""", (flight_id,)).fetchone()
    if not f or (f["started_at"] and not f["ended_at"]):
        return
    total, dual, solo, pic = flight_times(f)
    make_model = " ".join(x for x in [f["make"], f["model"]] if x) or None
    day_ldg = (f["day_landings_fs"] or 0) + (f["day_landings_tg"] or 0)
    night_ldg = (f["night_landings_fs"] or 0) + (f["night_landings_tg"] or 0)
    instructor = ((f["cfi_name"], f["cfi_cert_number"], f["cfi_cert_expires"], f["cfi_signature"])
                  if (f["cfi_id"] and dual > 0) else (None, None, None, None))
    existing = conn.execute("SELECT * FROM pilot_logbook WHERE flight_id = ?", (flight_id,)).fetchone()
    if existing is None:
        conn.execute("""INSERT INTO pilot_logbook (flight_id, student_id, cfi_id, asset_id, entry_date, tail, make_model,
                        total, dual, solo, pic, day_ldg, night_ldg, night_fs, instructor_name, instructor_cert,
                        instructor_cert_exp, instructor_signature, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                     (flight_id, f["student_id"], f["cfi_id"], f["asset_id"], f["flight_date"], f["tag"], make_model,
                      total, dual, solo, pic, day_ldg, night_ldg, f["night_landings_fs"] or 0, *instructor,
                      now_iso(), now_iso()))
    elif existing["status"] != "pending":
        # LOCK: the times come from the billed flight and nothing else, even once the student approved the
        # entry. A correction made on the flight flows through; the student's own sub-times (cross-country,
        # night, instrument, PIC) can never exceed the flight's total.
        conn.execute("""UPDATE pilot_logbook SET total=?, dual=?, solo=?,
                        pic=MIN(pic, ?), xc=MIN(xc, ?), night=MIN(night, ?), actual_inst=MIN(actual_inst, ?), sim_inst=MIN(sim_inst, ?),
                        updated_at=? WHERE id=?""",
                     (total, dual, solo, total, total, total, total, total, now_iso(), existing["id"]))
        _cap_instrument(conn, existing["id"], total)
    elif existing["status"] == "pending":
        conn.execute("""UPDATE pilot_logbook SET student_id=?, cfi_id=?, asset_id=?, entry_date=?, tail=?, make_model=?,
                        total=?, dual=?, solo=?, pic=?, day_ldg=?, night_ldg=?, night_fs=?, instructor_name=?,
                        instructor_cert=?, instructor_cert_exp=?, instructor_signature=?, updated_at=? WHERE id=?""",
                     (f["student_id"], f["cfi_id"], f["asset_id"], f["flight_date"], f["tag"], make_model,
                      total, dual, solo, pic, day_ldg, night_ldg, f["night_landings_fs"] or 0, *instructor,
                      now_iso(), existing["id"]))


def _cap_instrument(conn, entry_id, total):
    """Actual + simulated instrument together can't be more than the flight's total."""
    r = conn.execute("SELECT actual_inst, sim_inst FROM pilot_logbook WHERE id = ?", (entry_id,)).fetchone()
    if r and (r["actual_inst"] or 0) + (r["sim_inst"] or 0) > total + 1e-9:
        conn.execute("UPDATE pilot_logbook SET sim_inst=? WHERE id=?", (round(max(0.0, total - (r["actual_inst"] or 0)), 1), entry_id))


def remove_for_flight(conn, flight_id):
    """A deleted flight takes its still-pending logbook entry with it."""
    conn.execute("DELETE FROM pilot_logbook WHERE flight_id = ? AND status = 'pending'", (flight_id,))


def _entry_values(e):
    """Every logbook column the requirements total, for one entry."""
    g = lambda k: (e[k] or 0)
    dual, solo, pic, xc, night = g("dual"), g("solo"), g("pic"), g("xc"), g("night")
    inst = g("actual_inst") + g("sim_inst")
    return {
        "total": g("total"), "dual": dual, "solo": solo, "pic": pic, "xc": xc, "night": night, "inst": inst,
        "dual_xc": min(xc, dual), "solo_xc": min(xc, solo), "pic_xc": min(xc, pic),
        "dual_night": min(night, dual), "pic_night": min(night, pic), "dual_inst": min(inst, dual),
        "night_fs": g("night_fs"), "all_ldg": g("day_ldg") + g("night_ldg"),
    }


def _two_cal_months_start(today=None):
    today = today or date.today()
    m = today.month - 2
    y = today.year
    if m < 1:
        m += 12
        y -= 1
    return date(y, m, 1).isoformat()


def build_progress(conn, student):
    entries = conn.execute("""SELECT * FROM pilot_logbook WHERE student_id = ? AND status != 'denied' ORDER BY entry_date, id""",
                           (student["id"],)).fetchall()
    vals = [(e, _entry_values(e)) for e in entries]
    miles = {(r["track"], r["item"]): r for r in conn.execute(
        "SELECT * FROM academy_milestones WHERE student_id = ?", (student["id"],)).fetchall()}
    linked = {}
    for r in conn.execute("""SELECT mf.track, mf.item, l.id, l.entry_date, l.tail, l.route_from, l.route_to, l.total
                             FROM academy_milestone_flights mf JOIN pilot_logbook l ON l.id = mf.entry_id
                             WHERE mf.student_id = ? AND l.status != 'denied' ORDER BY l.entry_date, l.id""", (student["id"],)).fetchall():
        linked.setdefault((r["track"], r["item"]), []).append(dict(r))
    cert = student["pilot_certificate"] or ""
    ratings = [r.strip() for r in (student["pilot_ratings"] or "").split(",") if r.strip()]
    window_start = _two_cal_months_start()
    today = date.today().isoformat()
    tracks = []
    for t in TRACKS:
        items = []
        for it in t["items"]:
            row = {**it, "have": 0, "pct": 0, "done": False, "flights": [], "who": None}
            if it["kind"] in ("hours", "count"):
                total = 0.0
                for e, v in vals:
                    if it.get("window") == "2cal" and e["entry_date"] < window_start:
                        continue
                    amt = v[it["col"]]
                    if amt:
                        total += amt
                        row["flights"].append({"id": e["id"], "date": e["entry_date"], "tail": e["tail"],
                                               "amount": amt, "status": e["status"]})
                row["have"] = round(total, 1) if it["kind"] == "hours" else int(total)
                row["pct"] = min(100, int(total / it["need"] * 100)) if it["need"] else 100
                row["done"] = total >= it["need"]
            elif it["kind"] == "manual":
                m = miles.get((t["code"], it["code"]))
                row["done"] = bool(m)
                row["who"] = m
                row["pct"] = 100 if m else 0
                row["linked"] = linked.get((t["code"], it["code"]), [])
            elif it["kind"] == "cert":
                ok = (cert in it["cert"]) if it.get("cert") else (it["rating"] in ratings)
                row["done"], row["pct"] = ok, 100 if ok else 0
            elif it["kind"] == "solo_signoff":
                ok = bool(student["solo_signoff_date"]) and (not student["solo_signoff_expires"]
                                                             or student["solo_signoff_expires"] >= today)
                row["done"], row["pct"] = ok, 100 if ok else 0
                row["have"] = student["solo_signoff_date"]
            items.append(row)
        pct = int(sum(i["pct"] for i in items) / len(items)) if items else 0
        tracks.append({**t, "items": items, "pct": pct, "done_count": sum(1 for i in items if i["done"])})
    return tracks


def active_goal_track(conn, student):
    """The track for this student's current next goal (the same one
    Progress opens to by default) - a compact summary for the Logbook tab
    so the goal is visible without switching tabs. None if there's nothing
    to show (no student picked, or that track is already fully done)."""
    if not student:
        return None
    code = goal_track_code(student)
    if not code:
        return None
    tracks = build_progress(conn, student)
    track = next((t for t in tracks if t["code"] == code), None)
    if not track or track["pct"] >= 100:
        return None
    return track


def _totals(entries):
    keys = ["total", "dual", "solo", "pic", "xc", "night", "actual_inst", "sim_inst", "approaches",
            "day_ldg", "night_ldg"]
    return {k: round(sum((e[k] or 0) for e in entries), 1) for k in keys}


def target_eta(entries, track):
    """Weeks-to-go for a chosen target track's headline hours requirement
    (the "totalizar" button's estimate), based on this student's own
    average hours/week so far - not a generic rule of thumb. None if the
    track has no hours requirement to project, or there isn't enough
    logbook history yet (fewer than 2 distinct flight dates, or no hours
    logged in that span)."""
    hour_items = [i for i in track["items"] if i["kind"] == "hours"]
    if not hour_items:
        return None
    # The track's overall "total flight time" item if it has one, else
    # whichever single item needs the most hours - the likely long pole.
    headline = next((i for i in hour_items if i["col"] == "total"), None) or max(hour_items, key=lambda i: i["need"])
    remaining = round(max(0.0, headline["need"] - headline["have"]), 1)
    if remaining <= 0:
        return {"headline": headline, "remaining": 0, "weeks": 0}
    dates = sorted(set(e["entry_date"] for e in entries))
    if len(dates) < 2:
        return None
    days = max(1, (date.fromisoformat(dates[-1][:10]) - date.fromisoformat(dates[0][:10])).days)
    hrs_per_week = sum((e["total"] or 0) for e in entries) / (days / 7)
    if hrs_per_week <= 0:
        return None
    return {"headline": headline, "remaining": remaining, "weeks": round(remaining / hrs_per_week, 1),
            "hrs_per_week": round(hrs_per_week, 1)}


def _pick_student(conn):
    """The student whose logbook/progress is shown: yourself, or (staff)
    whoever ?student_id picks. Returns (student, students_for_picker)."""
    if _is_staff():
        students = conn.execute("""SELECT * FROM students WHERE active = 1 AND COALESCE(is_station, 0) = 0
                                   ORDER BY name""").fetchall()
        # A staff pick sticks across the logbook / totalizer / endorsements / map tabs.
        ids = {r["id"] for r in students}
        asked = request.values.get("student_id", type=int)
        if asked in ids:
            session["picked_student_id"] = asked
        sticky = session.get("picked_student_id")
        sid = asked or (sticky if sticky in ids else None) or session.get("student_id") or \
            (students[0]["id"] if students else None)
        student = conn.execute("SELECT * FROM students WHERE id = ?", (sid,)).fetchone() if sid else None
        return student, students
    sid = session.get("student_id")
    student = conn.execute("SELECT * FROM students WHERE id = ?", (sid,)).fetchone() if sid else None
    return student, []


def _can_touch(entry):
    return _is_staff() or entry["student_id"] == session.get("student_id")


# ---------------------------------------------------------------- routes

@pilotlog_bp.route("/logbook")
def logbook():
    denied = _require_access()
    if denied:
        return denied
    conn = get_db()
    student, students = _pick_student(conn)
    entries = conn.execute("""SELECT * FROM pilot_logbook WHERE student_id = ? AND status != 'denied'
                              ORDER BY entry_date DESC, id DESC""", (student["id"],)).fetchall() if student else []
    denied_entries = conn.execute("""SELECT * FROM pilot_logbook WHERE student_id = ? AND status = 'denied'
                              ORDER BY entry_date DESC, id DESC""", (student["id"],)).fetchall() if student else []
    dual_given = None
    if session.get("cfi_id"):
        r = conn.execute("SELECT SUM(dual) d, COUNT(*) c FROM pilot_logbook WHERE cfi_id = ? AND dual > 0 AND status != 'denied'",
                         (session["cfi_id"],)).fetchone()
        dual_given = {"hours": round(r["d"] or 0, 1), "flights": r["c"]}
    goal_track = active_goal_track(conn, student) if student else None
    # Log Your Flying (things the flight log doesn't track) lives here, under Logbook.
    log_entries = conn.execute("""SELECT * FROM academy_entries WHERE student_id = ?
                                  ORDER BY entry_date DESC, id DESC LIMIT 25""", (student["id"],)).fetchall() if student else []
    conn.close()
    pending = [e for e in entries if e["status"] == "pending"]
    extra = {k: round(sum((e[k] or 0) for e in entries), 1) for k in ("sic", "dual_given")}
    import academy
    return render_template("academy_logbook.html", denied_entries=denied_entries, log_entries=log_entries, log_kinds=academy.ENTRY_KINDS,
                           log_kind_map=academy.ENTRY_KIND_MAP, log_today=date.today().isoformat(), student=student, students=students, entries=entries,
                           pending=pending, totals=_totals(entries), extra=extra, can_import=_can_import(student), staff=_is_staff(), dual_given=dual_given,
                           goal_track=goal_track, tab="logbook")


@pilotlog_bp.route("/logbook/<int:entry_id>", methods=["GET", "POST"])
def entry(entry_id):
    denied = _require_access()
    if denied:
        return denied
    conn = get_db()
    e = conn.execute("""SELECT l.*, s.name as student_name FROM pilot_logbook l JOIN students s ON s.id = l.student_id
                        WHERE l.id = ?""", (entry_id,)).fetchone()
    if not e or not _can_touch(e):
        conn.close()
        abort(404)
    if request.method == "POST":
        if not _can_edit(e):
            conn.close()
            flash("Instructors can read a student's logbook but only the student (or a Flight Admin) can change it.", "warning")
            return redirect(url_for("pilotlog.entry", entry_id=entry_id))
        if e["status"] in ("complete", "denied") and not _is_staff() and request.form.get("action") != "reopen":
            conn.close()
            flash("This entry is already approved. Reopen it to make changes.", "warning")
            return redirect(url_for("pilotlog.entry", entry_id=entry_id))
        if request.form.get("action") == "deny":
            if e["status"] != "pending":
                conn.close()
                flash("Only a pending entry can be denied.", "warning")
                return redirect(url_for("pilotlog.entry", entry_id=entry_id))
            reason = (request.form.get("deny_reason") or "").strip()[:300] or None
            conn.execute("""UPDATE pilot_logbook SET status='denied', denied_at=?, denied_by=?, deny_reason=?, updated_at=? WHERE id=?""",
                         (now_iso(), session.get("user_name"), reason, now_iso(), entry_id))
            conn.commit()
            conn.close()
            flash("Entry denied - it won't count toward totals or requirements. You can reopen it any time.", "success")
            return redirect(url_for("pilotlog.logbook", student_id=e["student_id"] if _is_staff() else None))
        if request.form.get("action") == "reopen":
            conn.execute("UPDATE pilot_logbook SET status='pending', approved_at=NULL, approved_by=NULL, denied_at=NULL, denied_by=NULL, deny_reason=NULL, updated_at=? WHERE id=?",
                         (now_iso(), entry_id))
            conn.commit()
            conn.close()
            flash("Entry reopened - make your changes and approve it again.", "success")
            return redirect(url_for("pilotlog.entry", entry_id=entry_id))
        sets, args = [], []
        for field, _label, kind in STUDENT_FIELDS:
            raw = request.form.get(field)
            if kind == "text":
                val = (raw or "").strip()[:500] or None
            else:
                val = _num(raw, kind)
            sets.append(f"{field}=?")
            args.append(val)
        # LOCK: the times of a school flight come from the billed flight, for everyone (admins too):
        # correct the flight itself and the entry follows. Only an entry with no flight behind it
        # (not one the school billed) can have its times typed in, and only by staff.
        total_val = e["total"] or 0
        if e["flight_id"] is None and _is_staff():
            total_val = _num(request.form.get("total")) or 0
            for field in ("total", "dual", "solo"):
                sets.append(f"{field}=?")
                args.append(_num(request.form.get(field)) or 0)
        # A student can't log more of anything than the flight itself lasted.
        for i, (field, _label, kind) in enumerate(STUDENT_FIELDS):
            if field in ("pic", "xc", "night", "actual_inst", "sim_inst") and args[i] is not None:
                args[i] = min(args[i], total_val)
        ai, si = [i for i, f in enumerate(STUDENT_FIELDS) if f[0] == "actual_inst"][0], [i for i, f in enumerate(STUDENT_FIELDS) if f[0] == "sim_inst"][0]
        if (args[ai] or 0) + (args[si] or 0) > total_val:
            args[si] = round(max(0.0, total_val - (args[ai] or 0)), 1)
        approve = request.form.get("action") == "approve"
        if approve:
            sets += ["status='complete'", "approved_at=?", "approved_by=?"]
            args += [now_iso(), session.get("user_name")]
        sets.append("updated_at=?")
        args.append(now_iso())
        conn.execute(f"UPDATE pilot_logbook SET {', '.join(sets)} WHERE id=?", [*args, entry_id])
        conn.commit()
        conn.close()
        flash("Logbook entry approved - it's complete." if approve else "Saved. It stays pending until you approve it.",
              "success")
        if approve:
            return redirect(url_for("pilotlog.logbook", student_id=e["student_id"] if _is_staff() else None))
        return redirect(url_for("pilotlog.entry", entry_id=entry_id))
    conn.close()
    return render_template("academy_logbook_entry.html", e=e, fields=STUDENT_FIELDS, staff=_is_staff(), can_edit=_can_edit(e), tab="logbook",
                           times_locked=e["flight_id"] is not None or not _is_staff())


# ---------------------------------------------------------------- ForeFlight import

_IMPORT_COLS = ("entry_date", "tail", "make_model", "route_from", "route_to", "route", "total", "dual", "dual_given", "solo", "pic", "sic",
                "xc", "night", "ifr", "actual_inst", "sim_inst", "sim_flight", "ground", "approaches", "day_ldg", "night_ldg", "night_fs",
                "remarks", "instructor_name", "import_key")


def _import_dir():
    import os
    import db as _db
    d = os.path.join(os.path.dirname(_db.DB_PATH), "imports")
    os.makedirs(d, exist_ok=True)
    return d


def _looks_logged(entry, existing):
    """A ForeFlight flight that is probably one the school already logged: same day, same plane, hours within 0.3."""
    for x in existing:
        if x["entry_date"] == entry["entry_date"] and (x["tail"] or "").upper() == (entry["tail"] or "") \
                and abs((x["total"] or 0) - entry["total"]) <= 0.3:
            return True
    return False


@pilotlog_bp.route("/logbook/import", methods=["GET", "POST"])
def import_foreflight():
    """Upload a ForeFlight logbook export, look over what it would add, then confirm."""
    import json
    import os
    import re
    import secrets
    import time
    import foreflight_import
    denied = _require_access()
    if denied:
        return denied
    conn = get_db()
    student, _students = _pick_student(conn)
    if not student:
        conn.close()
        flash("No student profile is linked to your account, so there's no logbook to import into.", "warning")
        return redirect(url_for("pilotlog.logbook"))
    if not _can_import(student):
        conn.close()
        flash("A ForeFlight import can only go into your own logbook, from your own account.", "warning")
        return redirect(url_for("pilotlog.logbook", student_id=student["id"] if _is_staff() else None))
    sid_arg = student["id"] if _is_staff() else None
    folder = _import_dir()
    for f in os.listdir(folder):    # forget old previews
        fp = os.path.join(folder, f)
        if time.time() - os.path.getmtime(fp) > 86400:
            os.remove(fp)

    if request.method == "POST" and request.form.get("action") == "confirm":
        token = request.form.get("token", "")
        path = os.path.join(folder, token + ".json") if re.fullmatch(r"[A-Za-z0-9_-]{16,40}", token) else None
        if not path or not os.path.exists(path):
            conn.close()
            flash("That preview has expired. Upload the file again.", "warning")
            return redirect(url_for("pilotlog.import_foreflight", student_id=sid_arg))
        with open(path) as fh:
            pending = json.load(fh)
        if pending["student_id"] != student["id"] or pending["user_id"] != session.get("user_id"):
            conn.close()
            abort(403)
        rows = pending["new"] + (pending["maybe"] if request.form.get("include_maybe") else [])
        added = 0
        marks = ",".join("?" * (len(_IMPORT_COLS) + 5))
        for e in rows:
            cur = conn.execute(f"""INSERT OR IGNORE INTO pilot_logbook (student_id, {', '.join(_IMPORT_COLS)}, status, approved_at, approved_by, source)
                                   VALUES ({marks})""",
                               [student["id"]] + [e[c] for c in _IMPORT_COLS] + ["complete", now_iso(), "Imported from ForeFlight", "foreflight"])
            added += cur.rowcount
        conn.commit()
        conn.close()
        os.remove(path)
        flash(f"Imported {added} flight{'s' if added != 1 else ''} from ForeFlight into {student['name']}'s logbook.", "success")
        return redirect(url_for("pilotlog.logbook", student_id=sid_arg))

    preview = None
    if request.method == "POST":
        up = request.files.get("file")
        raw = up.read(5 * 1024 * 1024 + 1) if up else b""
        if not raw:
            flash("Choose the ForeFlight CSV file first.", "warning")
        elif len(raw) > 5 * 1024 * 1024:
            flash("That file is over 5 MB - too big to be a ForeFlight logbook export.", "warning")
        else:
            parsed = foreflight_import.parse(raw.decode("utf-8-sig", errors="replace"))
            if parsed["problem"]:
                flash(parsed["problem"], "danger")
            else:
                have_keys = {r["import_key"] for r in conn.execute(
                    "SELECT import_key FROM pilot_logbook WHERE student_id = ? AND import_key IS NOT NULL", (student["id"],))}
                school = conn.execute("""SELECT entry_date, tail, total FROM pilot_logbook WHERE student_id = ? AND status != 'denied'
                                         AND import_key IS NULL""", (student["id"],)).fetchall()
                new, maybe, already = [], [], 0
                for e in parsed["entries"]:
                    if e["import_key"] in have_keys:
                        already += 1
                    elif _looks_logged(e, school):
                        maybe.append(e)
                    else:
                        new.append(e)
                token = secrets.token_urlsafe(16)
                with open(os.path.join(folder, token + ".json"), "w") as fh:
                    json.dump({"student_id": student["id"], "user_id": session.get("user_id"), "new": new, "maybe": maybe}, fh)
                dates = [e["entry_date"] for e in new] or [e["entry_date"] for e in parsed["entries"]]
                preview = {"token": token, "new": len(new), "maybe": len(maybe), "already": already, "skipped": parsed["skipped"],
                           "aircraft": parsed["aircraft"], "flags": parsed["flags"], "totals": foreflight_import.totals(new),
                           "first": min(dates) if dates else None, "last": max(dates) if dates else None,
                           "sample": sorted(new, key=lambda x: x["entry_date"], reverse=True)[:8], "maybe_sample": maybe[:5]}
    conn.close()
    return render_template("academy_logbook_import.html", student=student, preview=preview, staff=_is_staff(), tab="logbook")


@pilotlog_bp.route("/logbook/export.csv")
def export_csv():
    if not _access_ok():
        return redirect(url_for("home_launcher"))
    conn = get_db()
    student, _students = _pick_student(conn)
    if not student:
        conn.close()
        abort(404)
    entries = conn.execute("SELECT * FROM pilot_logbook WHERE student_id = ? AND status != 'denied' ORDER BY entry_date, id",
                           (student["id"],)).fetchall()
    conn.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([label for _k, label in EXPORT_COLUMNS])
    for e in entries:
        w.writerow(["" if e[k] is None else e[k] for k, _l in EXPORT_COLUMNS])
    t = _totals(entries)
    w.writerow(["TOTALS", "", "", "", "", t["total"], t["dual"], t["solo"], t["pic"], t["xc"], t["night"],
                t["actual_inst"], t["sim_inst"], t["approaches"], t["day_ldg"], t["night_ldg"]])
    fname = f"logbook-{student['name'].replace(' ', '_')}-{date.today().isoformat()}.csv"
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@pilotlog_bp.route("/progress")
def progress():
    denied = _require_access()
    if denied:
        return denied
    conn = get_db()
    student, students = _pick_student(conn)
    tracks = build_progress(conn, student) if student else []
    pending_count = conn.execute("SELECT COUNT(*) c FROM pilot_logbook WHERE student_id = ? AND status = 'pending'",
                                 (student["id"],)).fetchone()["c"] if student else 0
    flights = [dict(r) for r in conn.execute("""SELECT id, entry_date, tail, route_from, route_to, total FROM pilot_logbook
                                                WHERE student_id = ? AND status != 'denied' ORDER BY entry_date DESC, id DESC""",
                                             (student["id"],)).fetchall()] if (student and _is_staff()) else []
    conn.close()
    open_track = request.args.get("track") or goal_track_code(student)
    return render_template("academy_progress.html", student=student, students=students, tracks=tracks, flights=flights,
                           open_track=open_track, staff=_is_staff(), pending_count=pending_count,
                           window_start=_two_cal_months_start(), tab="progress")


@pilotlog_bp.route("/progress/milestone", methods=["POST"])
def milestone():
    """An instructor ticks (or unticks) a requirement that can't be totalled
    from the logbook - a knowledge test, the long cross-country, an
    endorsement."""
    if not _access_ok() or not _is_staff():
        flash("Only an instructor can check that off.", "danger")
        return redirect(url_for("pilotlog.progress"))
    student_id = request.form.get("student_id", type=int)
    track, item = request.form.get("track"), request.form.get("item")
    t = TRACK_MAP.get(track)
    if not student_id or not t or item not in [i["code"] for i in t["items"] if i["kind"] == "manual"]:
        abort(400)
    conn = get_db()
    existing = conn.execute("SELECT id FROM academy_milestones WHERE student_id=? AND track=? AND item=?",
                            (student_id, track, item)).fetchone()
    if existing:
        conn.execute("DELETE FROM academy_milestones WHERE id = ?", (existing["id"],))
    else:
        conn.execute("""INSERT INTO academy_milestones (student_id, track, item, done_date, signed_by, note, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (student_id, track, item, (request.form.get("done_date") or date.today().isoformat())[:10],
                      session.get("user_name"), (request.form.get("note") or "").strip()[:200] or None, now_iso()))
    conn.commit()
    conn.close()
    return redirect(url_for("pilotlog.progress", student_id=student_id, track=track) + f"#t-{track}")


@pilotlog_bp.route("/progress/milestone/flights", methods=["POST"])
def milestone_flights():
    """An instructor picks the specific logbook flights that meet a checkoff requirement. Saving with at
    least one flight picked checks the requirement off (dated from the latest flight); saving with none
    just clears the picks. Students can see the picks but never change them."""
    if not _access_ok() or not _is_staff():
        flash("Only an instructor can pick the flights for a requirement.", "danger")
        return redirect(url_for("pilotlog.progress"))
    student_id = request.form.get("student_id", type=int)
    track, item = request.form.get("track"), request.form.get("item")
    t = TRACK_MAP.get(track)
    if not student_id or not t or item not in [i["code"] for i in t["items"] if i["kind"] == "manual"]:
        abort(400)
    conn = get_db()
    ids = {int(x) for x in request.form.getlist("entry_id") if x.isdigit()}
    rows = conn.execute(f"""SELECT id, entry_date FROM pilot_logbook WHERE student_id = ? AND status != 'denied'
                            AND id IN ({','.join('?' * len(ids)) or 'NULL'})""", [student_id, *ids]).fetchall() if ids else []
    conn.execute("DELETE FROM academy_milestone_flights WHERE student_id=? AND track=? AND item=?", (student_id, track, item))
    for r in rows:
        conn.execute("""INSERT OR IGNORE INTO academy_milestone_flights (student_id, track, item, entry_id, added_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)""", (student_id, track, item, r["id"], session.get("user_name"), now_iso()))
    if rows and not conn.execute("SELECT 1 FROM academy_milestones WHERE student_id=? AND track=? AND item=?",
                                 (student_id, track, item)).fetchone():
        conn.execute("""INSERT INTO academy_milestones (student_id, track, item, done_date, signed_by, note, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (student_id, track, item, max(r["entry_date"] for r in rows)[:10], session.get("user_name"),
                      f"{len(rows)} flight{'s' if len(rows) != 1 else ''} picked", now_iso()))
    conn.commit()
    conn.close()
    flash("Flights saved for that requirement." if rows else "Flights cleared for that requirement.", "success")
    return redirect(url_for("pilotlog.progress", student_id=student_id, track=track) + f"#t-{track}")


@pilotlog_bp.route("/totalizer")
def totalizer():
    """Totalizer (SCHOOL-35: one name everywhere) - a step past Progress's fixed next-track view: pick any
    certificate/rating as your target (not just the next logical one) and
    see an ETA for it based on your own average pace, not a generic
    estimate. Totals-per-category are the same numbers already on the
    Logbook tab, just given their own page alongside the target picker."""
    denied = _require_access()
    if denied:
        return denied
    conn = get_db()
    student, students = _pick_student(conn)
    entries = conn.execute("""SELECT * FROM pilot_logbook WHERE student_id = ? AND status != 'denied' ORDER BY entry_date, id""",
                           (student["id"],)).fetchall() if student else []
    tracks = build_progress(conn, student) if student else []
    conn.close()
    target_code = request.args.get("target") or goal_track_code(student) or "atp"
    target = next((t for t in tracks if t["code"] == target_code), None)
    eta = target_eta(entries, target) if target else None
    # SCHOOL-35: tab stays "logbook" (the Academy tab row and the sub-tab
    # strip key on it); subtab lights the Totalizer sub-tab.
    return render_template("academy_totalizer.html", student=student, students=students, totals=_totals(entries),
                           tracks=tracks, target=target, target_code=target_code, eta=eta, tab="logbook",
                           subtab="totalizer")


@pilotlog_bp.app_template_global()
def pending_logbook_count():
    """Pending logbook entries for the logged-in student (for the Academy
    tab badge)."""
    sid = session.get("student_id")
    if not sid:
        return 0
    conn = get_db()
    try:
        return conn.execute("SELECT COUNT(*) c FROM pilot_logbook WHERE student_id = ? AND status = 'pending'",
                            (sid,)).fetchone()["c"]
    except Exception:
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------- airport map

_AIRPORT_TOKEN = re.compile(r"[A-Za-z0-9]+")
_HOME_AIRPORT = "N89"


def _airport_codes(text):
    """Airport identifiers typed in a From / To box ("N89", "kpou", "N89-KMGJ-N89",
    "N89 via 44N"). Keeps 3-4 character tokens that have a letter; plain
    words like "via" / "and" / "local" are dropped."""
    out = []
    for tok in _AIRPORT_TOKEN.findall(text or ""):
        t = tok.upper()
        if t in ("VIA", "AND", "THE", "LOCAL", "PATT", "RTN", "RET", "BACK", "TO", "FROM", "XC", "TNG", "DUAL", "SOLO"):
            continue
        if len(t) not in (3, 4) or not any(c.isalpha() for c in t):
            continue
        if len(t) == 3 and t.isalpha():
            t = "K" + t  # POU and KPOU are the same field
        out.append(t)
    return out


def _lookup_airports(conn, codes):
    """Returns {code: row} with lat/lon for every code it can place. Unknown
    codes are looked up on aviationweather.gov in one request (3-letter
    all-letter codes also as K+code, e.g. POU -> KPOU) and cached in
    airport_coords, so each airport is only ever fetched once."""
    import json
    import urllib.parse
    import urllib.request
    from datetime import datetime, timedelta
    codes = sorted(set(codes))
    if not codes:
        return {}
    local = _lookup_local(codes)
    if local and len(local) == len(codes):
        return local
    marks = ",".join("?" * len(codes))
    have = {r["code"]: r for r in conn.execute(f"SELECT * FROM airport_coords WHERE code IN ({marks})", codes)}
    week_ago = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    todo = [c for c in codes if c not in have or (not have[c]["found"] and (have[c]["fetched_at"] or "") < week_ago)]
    if todo:
        ask = set(todo)
        ask.update("K" + c for c in todo if len(c) == 3 and c.isalpha())
        got = {}
        try:
            url = "https://aviationweather.gov/api/data/airport?" + urllib.parse.urlencode(
                {"ids": ",".join(sorted(ask)), "format": "json"})
            req = urllib.request.Request(url, headers={"User-Agent": "OpsHub-FlightSchool/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8")) or []
            for a in data:
                try:
                    lat, lon = float(a.get("lat")), float(a.get("lon"))
                except (TypeError, ValueError):
                    continue
                for key in (a.get("icaoId"), a.get("id"), a.get("faaId")):
                    if key:
                        got[str(key).upper()] = (lat, lon, a.get("name") or "")
        except Exception:
            got = None  # offline / service down: try again next time, don't cache misses
        if got is not None:
            now = now_iso()
            for c in todo:
                hit = got.get(c) or (got.get("K" + c) if len(c) == 3 else None)
                if hit:
                    conn.execute("""INSERT OR REPLACE INTO airport_coords (code, lat, lon, name, found, fetched_at)
                                    VALUES (?, ?, ?, ?, 1, ?)""", (c, hit[0], hit[1], hit[2], now))
                else:
                    conn.execute("""INSERT OR REPLACE INTO airport_coords (code, lat, lon, name, found, fetched_at)
                                    VALUES (?, NULL, NULL, NULL, 0, ?)""", (c, now))
            conn.commit()
            have = {r["code"]: r for r in conn.execute(f"SELECT * FROM airport_coords WHERE code IN ({marks})", codes)}
    out = {c: dict(r) for c, r in have.items() if r["found"] and r["lat"] is not None}
    # Our own field has no ICAO code, so aviationweather.gov doesn't list it.
    if _HOME_AIRPORT in codes and _HOME_AIRPORT not in out:
        out[_HOME_AIRPORT] = {"code": _HOME_AIRPORT, "lat": 41.72778, "lon": -74.37722,
                              "name": "Joseph Y. Resnick (Ellenville)", "found": 1}
    return out


def _lookup_local(codes):
    """Airports from the local nav database (instance/navdata.db), no internet needed.
    Returns {code: {"code","lat","lon","name"}} for the codes it knows."""
    import navdb
    if not navdb.available():
        return {}
    conn = navdb.connect()
    try:
        out = {}
        for c in codes:
            a = navdb.lookup_airport(conn, c)
            if a:
                out[c] = {"code": c, "lat": a["lat"], "lon": a["lon"], "name": a["name"], "found": 1}
        return out
    finally:
        conn.close()


def _visit_counts(conn, student_id):
    """{airport code: times it shows up in this student's From / To boxes}."""
    counts = {}
    for e in conn.execute("SELECT route_from, route_to FROM pilot_logbook WHERE student_id = ? AND status != 'denied'", (student_id,)):
        for c in _airport_codes(e["route_from"]) + _airport_codes(e["route_to"]):
            counts[c] = counts.get(c, 0) + 1
    return counts


@pilotlog_bp.route("/api/airports")
def api_airports():
    """Type-ahead for the From / To boxes. ?q=typed&role=from|to&dep=KPOU&student_id=
    Frequented airports first, then closest to home (or to the departure airport
    when filling in the destination)."""
    denied = _require_access()
    if denied:
        return jsonify([])
    import navdb
    if not navdb.available():
        return jsonify([dict(navdb.HOME, nm=0)] if request.args.get("q", "").upper() in ("N", "N8", "N89") else [])
    conn = get_db()
    student, _students = _pick_student(conn)
    freq = _visit_counts(conn, student["id"]) if student else {}
    conn.close()
    nav = navdb.connect()
    try:
        near = (navdb.HOME["lat"], navdb.HOME["lon"])
        if request.args.get("role") == "to":
            dep = navdb.lookup_airport(nav, request.args.get("dep", ""))
            if dep:
                near = (dep["lat"], dep["lon"])
        return jsonify(navdb.search_airports(nav, request.args.get("q", ""), limit=8, near=near, freq=freq))
    finally:
        nav.close()


@pilotlog_bp.route("/api/nav")
def api_nav():
    """Airports, navaids and waypoints inside the map window: ?south=&west=&north=&east=&zoom="""
    denied = _require_access()
    if denied:
        return jsonify({"airports": [], "navaids": [], "fixes": [], "airways": []})
    import navdb
    try:
        south, west, north, east = (float(request.args[k]) for k in ("south", "west", "north", "east"))
        zoom = int(float(request.args.get("zoom", "8")))
    except (KeyError, ValueError):
        return jsonify({"airports": [], "navaids": [], "fixes": [], "airways": []})
    if not navdb.available() or (north - south) > 40 or (east - west) > 80:
        return jsonify({"airports": [], "navaids": [], "fixes": [], "airways": []})
    nav = navdb.connect()
    try:
        return jsonify(navdb.in_view(nav, south, west, north, east, zoom))
    finally:
        nav.close()


@pilotlog_bp.route("/api/route")
def api_route():
    """Points + distance (nm) for the entry map: ?from=&to=&route=
    No route -> straight line. With a route -> follows its waypoints and airways."""
    denied = _require_access()
    if denied:
        return jsonify({"points": [], "nm": 0})
    import navdb
    if not navdb.available():
        return jsonify({"points": [], "nm": 0, "unresolved": [], "error": "Airport database not loaded yet."})
    frm = _airport_codes(request.args.get("from", ""))
    to = _airport_codes(request.args.get("to", ""))
    dep = frm[0] if frm else _HOME_AIRPORT
    dest = to[-1] if to else ""
    via = " ".join(to[:-1])
    nav = navdb.connect()
    try:
        return jsonify(navdb.parse_route(nav, dep, dest, (via + " " + request.args.get("route", "")).strip()))
    finally:
        nav.close()


@pilotlog_bp.route("/map")
def airport_map():
    """Sectional-chart map of every airport the student has landed at, from
    the From / To boxes of their logbook. Staff can switch to the whole
    school with ?all=1."""
    denied = _require_access()
    if denied:
        return denied
    conn = get_db()
    student, students = _pick_student(conn)
    whole_school = _is_staff() and request.args.get("all") == "1"
    if whole_school:
        entries = conn.execute("""SELECT l.*, s.name AS student_name FROM pilot_logbook l
                                  JOIN students s ON s.id = l.student_id WHERE l.status != 'denied' ORDER BY entry_date, l.id""").fetchall()
    elif student:
        entries = conn.execute("""SELECT l.*, NULL AS student_name FROM pilot_logbook l WHERE student_id = ? AND status != 'denied'
                                  ORDER BY entry_date, id""", (student["id"],)).fetchall()
    else:
        entries = []
    visits = {}   # code -> {"count", "first", "last", "students"}
    legs = {}     # (a, b) -> times flown
    for e in entries:
        codes = _airport_codes(e["route_from"]) + _airport_codes(e["route_to"])
        # Every flight from the school starts at home if the From box was left empty.
        if codes and not _airport_codes(e["route_from"]):
            codes.insert(0, _HOME_AIRPORT)
        for c in codes:
            v = visits.setdefault(c, {"count": 0, "first": e["entry_date"], "last": e["entry_date"], "students": set()})
            v["count"] += 1
            v["last"] = e["entry_date"]
            if e["student_name"]:
                v["students"].add(e["student_name"])
        for a, b in zip(codes, codes[1:]):
            if a != b:
                key = tuple(sorted((a, b)))
                legs[key] = legs.get(key, 0) + 1
    try:
        coords = _lookup_airports(conn, list(visits.keys()) + [_HOME_AIRPORT])
    except Exception:
        coords = {}
    conn.close()
    airports, unplaced = [], []
    for c, v in sorted(visits.items(), key=lambda kv: (-kv[1]["count"], kv[0])):
        if c in coords:
            r = coords[c]
            airports.append({"code": c, "name": r["name"] or "", "lat": r["lat"], "lon": r["lon"],
                             "count": v["count"], "first": v["first"], "last": v["last"],
                             "students": sorted(v["students"]), "home": c == _HOME_AIRPORT})
        else:
            unplaced.append(c)
    lines = [{"a": [coords[a]["lat"], coords[a]["lon"]], "b": [coords[b]["lat"], coords[b]["lon"]], "n": n}
             for (a, b), n in legs.items() if a in coords and b in coords]
    home = coords.get(_HOME_AIRPORT)
    center = [home["lat"], home["lon"]] if home else [41.72778, -74.37722]
    return render_template("academy_map.html", student=student, students=students, airports=airports,
                           unplaced=unplaced, lines=lines, center=center, whole_school=whole_school,
                           staff=_is_staff(), tab="map")


# ---------------------------------------------------------------- stats

def _haversine_nm(a, b):
    from math import radians, sin, cos, asin, sqrt
    la1, lo1, la2, lo2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 2 * 3440.065 * asin(sqrt(h))


def _stats_range():
    """(key, start_iso or None, end_iso or None, label) from ?range=all|1|6|12|custom&from=&to=."""
    from datetime import timedelta
    key = request.args.get("range") or "all"
    today = date.today()
    if key in ("1", "6", "12"):
        m = int(key)
        y, mo = today.year, today.month - m
        while mo <= 0:
            mo += 12
            y -= 1
        day = min(today.day, 28)
        return key, date(y, mo, day).isoformat(), today.isoformat(), f"Last {m} month{'s' if m > 1 else ''}"
    if key == "custom":
        def ok(v):
            try:
                return date.fromisoformat((v or "")[:10]).isoformat()
            except ValueError:
                return None
        s, e = ok(request.args.get("from")), ok(request.args.get("to"))
        if s and e and s > e:
            s, e = e, s
        return key, s, e, "Custom range"
    return "all", None, None, "All time"


def compute_stats(conn, entries):
    """Headline numbers for a list of logbook rows (airports, distance, hours, landings...)."""
    from collections import Counter
    s = _totals(entries)
    visits, legs_nm, longest = Counter(), 0.0, None
    seqs = []
    for e in entries:
        codes = _airport_codes(e["route_from"]) + _airport_codes(e["route_to"])
        if codes and not _airport_codes(e["route_from"]):
            codes.insert(0, _HOME_AIRPORT)
        for c in codes:
            visits[c] += 1
        seqs.append(codes)
    try:
        coords = _lookup_airports(conn, list(visits.keys()) + [_HOME_AIRPORT])
    except Exception:
        coords = {}
    def pt(c):
        r = coords.get(c)
        return (r["lat"], r["lon"]) if r and r["lat"] is not None and r["lon"] is not None else None
    for codes in seqs:
        for a, b in zip(codes, codes[1:]):
            if a != b and pt(a) and pt(b):
                d = _haversine_nm(pt(a), pt(b))
                legs_nm += d
                if not longest or d > longest[0]:
                    longest = (d, a, b)
    far = None
    home = pt(_HOME_AIRPORT)
    if home:
        for c in visits:
            if pt(c):
                d = _haversine_nm(home, pt(c))
                if not far or d > far[0]:
                    far = (d, c)
    flown = [e for e in entries if (e["total"] or 0) > 0]
    months = Counter((e["entry_date"] or "")[:7] for e in flown)
    busiest = months.most_common(1)[0] if months else None
    out = dict(
        flights=len(entries), hours=s["total"], dual=s["dual"], solo=s["solo"], pic=s["pic"], xc=s["xc"],
        night=s["night"], inst=round(s["actual_inst"] + s["sim_inst"], 1), approaches=int(s["approaches"]),
        landings=int(s["day_ldg"] + s["night_ldg"]),
        airports=len(visits), airports_placed=sum(1 for c in visits if pt(c)),
        nm=round(legs_nm), mi=round(legs_nm * 1.15078),
        longest=({"nm": round(longest[0]), "a": longest[1], "b": longest[2]} if longest else None),
        farthest=({"nm": round(far[0]), "code": far[1]} if far else None),
        top_airports=visits.most_common(5),
        aircraft=len({(e["tail"] or "").strip().upper() for e in entries if (e["tail"] or "").strip()}),
        types=len({(e["make_model"] or "").strip().upper() for e in entries if (e["make_model"] or "").strip()}),
        days=len({(e["entry_date"] or "")[:10] for e in flown}),
        avg=round(s["total"] / len(flown), 1) if flown else 0,
        busiest=({"month": busiest[0], "n": busiest[1]} if busiest else None),
    )
    return out


@pilotlog_bp.route("/stats")
def stats():
    """Pilot stats: airports visited, distance flown, hours and more, for all time, the last 1 / 6 / 12
    months, or a custom date range."""
    denied = _require_access()
    if denied:
        return denied
    conn = get_db()
    student, students = _pick_student(conn)
    key, start, end, label = _stats_range()
    entries = []
    if student:
        sql, args = "SELECT * FROM pilot_logbook WHERE student_id = ? AND status != 'denied'", [student["id"]]
        if start:
            sql += " AND entry_date >= ?"
            args.append(start)
        if end:
            sql += " AND entry_date <= ?"
            args.append(end + "~")   # entry_date may carry a time part
        entries = conn.execute(sql + " ORDER BY entry_date, id", args).fetchall()
    st = compute_stats(conn, entries) if student else None
    conn.close()
    return render_template("academy_stats.html", student=student, students=students, st=st, range_key=key,
                           range_label=label, start=start or "", end=end or "", staff=_is_staff(),
                           tab="logbook", subtab="stats")
