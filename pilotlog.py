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
import io
from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, abort, Response

from db import get_db, now_iso

pilotlog_bp = Blueprint("pilotlog", __name__, url_prefix="/academy")

# Columns a student fills in / can correct when completing an entry.
STUDENT_FIELDS = [
    # (field, label, kind)
    ("route_from", "From", "text"), ("route_to", "To / via", "text"),
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


# ---------------------------------------------------------------- helpers

def _access_ok():
    return bool(session.get("is_master_admin") or session.get("academy_access"))


def _is_staff():
    return bool(session.get("is_master_admin") or session.get("cfi_id"))


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
    elif existing["status"] == "pending":
        conn.execute("""UPDATE pilot_logbook SET student_id=?, cfi_id=?, asset_id=?, entry_date=?, tail=?, make_model=?,
                        total=?, dual=?, solo=?, pic=?, day_ldg=?, night_ldg=?, night_fs=?, instructor_name=?,
                        instructor_cert=?, instructor_cert_exp=?, instructor_signature=?, updated_at=? WHERE id=?""",
                     (f["student_id"], f["cfi_id"], f["asset_id"], f["flight_date"], f["tag"], make_model,
                      total, dual, solo, pic, day_ldg, night_ldg, f["night_landings_fs"] or 0, *instructor,
                      now_iso(), existing["id"]))


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
    entries = conn.execute("""SELECT * FROM pilot_logbook WHERE student_id = ? ORDER BY entry_date, id""",
                           (student["id"],)).fetchall()
    vals = [(e, _entry_values(e)) for e in entries]
    miles = {(r["track"], r["item"]): r for r in conn.execute(
        "SELECT * FROM academy_milestones WHERE student_id = ?", (student["id"],)).fetchall()}
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


def _totals(entries):
    keys = ["total", "dual", "solo", "pic", "xc", "night", "actual_inst", "sim_inst", "approaches",
            "day_ldg", "night_ldg"]
    return {k: round(sum((e[k] or 0) for e in entries), 1) for k in keys}


def _pick_student(conn):
    """The student whose logbook/progress is shown: yourself, or (staff)
    whoever ?student_id picks. Returns (student, students_for_picker)."""
    if _is_staff():
        students = conn.execute("""SELECT * FROM students WHERE active = 1 AND COALESCE(is_station, 0) = 0
                                   ORDER BY name""").fetchall()
        sid = request.values.get("student_id", type=int) or session.get("student_id") or \
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
    if not _access_ok():
        flash("You don't have access to Flight Academy yet. Ask an admin.", "danger")
        return redirect(url_for("home_launcher"))
    conn = get_db()
    student, students = _pick_student(conn)
    entries = conn.execute("""SELECT * FROM pilot_logbook WHERE student_id = ?
                              ORDER BY entry_date DESC, id DESC""", (student["id"],)).fetchall() if student else []
    dual_given = None
    if session.get("cfi_id"):
        r = conn.execute("SELECT SUM(dual) d, COUNT(*) c FROM pilot_logbook WHERE cfi_id = ? AND dual > 0",
                         (session["cfi_id"],)).fetchone()
        dual_given = {"hours": round(r["d"] or 0, 1), "flights": r["c"]}
    conn.close()
    pending = [e for e in entries if e["status"] == "pending"]
    return render_template("academy_logbook.html", student=student, students=students, entries=entries,
                           pending=pending, totals=_totals(entries), staff=_is_staff(), dual_given=dual_given,
                           tab="logbook")


@pilotlog_bp.route("/logbook/<int:entry_id>", methods=["GET", "POST"])
def entry(entry_id):
    if not _access_ok():
        return redirect(url_for("home_launcher"))
    conn = get_db()
    e = conn.execute("""SELECT l.*, s.name as student_name FROM pilot_logbook l JOIN students s ON s.id = l.student_id
                        WHERE l.id = ?""", (entry_id,)).fetchone()
    if not e or not _can_touch(e):
        conn.close()
        abort(404)
    if request.method == "POST":
        if e["status"] == "complete" and not _is_staff() and request.form.get("action") != "reopen":
            conn.close()
            flash("This entry is already approved. Reopen it to make changes.", "warning")
            return redirect(url_for("pilotlog.entry", entry_id=entry_id))
        if request.form.get("action") == "reopen":
            conn.execute("UPDATE pilot_logbook SET status='pending', approved_at=NULL, approved_by=NULL, updated_at=? WHERE id=?",
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
        # Times that come from the flight itself - only staff can correct them.
        if _is_staff():
            for field in ("total", "dual", "solo"):
                sets.append(f"{field}=?")
                args.append(_num(request.form.get(field)) or 0)
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
    return render_template("academy_logbook_entry.html", e=e, fields=STUDENT_FIELDS, staff=_is_staff(), tab="logbook")


@pilotlog_bp.route("/logbook/export.csv")
def export_csv():
    if not _access_ok():
        return redirect(url_for("home_launcher"))
    conn = get_db()
    student, _students = _pick_student(conn)
    if not student:
        conn.close()
        abort(404)
    entries = conn.execute("SELECT * FROM pilot_logbook WHERE student_id = ? ORDER BY entry_date, id",
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
    if not _access_ok():
        flash("You don't have access to Flight Academy yet. Ask an admin.", "danger")
        return redirect(url_for("home_launcher"))
    conn = get_db()
    student, students = _pick_student(conn)
    tracks = build_progress(conn, student) if student else []
    pending_count = conn.execute("SELECT COUNT(*) c FROM pilot_logbook WHERE student_id = ? AND status = 'pending'",
                                 (student["id"],)).fetchone()["c"] if student else 0
    conn.close()
    open_track = request.args.get("track") or NEXT_TRACK.get(student["pilot_certificate"] if student else None, "private")
    return render_template("academy_progress.html", student=student, students=students, tracks=tracks,
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
