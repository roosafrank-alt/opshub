"""ENDORSE: student endorsements, built on FAA AC 61-65K Appendix A (Sample Endorsements).

- The catalog (endorsement_data/ac61-65k.json) is the AC's own wording, parsed from the PDF,
  96 entries A.1-A.96 with their [blanks]. Nothing in the wording is written by us.
- META below adds what the AC text alone doesn't give a program: how long an endorsement is
  good for, which ones replace each other (a renewed solo replaces the old one), who issues it
  (a few are given by an examiner, not a CFI) and which ones each rating needs.
- Students see an Endorsements tab (Logbook > Endorsements): current ones, a small "View N
  expired" link, and a running checklist for the rating they're working toward.
- CFIs (and Flight Admins) sign new ones with one form: pick the endorsement and the student,
  the student's name, the plane, the date and the CFI's signature line fill in by themselves.
- A solo endorsement (A.6 / A.7) also sets the student's solo sign-off date and 90-day expiry,
  which is what flight.py checks when a solo is booked (_schedule_review_flag).
- check_expiries() runs from the app's once-a-minute loop (at most hourly) and tells the admins
  and the signing CFI once when an endorsement is 7 days from expiring and once when it expires.
"""
import calendar
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, abort

from db import get_db, now_iso

log = logging.getLogger(__name__)
endorse_bp = Blueprint("endorse", __name__, url_prefix="/academy/endorsements")

CATALOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "endorsement_data", "ac61-65k.json")

GROUP_SHORT = {
    "PREREQUISITES FOR THE PRACTICAL TEST ENDORSEMENT": "Practical test prerequisites",
    "STUDENT PILOT ENDORSEMENTS": "Student pilot",
    "ADDITIONAL STUDENT PILOT ENDORSEMENTS FOR STUDENTS SEEKING SPORT OR RECREATIONAL PILOT CERTIFICATES": "Student pilot (sport / recreational)",
    "SPORT PILOT ENDORSEMENTS": "Sport pilot",
    "RECREATIONAL PILOT ENDORSEMENTS": "Recreational pilot",
    "PRIVATE PILOT ENDORSEMENTS": "Private pilot",
    "COMMERCIAL PILOT ENDORSEMENTS": "Commercial pilot",
    "AIRLINE TRANSPORT PILOT (ATP) ENDORSEMENTS": "Airline transport pilot",
    "INSTRUMENT RATING ENDORSEMENTS": "Instrument rating",
    "FLIGHT INSTRUCTOR (OTHER THAN FLIGHT INSTRUCTORS WITH A SPORT PILOT RATING) ENDORSEMENTS": "Flight instructor",
    "FLIGHT INSTRUCTOR WITH A SPORT PILOT RATING ENDORSEMENT": "Flight instructor (sport)",
    "GROUND INSTRUCTOR ENDORSEMENT": "Ground instructor",
    "SPECIAL FEDERAL AVIATION REGULATION (SFAR) 73, ROBINSON HELICOPTER COMPANY, ROBINSON R-22/R-44 SPECIAL TRAINING AND EXPERIENCE REQUIREMENTS, ENDORSEMENTS": "Robinson R-22 / R-44 (SFAR 73)",
    "ADDITIONAL ENDORSEMENTS": "Additional (flight review, IPC, complex, tailwheel...)",
    "NIGHT VISION GOGGLES OPERATIONS (NVGO)": "Night vision goggles",
    "ENHANCED FLIGHT VISION SYSTEM (EFVS)": "Enhanced flight vision system",
    "SIMPLIFIED FLIGHT CONTROLS": "Simplified flight controls",
}
# Student-pilot endorsements stop mattering once the student holds a pilot certificate.
STUDENT_PILOT_GROUPS = {"STUDENT PILOT ENDORSEMENTS",
                        "ADDITIONAL STUDENT PILOT ENDORSEMENTS FOR STUDENTS SEEKING SPORT OR RECREATIONAL PILOT CERTIFICATES"}
HOLDS_CERTIFICATE = {"sport", "recreational", "private", "commercial", "atp"}

# How long an endorsement is good for. ("days", n): n days after the date; ("cal_months", n): to the
# end of the nth calendar month after the month of the date; ("flight_date",): good for the one flight
# named in its [date] blank. "src" says where the limit comes from. A CFI can always change the date.
EXPIRY = {
    "A.1": (("cal_months", 2), "AC 61-65K A.1 / 14 CFR 61.39(a)(6)(i): training within 2 calendar months preceding the month of application"),
    "A.5": (("days", 90), "AC 61-65K A.5: expires 90 calendar days from the night training"),
    "A.6": (("days", 90), "14 CFR 61.87(n): endorsed within the 90 calendar days before the solo"),
    "A.7": (("days", 90), "14 CFR 61.87(p): each additional 90-calendar-day period"),
    "A.10": (("flight_date",), "14 CFR 61.93(c)(3): for the one cross-country flight named"),
    "A.12": (("days", 90), "14 CFR 61.95(a): endorsement dated within the 90 days before the flight"),
    "A.13": (("days", 90), "14 CFR 61.95(b): endorsement dated within the 90 days before the flight"),
    "A.15": (("days", 90), "14 CFR 61.94: endorsement dated within the 90 days before the flight"),
    "A.16": (("days", 90), "14 CFR 61.94: endorsement dated within the 90 days before the flight"),
    "A.34": (("flight_date",), "14 CFR 61.101(j): for the solo flight on the date named"),
    "A.44": (("cal_months", 2), "AC 61-65K A.44 / 14 CFR 61.39(a): training within 2 calendar months preceding the test"),
    "A.62": (("cal_months", 12), "AC 61-65K A.62: annual flight review due 12 calendar months after"),
    "A.66": (("cal_months", 12), "AC 61-65K A.66: annual flight review due 12 calendar months after"),
    "A.64": (("cal_months", 24), "14 CFR 61.56 / SFAR 73 section 2(c): flight review within the preceding 24 calendar months"),
    "A.68": (("cal_months", 24), "14 CFR 61.56 / SFAR 73 section 2(c): flight review within the preceding 24 calendar months"),
    "A.69": (("cal_months", 24), "14 CFR 61.56(c): flight review within the preceding 24 calendar months"),
    "A.70": (("cal_months", 24), "14 CFR 61.56(e): a WINGS phase counts as the flight review for 24 calendar months"),
    "A.71": (("cal_months", 6), "14 CFR 61.57(c)-(d): instrument current for 6 calendar months after the IPC (approaches flown since can extend it; the IPC itself doesn't expire)"),
}
# Endorsements that replace each other: the newest one in a family is the one that counts.
FAMILY = {"A.6": "solo", "A.7": "solo", "A.69": "flight_review", "A.70": "flight_review"}
# Issued by someone other than the student's CFI - recorded here, not signed by the CFI.
ISSUED_BY = {"A.21": "an examiner", "A.22": "an examiner", "A.40": "the institution", "A.41": "the ATP CTP provider",
             "A.56": "an authorized evaluator", "A.63": "a DPE or FAA inspector", "A.67": "a DPE or FAA inspector",
             "A.84": "the towing pilot", "A.90": "an FAA inspector or other FAA-authorized person"}
# Shown as currency, not as an endorsement that expires: no "expired" warning and no alert.
CURRENCY_ONLY = {"A.71"}
# Blanks that tell two endorsements of the same kind apart (a solo endorsement for a different
# plane, Class B for a different airport): a newer one only replaces an older one with the same value.
DISTINCT_BLANKS = ("M/M", "make and model (M/M)", "name of Class B", "name of airport", "name of Class B, C, or D",
                   "name of airport located in Class B, C, or D airspace or an airport having an operational control tower",
                   "airport name", "airport names")
# Auto-fill the test name / category only where it can't be wrong.
NO_TEST_NAME = {"A.18", "A.19", "A.52", "A.53", "A.54", "A.78", "A.79", "A.80", "A.81", "A.82", "A.86"}
NO_CATEGORY = {"A.18", "A.19", "A.53", "A.54", "A.76", "A.78", "A.79", "A.80", "A.81", "A.82"}
# Shorter names for long AC titles, for lists (the full AC title shows on the endorsement itself).
SHORT_TITLE = {"A.14": "U.S. citizenship (TSA)", "A.8": "Solo takeoffs and landings at another airport (25 NM)",
               "A.11": "Repeated solo cross-country (50 NM)", "A.13": "Solo at an airport in Class B",
               "A.16": "Solo at a Class B, C, or D / towered airport", "A.10": "Solo cross-country flight (each flight)",
               "A.7": "Solo flight (renewed 90 days)", "A.6": "Solo flight (first 90 days)",
               "A.70": "WINGS phase completed", "A.74": "PIC in a pressurized high-altitude aircraft",
               "A.76": "Solo without the category/class rating"}
# Quick buttons on the sign form.
QUICK_BUTTONS = ["A.3", "A.4", "A.6", "A.7", "A.9", "A.10", "A.36", "A.37", "A.1", "A.69"]
# Quick groups shown first in the picker.
QUICK = [
    ("Solo", ["A.14", "A.3", "A.4", "A.6", "A.7", "A.5"]),
    ("Cross-country", ["A.9", "A.10", "A.8", "A.11"]),
    ("Airspace", ["A.12", "A.13", "A.15", "A.16"]),
    ("Private pilot checkride", ["A.36", "A.37", "A.1", "A.2"]),
    ("Instrument checkride", ["A.42", "A.43", "A.44"]),
    ("Commercial checkride", ["A.38", "A.39", "A.1", "A.2"]),
    ("Flight review & IPC", ["A.69", "A.70", "A.71"]),
    ("Aircraft", ["A.72", "A.73", "A.75", "A.74"]),
    ("Retest", ["A.77"]),
]
# What each rating needs before the checkride. "ever": received at some point (even if since
# expired); "current": must still be good on the day. "if" marks ones only needed sometimes.
_SOLO = [
    {"ids": ["A.14"], "need": "ever", "if": "for U.S. citizens (anyone else goes through the TSA flight training security program)"},
    {"ids": ["A.3"], "need": "ever"},
    {"ids": ["A.4"], "need": "ever"},
]
REQUIREMENTS = {
    "solo": _SOLO + [{"ids": ["A.6", "A.7"], "need": "current", "label": "Solo flight (current 90-day endorsement)"},
                     {"ids": ["A.5"], "need": "current", "if": "only for solo flight at night"}],
    "sport": _SOLO + [{"ids": ["A.6", "A.7"], "need": "ever", "label": "Solo flight"},
                      {"ids": ["A.9"], "need": "ever"}, {"ids": ["A.10"], "need": "ever", "label": "Solo cross-country flight (at least one)"},
                      {"ids": ["A.17", "A.86"], "need": "ever"}, {"ids": ["A.20"], "need": "ever"},
                      {"ids": ["A.1"], "need": "current"},
                      {"ids": ["A.2"], "need": "current", "if": "only if the knowledge test report lists missed subject areas"}],
    "recreational": _SOLO + [{"ids": ["A.6", "A.7"], "need": "ever", "label": "Solo flight"},
                             {"ids": ["A.29", "A.86"], "need": "ever"}, {"ids": ["A.30"], "need": "ever"},
                             {"ids": ["A.1"], "need": "current"},
                             {"ids": ["A.2"], "need": "current", "if": "only if the knowledge test report lists missed subject areas"}],
    "private": _SOLO + [{"ids": ["A.6", "A.7"], "need": "ever", "label": "Solo flight"},
                        {"ids": ["A.9"], "need": "ever"}, {"ids": ["A.10"], "need": "ever", "label": "Solo cross-country flight (at least one)"},
                        {"ids": ["A.36", "A.86"], "need": "ever"}, {"ids": ["A.37"], "need": "ever"},
                        {"ids": ["A.1"], "need": "current"},
                        {"ids": ["A.2"], "need": "current", "if": "only if the knowledge test report lists missed subject areas"}],
    "instrument": [{"ids": ["A.42", "A.86"], "need": "ever"}, {"ids": ["A.43"], "need": "ever"}, {"ids": ["A.44"], "need": "current"}],
    "commercial": [{"ids": ["A.38", "A.86"], "need": "ever"}, {"ids": ["A.39"], "need": "ever"}, {"ids": ["A.1"], "need": "current"},
                   {"ids": ["A.2"], "need": "current", "if": "only if the knowledge test report lists missed subject areas"}],
    "cfi": [{"ids": ["A.45"], "need": "ever", "if": "if not already exempt from the FOI test (61.183(e))"},
            {"ids": ["A.46"], "need": "ever"}, {"ids": ["A.47"], "need": "ever"},
            {"ids": ["A.49"], "need": "ever", "if": "flight instructor airplane or glider applicants"},
            {"ids": ["A.1"], "need": "current"},
            {"ids": ["A.2"], "need": "current", "if": "only if the knowledge test report lists missed subject areas"}],
    "multi": [{"ids": ["A.78"], "need": "ever"}, {"ids": ["A.1"], "need": "current"}],
    "atp": [{"ids": ["A.41"], "need": "ever", "label": "ATP CTP completed (airplane multiengine)"}],
}
TRACK_NAMES = {"solo": "First Solo", "sport": "Sport Pilot", "recreational": "Recreational Pilot", "private": "Private Pilot",
               "instrument": "Instrument Rating", "commercial": "Commercial Pilot", "cfi": "Flight Instructor",
               "multi": "Multi-Engine Rating", "atp": "Airline Transport Pilot"}
NEXT_TRACK = {None: "private", "": "private", "student": "private", "sport": "private", "recreational": "private",
              "private": "instrument", "commercial": "cfi", "atp": "atp"}
CERT_LABELS = {"student": "Student Pilot", "sport": "Sport Pilot", "recreational": "Recreational Pilot",
               "private": "Private Pilot", "commercial": "Commercial Pilot", "atp": "Airline Transport Pilot"}
# Name of the test or certificate, by the group an endorsement sits in (fills [name of], [applicable]...).
GROUP_TEST = {"SPORT PILOT ENDORSEMENTS": "Sport Pilot", "RECREATIONAL PILOT ENDORSEMENTS": "Recreational Pilot Airplane",
              "PRIVATE PILOT ENDORSEMENTS": "Private Pilot Airplane", "COMMERCIAL PILOT ENDORSEMENTS": "Commercial Pilot Airplane",
              "INSTRUMENT RATING ENDORSEMENTS": "Instrument Rating Airplane",
              "FLIGHT INSTRUCTOR (OTHER THAN FLIGHT INSTRUCTORS WITH A SPORT PILOT RATING) ENDORSEMENTS": "Flight Instructor Airplane"}
TRACK_TEST = {"private": "Private Pilot", "sport": "Sport Pilot", "recreational": "Recreational Pilot",
              "commercial": "Commercial Pilot", "instrument": "Instrument Rating", "cfi": "Flight Instructor", "multi": "Multiengine class rating"}
# Written with the older "Write endorsement" pop-up (kind -> catalog id).
LEGACY_KIND = {"pre_knowledge": "A.3", "pre_training": "A.4", "solo": "A.6", "solo_renew": "A.7",
               "knowledge_test": "A.36", "practical_test": "A.37"}
CONDITIONS_BLANK = "List any applicable conditions or limitations."

_cache = {}


def catalog():
    """[entry...] in AC order, each with group_short, student_pilot_only, blanks, expiry, issued_by."""
    mtime = os.path.getmtime(CATALOG_FILE)
    if _cache.get("mtime") != mtime:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            raw = json.load(f)["endorsements"]
        out = []
        for e in raw:
            e = dict(e)
            e["group_short"] = GROUP_SHORT.get(e["group"], (e["group"] or "Other").title())
            e["student_pilot_only"] = e["group"] in STUDENT_PILOT_GROUPS
            e["blanks"] = blanks(e["text"])
            rule = EXPIRY.get(e["id"])
            e["expiry"] = list(rule[0]) if rule else None
            e["expiry_src"] = rule[1] if rule else None
            e["issued_by"] = ISSUED_BY.get(e["id"])
            e["family"] = FAMILY.get(e["id"], e["id"])
            e["short"] = SHORT_TITLE.get(e["id"], e["title"])
            out.append(e)
        _cache.update(mtime=mtime, list=out, by_id={e["id"]: e for e in out})
    return _cache["list"]


def get(entry_id):
    catalog()
    return _cache["by_id"].get(entry_id)


def blanks(text):
    seen, out = set(), []
    for b in re.findall(r"\[([^\]]+)\]", text or ""):
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def fill(text, values):
    """The wording with each [blank] replaced; an empty conditions blank is dropped, any other
    empty blank stays as [blank] so it is obvious on paper."""
    def rep(m):
        key = m.group(1)
        v = (values.get(key) or "").strip()
        if not v:
            return "" if key == CONDITIONS_BLANK else m.group(0)
        return v
    out = re.sub(r"\[([^\]]+)\]", rep, text or "")
    return re.sub(r"[ \t]+([.,])", r"\1", re.sub(r"[ \t]{2,}", " ", out)).strip()


def us_date(iso):
    try:
        d = datetime.strptime(str(iso)[:10], "%Y-%m-%d")
        return d.strftime("%m-%d-%Y")
    except (TypeError, ValueError):
        return iso or ""


def _parse_any_date(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%m-%d-%Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _end_of_month_plus(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, calendar.monthrange(y, m)[1])


def compute_expiry(entry_id, signed_on, values=None):
    """YYYY-MM-DD the endorsement stops being good, or None if it doesn't expire."""
    e = get(entry_id)
    rule = e["expiry"] if e else None
    if not rule:
        return None
    d = _parse_any_date(signed_on) or date.today()
    if rule[0] == "days":
        return (d + timedelta(days=rule[1])).isoformat()
    if rule[0] == "cal_months":
        return _end_of_month_plus(d, rule[1]).isoformat()
    if rule[0] == "flight_date":
        fd = _parse_any_date((values or {}).get("date")) or d
        return fd.isoformat()
    return None


def sig_line(cfi_name, cert, cert_kind, cert_date, signed_on, issued_by=None):
    if issued_by:
        return f"/s/ {us_date(signed_on)} (issued by {issued_by})"
    tail = ""
    if cert_date:
        tail = (" Exp. " if cert_kind == "exp" else " RE ") + us_date(cert_date)
    return f"/s/ {us_date(signed_on)} {cfi_name or ''} {(cert or '').strip()}{'CFI' if cert else ''}{tail}".replace("  ", " ").strip()


def defaults(entry, student, plane, track, today=None):
    """Blank -> value the form starts with: everything the program already knows."""
    today = today or date.today().isoformat()
    cert = (student["pilot_certificate"] if student is not None and "pilot_certificate" in student.keys() else None) or "student"
    test = GROUP_TEST.get(entry["group"]) or (TRACK_TEST.get(track, "Private Pilot") if entry["id"] in ("A.1", "A.2", "A.77") else "")
    if entry["id"] in NO_TEST_NAME:
        test = ""
    out = {}
    for b in entry["blanks"]:
        k = b.lower().strip()
        v = ""
        if k.startswith("first name, mi, last name"):
            v = student["name"] if student is not None else ""
            if "pilot certificate no" in k:
                v += ", Pilot Certificate No. ________"
        elif k in ("m/m", "make and model (m/m)", "aircraft model"):
            v = plane or ""
        elif k in ("date", "date of flight review"):
            v = us_date(today)
        elif k in ("grade of pilot certificate", "pilot certificate"):
            v = CERT_LABELS.get(cert, "")
        elif k in ("applicable",):
            v = TRACK_TEST.get(track, "Private Pilot") if entry["id"] in ("A.1", "A.2") else test
        elif k in ("name of", "type of", "name of the knowledge test"):
            v = test
        elif entry["id"] in NO_CATEGORY and ("categ" in k or "class" in k):
            v = ""
        elif k == "aircraft category":
            v = "airplane"
        elif k in ("category and class", "aircraft category and class", "aircraft category/class", "specific category and class",
                   "category and class of"):
            v = "airplane single-engine land"
        elif k == "category and class, m/m":
            v = "airplane single-engine land, " + (plane or "")
        elif k == "date 12 calendar months after date of this endorsement":
            v = us_date(_end_of_month_plus(_parse_any_date(today) or date.today(), 12).isoformat())
        out[b] = v
    return out


# ---------------------------------------------------------------- reading a student's endorsements

def _rows(conn, where, args):
    return conn.execute(f"""SELECT e.*, s.name AS student_name FROM student_endorsements e
                            LEFT JOIN students s ON s.id = e.student_id WHERE {where}
                            ORDER BY e.signed_on DESC, e.id DESC""", args).fetchall()


def _item(row, today):
    cid = row["catalog_id"] if "catalog_id" in row.keys() and row["catalog_id"] else LEGACY_KIND.get(row["kind"])
    entry = get(cid) if cid else None
    expires = row["expires_on"] if "expires_on" in row.keys() and row["expires_on"] else None
    if not expires and cid and cid in EXPIRY and not row["catalog_id"]:
        expires = compute_expiry(cid, row["signed_on"])          # older pop-up rows: work it out
    days_left = None
    if expires:
        try:
            days_left = (date.fromisoformat(expires[:10]) - today).days
        except ValueError:
            pass
    try:
        vals = json.loads(row["fields_json"]) if "fields_json" in row.keys() and row["fields_json"] else {}
    except ValueError:
        vals = {}
    distinct = next(((vals.get(b) or "").strip().lower() for b in DISTINCT_BLANKS if (vals.get(b) or "").strip()),
                    (row["plane"] or "").strip().lower())
    currency = cid in CURRENCY_ONLY
    return {"row": row, "id": row["id"], "cid": cid, "entry": entry, "currency": currency,
            "title": (entry["short"] if entry else row["label"]), "full_title": (entry["title"] if entry else row["label"]),
            "num": cid or "",
            "family": FAMILY.get(cid, cid or ("legacy:%s" % row["kind"])) + "|" + distinct,
            "signed_on": row["signed_on"], "expires": expires, "days_left": days_left,
            "expired": days_left is not None and days_left < 0 and not currency,
            "lapsed": currency and days_left is not None and days_left < 0,
            "flight_specific": bool(entry and entry["expiry"] and entry["expiry"][0] == "flight_date"),
            "student_pilot_only": bool(entry and entry["student_pilot_only"]),
            "cfi_name": row["cfi_name"], "student_name": row["student_name"] if "student_name" in row.keys() else None,
            "voided": bool("voided_at" in row.keys() and row["voided_at"])}


def split_items(items, holds_cert=False):
    """(current, expired, no_longer_needed). An older one replaced by a newer one of the same
    family counts as expired history; a past one-flight cross-country is history, not a warning."""
    current, expired, old = [], [], []
    newest = {}
    for it in sorted(items, key=lambda x: (x["signed_on"] or "", x["id"]), reverse=True):
        if it["voided"]:
            continue
        if holds_cert and it["student_pilot_only"]:
            old.append(it)
            continue
        fam = it["family"]
        superseded = (fam in newest and it["entry"] is not None and it["entry"]["expiry"] is not None
                      and not it["flight_specific"])
        if not superseded:
            newest.setdefault(fam, it)
        it["superseded"] = superseded
        if it["expired"] or it["lapsed"] or superseded:
            expired.append(it)
        else:
            current.append(it)
    return current, expired, old


def student_items(conn, student_id):
    today = date.today()
    return [_item(r, today) for r in _rows(conn, "e.student_id = ?", (student_id,))]


def checklist(items, track, holds_cert=False):
    """Rows for the rating's checklist: each requirement with done / expired / missing. A pilot who
    already holds a certificate (say a sport or recreational pilot going for private) doesn't need
    the student-pilot ones again."""
    reqs = REQUIREMENTS.get(track) or []
    if holds_cert:
        reqs = [r for r in reqs if not all((get(i) or {}).get("student_pilot_only") for i in r["ids"])]
    live = [i for i in items if not i["voided"]]
    rows = []
    for rq in reqs:
        have = [i for i in live if i["cid"] in rq["ids"]]
        have.sort(key=lambda x: x["signed_on"] or "", reverse=True)
        best = have[0] if have else None
        e = get(rq["ids"][0])
        label = rq.get("label") or (e["short"] if e else rq["ids"][0])
        if not best:
            state = "missing"
        elif rq["need"] == "current" and best["expired"]:
            state = "expired"
        else:
            state = "done"
        rows.append({"ids": rq["ids"], "label": label, "need": rq["need"], "if": rq.get("if"), "note": rq.get("note"),
                     "state": state, "item": best})
    required = [r for r in rows if not r["if"]]
    return {"track": track, "name": TRACK_NAMES.get(track, track), "rows": rows,
            "done": sum(1 for r in required if r["state"] == "done"), "total": len(required),
            "missing": [r for r in required if r["state"] != "done"]}


def sync_solo_signoff(conn, student_id):
    """The newest solo endorsement (A.6 / A.7, or the older pop-up's solo kinds) drives the
    student's solo sign-off date and expiry, which booking checks (flight._schedule_review_flag)."""
    rows = conn.execute("""SELECT * FROM student_endorsements WHERE student_id = ? AND voided_at IS NULL
                           AND (catalog_id IN ('A.6', 'A.7') OR (catalog_id IS NULL AND kind IN ('solo', 'solo_renew')))
                           ORDER BY signed_on DESC, id DESC LIMIT 1""", (student_id,)).fetchone()
    if rows:
        exp = rows["expires_on"] or compute_expiry("A.6", rows["signed_on"])
        conn.execute("UPDATE students SET solo_signoff_date = ?, solo_signoff_expires = ? WHERE id = ?",
                     (rows["signed_on"], exp, student_id))


# ---------------------------------------------------------------- access

def _is_flight_admin():
    from auth import is_flight_admin
    return is_flight_admin()


def can_sign():
    """CFIs and Flight Admins (not while looking at a student's view)."""
    return bool(not session.get("_view_as_readonly") and not session.get("student_id")
                and (session.get("cfi_id") or _is_flight_admin()))


def _access():
    from pilotlog import _require_access
    return _require_access("endorsements")


def _real_cfis(conn):
    return conn.execute("""SELECT * FROM cfis WHERE active = 1 AND COALESCE(is_station, 0) = 0 ORDER BY name""").fetchall()


def _signer(conn, cfi_id=None):
    """The CFI signing: always the signed-in person's own real CFI profile.
    Nobody signs another CFI's block, whatever their login level (cfi_id from
    the page is ignored on purpose). None = this login holds no CFI profile."""
    if session.get("_view_as_readonly"):
        return None
    from auth import own_cfi
    return own_cfi(conn, real_only=True)


def _planes(conn):
    rows = conn.execute("""SELECT tag, make, model FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1
                           AND COALESCE(is_simulator, 0) = 0 ORDER BY schedule_order, tag""").fetchall()
    out = []
    for p in rows:
        mm = " ".join(x for x in [p["make"], p["model"]] if x).strip()
        if mm and mm not in out:
            out.append(mm)
    return out


def _student_plane(conn, student_id, planes):
    """The make/model of the plane this student flew last, else the first plane."""
    row = conn.execute("""SELECT a.make, a.model FROM flights f JOIN assets a ON a.id = f.asset_id
                          WHERE f.student_id = ? ORDER BY f.flight_date DESC, f.id DESC LIMIT 1""", (student_id,)).fetchone() \
        if student_id else None
    if row:
        mm = " ".join(x for x in [row["make"], row["model"]] if x).strip()
        if mm:
            return mm
    return planes[0] if planes else ""


def _track_for(student, requested=None):
    if requested in REQUIREMENTS:
        return requested
    from pilotlog import goal_track_code
    code = goal_track_code(student)
    if code in REQUIREMENTS:
        return code
    cert = student["pilot_certificate"] if student is not None and "pilot_certificate" in student.keys() else None
    return NEXT_TRACK.get(cert, "private")


# ---------------------------------------------------------------- pages

@endorse_bp.route("/")
def student_tab():
    denied = _access()
    if denied:
        return denied
    from pilotlog import _pick_student, _is_staff
    conn = get_db()
    student, students = _pick_student(conn)
    items = student_items(conn, student["id"]) if student else []
    conn.close()
    holds = bool(student and (student["pilot_certificate"] or "") in HOLDS_CERTIFICATE)
    current, expired, old = split_items(items, holds)
    track = _track_for(student, request.args.get("track"))
    chk = checklist(items, track, holds) if student else None
    return render_template("academy_endorsements.html", student=student, students=students, staff=_is_staff(),
                           current=current, expired=expired, old=old, chk=chk, tracks=TRACK_NAMES, track=track,
                           can_sign=can_sign(), tab="logbook", subtab="endorsements", us_date=us_date)


@endorse_bp.route("/new", methods=["GET", "POST"])
def new():
    denied = _access()
    if denied:
        return denied
    if not can_sign():
        flash("Only a CFI or a Flight Admin can sign endorsements.", "warning")
        return redirect(url_for("endorse.student_tab"))
    conn = get_db()
    if request.method == "POST":
        return _save(conn)
    students = conn.execute("""SELECT * FROM students WHERE active = 1 AND COALESCE(is_station, 0) = 0
                               ORDER BY lower(name)""").fetchall()
    sid = request.args.get("student_id", type=int)
    student = next((s for s in students if s["id"] == sid), None)
    planes = _planes(conn)
    signer = _signer(conn, request.args.get("cfi_id", type=int))
    cfis = []   # no "Signing CFI" picker: you only ever sign as yourself
    conn.close()
    cat = [{k: e[k] for k in ("id", "num", "title", "short", "reg", "desc", "group", "group_short", "text", "notes", "blanks",
                               "expiry", "expiry_src", "issued_by", "student_pilot_only")} for e in catalog()]
    pick = request.args.get("e")
    track = request.args.get("track") if request.args.get("track") in REQUIREMENTS else ""
    return render_template("academy_endorsement_new.html", all_students=students, student=student, planes=planes,
                           signer=signer, cfis=cfis, catalog_json=cat, quick=QUICK, groups=_groups(), quick_buttons=QUICK_BUTTONS,
                           today=date.today().isoformat(), track=track, pick=pick if get(pick or "") else "",
                           group_test=GROUP_TEST, track_test=TRACK_TEST, cert_labels=CERT_LABELS,
                           tab="logbook", subtab="endorsements")


@endorse_bp.route("/defaults")
def defaults_json():
    """What the sign form fills in for one endorsement + student (and the expiry it works out)."""
    if not can_sign():
        abort(403)
    entry = get(request.args.get("e") or "")
    if not entry:
        return {"ok": False}, 404
    conn = get_db()
    student = conn.execute("SELECT * FROM students WHERE id = ?", (request.args.get("student_id", type=int),)).fetchone()
    plane = _student_plane(conn, student["id"], _planes(conn)) if student else ""
    conn.close()
    signed_on = (_parse_any_date(request.args.get("signed_on")) or date.today()).isoformat()
    vals = defaults(entry, student, plane, _track_for(student, request.args.get("track")), today=signed_on)
    return {"ok": True, "values": [vals.get(b, "") for b in entry["blanks"]],
            "expires": compute_expiry(entry["id"], signed_on, {"date": vals.get("date")}),
            "holds_cert": bool(student and (student["pilot_certificate"] or "") in HOLDS_CERTIFICATE)}


def _groups():
    out = []
    for e in catalog():
        if not out or out[-1][0] != e["group_short"]:
            out.append((e["group_short"], []))
        out[-1][1].append(e)
    return out


def _save(conn):
    f = request.form
    entry = get(f.get("catalog_id") or "")
    student = conn.execute("SELECT * FROM students WHERE id = ? AND COALESCE(is_station, 0) = 0",
                           (f.get("student_id", type=int),)).fetchone()
    if not entry or not student:
        conn.close()
        flash("Pick an endorsement and a student.", "danger")
        return redirect(url_for("endorse.new", student_id=f.get("student_id") or None))
    signed_on = (_parse_any_date(f.get("signed_on")) or date.today()).isoformat()
    values = {}
    for i, b in enumerate(entry["blanks"]):
        values[b] = (f.get("b%d" % i) or "").strip()[:300]
    body = fill(entry["text"], values)
    expires = (_parse_any_date(f.get("expires_on")).isoformat() if _parse_any_date(f.get("expires_on")) else None) \
        if f.get("expires_on") else compute_expiry(entry["id"], signed_on, {"date": values.get("date")})
    signer = _signer(conn)
    if not entry["issued_by"] and not signer:
        conn.close()
        flash("Only the CFI themselves can sign this. Your login has no CFI profile of its own, "
              "and nobody can sign another CFI's endorsement.", "danger")
        return redirect(url_for("endorse.new", student_id=student["id"], e=entry["id"]))
    if entry["issued_by"]:
        cfi_id, cfi_name, cert, cert_kind, cert_date = (signer["id"] if signer else None), (f.get("issuer_name") or "").strip()[:120], \
            (f.get("issuer_cert") or "").strip()[:40], "", ""
        sig = f"/s/ {us_date(signed_on)} {cfi_name} {cert}".strip()
    else:
        cfi_id = signer["id"]
        cfi_name = signer["name"] or ""
        cert = (f.get("cfi_cert") or signer["cfi_cert_number"] or "").strip()[:40]
        cert_kind = "exp" if f.get("cert_kind") == "exp" else "re"
        cert_date = (f.get("cfi_cert_date") or signer["cfi_cert_expires"] or "").strip()[:10]
        sig = sig_line(cfi_name, cert, cert_kind, cert_date, signed_on)
    full = body + "\n\n" + sig
    cur = conn.execute("""INSERT INTO student_endorsements (student_id, cfi_id, cfi_name, kind, label, plane, body, signed_on,
                          created_at, catalog_id, expires_on, fields_json, cfi_cert, signature_line, created_by)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (student["id"], cfi_id, cfi_name, entry["id"], entry["title"][:120], values.get("M/M") or values.get("make and model (M/M)") or "",
                        full, signed_on, datetime.now().isoformat(timespec="seconds"), entry["id"], expires,
                        json.dumps(values), cert, sig, session.get("user_name")))
    eid = cur.lastrowid
    if entry["id"] in ("A.6", "A.7"):
        sync_solo_signoff(conn, student["id"])
    conn.commit()
    conn.close()
    flash(f"Saved {entry['id']} {entry['title']} for {student['name']}." +
          (f" Good until {us_date(expires)}." if expires else "") +
          (" Solo sign-off updated for booking." if entry["id"] in ("A.6", "A.7") else ""), "success")
    return redirect(url_for("endorse.view", endorsement_id=eid))


@endorse_bp.route("/<int:endorsement_id>")
def view(endorsement_id):
    denied = _access()
    if denied:
        return denied
    from pilotlog import _is_staff
    conn = get_db()
    rows = _rows(conn, "e.id = ?", (endorsement_id,))
    signer = None
    if rows:
        signer = conn.execute("SELECT signature FROM cfis WHERE id = ?", (rows[0]["cfi_id"],)).fetchone() if rows[0]["cfi_id"] else None
    conn.close()
    if not rows or not (_is_staff() or rows[0]["student_id"] == session.get("student_id")):
        abort(404)
    it = _item(rows[0], date.today())
    can_void = can_sign() and (_is_flight_admin() or rows[0]["cfi_id"] == session.get("cfi_id"))
    return render_template("academy_endorsement_view.html", it=it, e=rows[0], can_void=can_void,
                           signature=signer["signature"] if signer else None, tab="logbook", subtab="endorsements",
                           us_date=us_date)


@endorse_bp.route("/<int:endorsement_id>/void", methods=["POST"])
def void(endorsement_id):
    denied = _access()
    if denied:
        return denied
    conn = get_db()
    row = conn.execute("SELECT * FROM student_endorsements WHERE id = ?", (endorsement_id,)).fetchone()
    if not row or not can_sign() or not (_is_flight_admin() or row["cfi_id"] == session.get("cfi_id")):
        conn.close()
        abort(403)
    conn.execute("UPDATE student_endorsements SET voided_at = ?, voided_by = ?, void_reason = ? WHERE id = ?",
                 (now_iso(), session.get("user_name"), (request.form.get("reason") or "").strip()[:300] or None, endorsement_id))
    if (row["catalog_id"] or LEGACY_KIND.get(row["kind"])) in ("A.6", "A.7"):
        conn.execute("UPDATE students SET solo_signoff_date = NULL, solo_signoff_expires = NULL WHERE id = ?", (row["student_id"],))
        sync_solo_signoff(conn, row["student_id"])
    conn.commit()
    conn.close()
    flash("Endorsement removed. It no longer counts.", "success")
    return redirect(url_for("endorse.student_tab", student_id=row["student_id"]))


@endorse_bp.route("/given")
def given():
    """A CFI's record of every endorsement they gave (a Flight Admin can pick any CFI)."""
    denied = _access()
    if denied:
        return denied
    if not can_sign():
        abort(403)
    conn = get_db()
    cfis = _real_cfis(conn) if _is_flight_admin() else []
    cfi_id = request.args.get("cfi_id", type=int) if _is_flight_admin() else None
    if cfi_id is None:
        cfi_id = session.get("cfi_id")
    cfi = conn.execute("SELECT * FROM cfis WHERE id = ?", (cfi_id,)).fetchone() if cfi_id else None
    today = date.today()
    items = [_item(r, today) for r in _rows(conn, "e.cfi_id = ?", (cfi_id,))] if cfi_id else []
    holds = {r["id"]: (r["pilot_certificate"] or "") in HOLDS_CERTIFICATE
             for r in conn.execute("SELECT id, pilot_certificate FROM students").fetchall()}
    conn.close()
    current, expired = [], []
    by_student = {}
    for it in items:
        by_student.setdefault(it["row"]["student_id"], []).append(it)
    for sid, its in by_student.items():
        c, x, o = split_items(its, holds.get(sid, False))
        current += c
        expired += x + o
    current.sort(key=lambda x: x["signed_on"] or "", reverse=True)
    expired.sort(key=lambda x: x["signed_on"] or "", reverse=True)
    return render_template("academy_endorsements_given.html", cfi=cfi, cfis=cfis, current=current, expired=expired,
                           voided=[i for i in items if i["voided"]], tab="logbook", subtab="endorsements", us_date=us_date)


# ---------------------------------------------------------------- expiry alerts

_last_check = {"t": 0}


def run_expiry_check():
    """From the app's once-a-minute loop; does the work at most once an hour."""
    if time.time() - _last_check["t"] < 3600:
        return
    _last_check["t"] = time.time()
    conn = get_db()
    try:
        check_expiries(conn)
    finally:
        conn.close()


def check_expiries(conn, today=None):
    """Tells the admins and the signing CFI once when an endorsement that matters is 7 days from
    expiring, and once when it has expired. Skips replaced, removed and no-longer-needed ones and
    one-flight cross-countries. Returns the number of alerts sent."""
    import notify
    import push
    today = today or date.today()
    soon = (today + timedelta(days=7)).isoformat()
    rows = conn.execute("""SELECT e.*, s.name AS student_name, s.pilot_certificate, s.active AS s_active
                           FROM student_endorsements e JOIN students s ON s.id = e.student_id
                           WHERE e.voided_at IS NULL AND e.expires_on IS NOT NULL AND e.expires_on <= ?
                           AND s.active = 1 AND COALESCE(s.is_station, 0) = 0""", (soon,)).fetchall()
    sent = 0
    admins = [r["id"] for r in conn.execute(
        """SELECT id FROM users WHERE active = 1 AND (is_master_admin = 1 OR flight_role = 'admin'
           OR ',' || COALESCE(flight_roles, '') || ',' LIKE '%,admin,%')""").fetchall()]
    for r in rows:
        it = _item(r, today)
        if it["flight_specific"]:
            continue
        if it["student_pilot_only"] and (r["pilot_certificate"] or "") in HOLDS_CERTIFICATE:
            continue
        if it["currency"]:
            continue
        others = [_item(x, today) for x in conn.execute(
            "SELECT * FROM student_endorsements WHERE student_id = ? AND voided_at IS NULL AND id != ? AND signed_on >= ?",
            (r["student_id"], r["id"], r["signed_on"])).fetchall()]
        newer = any(o["family"] == it["family"] for o in others)
        if newer:
            continue
        expired = r["expires_on"] < today.isoformat()
        cat = "endorsement_expired" if expired else "endorsement_expiring"
        if conn.execute("SELECT 1 FROM notification_log WHERE category = ? AND ref_id = ? AND ref_key = ?",
                        (cat, r["id"], r["expires_on"])).fetchone():
            continue
        if expired:
            title = "Endorsement expired"
            body = f"{r['student_name']}'s {it['num']} {it['title']} endorsement expired {us_date(r['expires_on'])}."
        else:
            title = "Endorsement expiring soon"
            body = f"{r['student_name']}'s {it['num']} {it['title']} endorsement expires {us_date(r['expires_on'])}."
        if it["cid"] in ("A.6", "A.7"):
            body += " Solo bookings after that date get flagged for review until it's renewed (A.7)."
        recipients = set(admins)
        if r["cfi_id"]:
            cu = conn.execute("SELECT user_id FROM cfis WHERE id = ?", (r["cfi_id"],)).fetchone()
            if cu and cu["user_id"]:
                recipients.add(cu["user_id"])
        settings = notify.get_settings(conn)
        link = "/academy/endorsements/?student_id=%d" % r["student_id"]
        for uid in recipients:
            try:
                push.queue_and_push(conn, uid, title, body, tag=f"endorse-{r['id']}-{cat}", url=link)
                user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
                if user:
                    notify.notify_user(settings, user, title, body, brand="Fly with Kate!")
            except Exception:
                log.exception("Endorsement alert failed")
        conn.execute("INSERT INTO notification_log (category, ref_id, ref_key) VALUES (?, ?, ?)", (cat, r["id"], r["expires_on"]))
        conn.commit()
        sent += 1
    return sent
