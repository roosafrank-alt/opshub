"""Flight School: its own section/tile, reachable from the home launcher.
Login is unified with Shop Inventory - one master account (see auth.py) -
so this module's own decorators just check the session keys that master
login populates for anyone with a CFI or student role. Shares the `assets`
table with the shop/maintenance side (a plane is the same record either
way), and logging a flight advances that asset's Hobbs/Tach readings, which
is what keeps the maintenance tile's due-soon/overdue tracking accurate.

Phase 1 scope: accounts + login, CFI/student/plane profiles, and logging a
flight (plane, student, CFI, Hobbs/Tach, notes, oil added). Invoicing and
oil-consumption analytics are planned as later phases.
"""
from functools import wraps
from datetime import date, datetime, timedelta
import calendar as calendar_mod
import itertools
import math
import csv
import io

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, Response, current_app
from werkzeug.security import generate_password_hash

from db import get_db, now_iso, asset_meter, maintenance_status
from auth import authenticate, log_in_user, log_out_user, can_manage_billing, owner_locked
from pilotlog import NEXT_TRACK, TRACK_MAP
import weather
import adsb
import notams
import sun
import push
import notify
import threading
import pilotlog

# Distinct colors cycled through for each instructor on the schedule
# calendar, so flights are identifiable by instructor at a glance without
# hand-assigning a color to every CFI.
INSTRUCTOR_COLORS = ["#0d6efd", "#dc3545", "#198754", "#fd7e14", "#6f42c1", "#20c997", "#d63384", "#6c757d"]

# Same idea, but for planes - picked from a plane's own profile
# (asset_form.html, see PLANE_COLORS usage in app.py's asset_new/asset_edit)
# and consumed here for the dashboard's per-plane chip accent stripe.
PLANE_COLORS = ["#0d6efd", "#d63384", "#198754", "#fd7e14", "#6f42c1", "#20c997", "#dc3545", "#0dcaf0"]


def _instructor_color(name):
    if not name:
        return "#6c757d"
    return INSTRUCTOR_COLORS[sum(ord(ch) for ch in name) % len(INSTRUCTOR_COLORS)]


# Every color an admin can pick on the schedule - for an instructor (CFI
# form) or a plane (Planes > Edit). One shared palette, and a color taken
# by any active instructor or any plane is greyed out for everyone else, so
# a CFI and a plane can never share a color (the calendar shows both).
SCHEDULE_COLORS = INSTRUCTOR_COLORS + ["#0dcaf0", "#ffc107", "#8b4513", "#003f7f", "#556b2f",
                                       "#800000", "#008080", "#b8860b", "#4b0082", "#2f4f4f"]
PLANE_FALLBACK_COLOR = "#868e96"  # plane with no color picked yet
NO_CFI_STRIPE_COLOR = "#dee2e6"  # dashboard chip with no instructor (solo / needs CFI)


def _hex_to_rgb(hex_color):
    h = (hex_color or "").lstrip("#")
    if len(h) != 6:
        return (134, 142, 150)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _neon_color(hex_color):
    """The "neon" version of a plane color, used for solo bookings: same
    hue, fully saturated and bright."""
    import colorsys
    r, g, b = (v / 255 for v in _hex_to_rgb(hex_color))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if s < 0.08:  # grey/near-grey has no hue to brighten - use a neon lime
        h = 0.25
    r, g, b = colorsys.hls_to_rgb(h, 0.55, 1.0)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _text_on(hex_color):
    """White or near-black text, whichever reads better on this background."""
    r, g, b = _hex_to_rgb(hex_color)
    return "#111" if (0.299 * r + 0.587 * g + 0.114 * b) > 165 else "#fff"


def _cfi_display_color(cfi_name, cfi_color=None):
    """An admin-picked color (cfis.color) always wins; otherwise fall back
    to the old deterministic hash-based color so a CFI who's never picked
    one still shows up consistently."""
    return cfi_color or _instructor_color(cfi_name or "Solo")


def _plane_hash_color(tag):
    if not tag:
        return "#6c757d"
    return PLANE_COLORS[sum(ord(ch) for ch in tag) % len(PLANE_COLORS)]


def _plane_display_color(tag, plane_color=None, schedule_color=None):
    """The plane's stripe color on the dashboard. The color picked in
    Flight School > Planes > Edit (assets.schedule_color) wins, because
    that's the one the Schedule calendar and its legend use - so the
    dashboard always matches the Schedule. If only the shop-side asset
    form color (assets.color) is set, use that; with neither, the same
    grey the Schedule shows for a plane with no color picked."""
    return schedule_color or plane_color or PLANE_FALLBACK_COLOR


def _format_time_12h(hhmm):
    """'14:30' -> '2:30 PM'. Returns None for a blank/unset time."""
    if not hhmm:
        return None
    try:
        t = datetime.strptime(hhmm, "%H:%M")
    except ValueError:
        return hhmm
    # %-I (no leading zero) isn't available on every platform's strftime,
    # so build the 12-hour hour by hand instead of relying on it.
    hour12 = t.hour % 12 or 12
    return f"{hour12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


# A booked flight can't be started before its own slot - starting earlier
# than that has to go through the "move to now & start" flow instead (see
# schedule_start below and _early_start_modal.html), so the calendar,
# conflicts and billing reflect when it really flew. No free early grace
# window before that flow kicks in - kept as a named constant (0) since
# _start_window's math and every template that renders it still reads
# from here. A booking with no time set opens at the start of its
# scheduled day.
START_EARLY_BUFFER_MIN = 0


def _start_window(scheduled_date, scheduled_time):
    """Returns (can_start_now, opens_at) for a booking. opens_at is the
    earliest moment Start is allowed (a datetime), or None if the date
    can't be parsed (in which case starting isn't blocked)."""
    try:
        if scheduled_time:
            slot = datetime.strptime(f"{scheduled_date} {scheduled_time}", "%Y-%m-%d %H:%M")
            opens_at = slot - timedelta(minutes=START_EARLY_BUFFER_MIN)
        else:
            opens_at = datetime.strptime(scheduled_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return True, None
    return datetime.now() >= opens_at, opens_at


def _slot_label(scheduled_date, scheduled_time):
    """'Thu 9/24 at 2:00 PM' (or 'today at 2:00 PM') for a booking's slot -
    what the early-start prompt tells the instructor it's booked for."""
    try:
        d = datetime.strptime(scheduled_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return scheduled_date or ""
    day = "today" if d == date.today() else f"{d.strftime('%a')} {d.month}/{d.day}"
    return f"{day} at {_format_time_12h(scheduled_time)}" if scheduled_time else day


def _opens_label(opens_at):
    """'Tue 9/29 at 9:00 AM' (or 'today at 9:00 AM') for the not-yet message."""
    if not opens_at:
        return ""
    time_part = _format_time_12h(opens_at.strftime("%H:%M"))
    if opens_at.date() == date.today():
        return f"today at {time_part}"
    return f"{opens_at.strftime('%a')} {opens_at.month}/{opens_at.day} at {time_part}"


def _countdown_label(scheduled_date_str):
    """'2026-09-24' -> 'Today' / 'Tomorrow' / 'in 3 days', for the big "next
    lesson" dashboard tile."""
    try:
        d = datetime.strptime(scheduled_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return ""
    delta = (d - date.today()).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta < 0:
        return f"{abs(delta)} day{'s' if abs(delta) != 1 else ''} ago"
    return f"in {delta} days"


def _days_ago_label(date_str):
    """'2026-09-10' -> 'Today' / '3 days ago' / '2 weeks ago', for the "time
    since last flight" dashboard tile."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    days = (date.today() - d).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "1 day ago"
    if days < 14:
        return f"{days} days ago"
    weeks = days // 7
    if weeks < 8:
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    months = days // 30
    return f"{months} month{'s' if months != 1 else ''} ago"


DEFAULT_SCHEDULE_BLOCK_HOURS = 1.5  # assumed length when a booking has a time but no duration


def _time_window(scheduled_time, duration_hours):
    """(start_minutes, end_minutes) since midnight for a booking, or None if
    no time was given at all (in which case it's treated as blocking the
    whole day, since there's nothing more specific to compare against)."""
    if not scheduled_time:
        return None
    try:
        h, m = (int(x) for x in scheduled_time.split(":"))
    except (ValueError, AttributeError):
        return None
    start = h * 60 + m
    dur = duration_hours if duration_hours else DEFAULT_SCHEDULE_BLOCK_HOURS
    end = start + int(round(dur * 60))
    return (start, end)


def _windows_overlap(a, b):
    """Two (start,end) minute windows, where None means 'all day' (no time
    given), overlap."""
    if a is None or b is None:
        return True
    return a[0] < b[1] and b[0] < a[1]


# Intro flights and one-time flyers have no student profile: they're booked
# against one shared placeholder student ("Guest / Intro", created by the
# db.py migration, a station-type account nobody logs in as), with the
# person's name/phone/email kept on the booking itself (guest_name/guest_phone/
# guest_email) and shown wherever the student's name would be.
GUEST_USERNAME = "__guest__"


def _guest_student_id(conn):
    row = conn.execute("SELECT id FROM students WHERE username = ?", (GUEST_USERNAME,)).fetchone()
    return row["id"] if row else None


def guest_student_id():
    """Template helper (registered on flight_bp below): the Guest / Intro
    placeholder student's id, for the student pickers."""
    conn = get_db()
    try:
        return _guest_student_id(conn)
    finally:
        conn.close()


def _guest_fields(conn, form, student_id):
    """(guest_name, guest_phone, guest_email) from the form when the booking is
    for the Guest placeholder, else (None, None, None)."""
    gid = _guest_student_id(conn)
    if not gid or str(student_id or "") != str(gid):
        return None, None, None
    return ((form.get("guest_name") or "").strip()[:80] or None,
            (form.get("guest_phone") or "").strip()[:30] or None,
            (form.get("guest_email") or "").strip()[:120] or None)


def _booking_confirm_state(conn, self_service, guest_name, student_id):
    """(confirmed_at, confirm_required, notify_user_id) for a brand-new
    booking. A student booking their own flight, or a guest/intro with no
    account to log into, has nobody but the person who just made it - so it
    starts confirmed and never shows an unconfirmed/confirmed mark at all
    (confirm_required=0). A CFI/admin booking it for a real student with a
    login is the case that actually needs the student's own OK (see
    schedule_new / schedule_confirm_booking): it starts unconfirmed
    (confirm_required=1), and notify_user_id is who to push a confirm
    request to."""
    if self_service or guest_name or not student_id:
        return now_iso(), 0, None
    row = conn.execute("SELECT user_id FROM students WHERE id = ?", (student_id,)).fetchone()
    if not row or not row["user_id"]:
        return now_iso(), 0, None
    return None, 1, row["user_id"]


def _notify_booking_confirm(conn, student_user_id, scheduled_id, plane_tag, scheduled_date, scheduled_time):
    """Pushes the student a request to confirm a flight someone else just
    booked for them - same fire-and-forget pattern as the ETA/running-late
    pushes above (best effort; a push failure shouldn't fail the booking)."""
    when = _format_time_12h(scheduled_time) if scheduled_time else "a time to be set"
    try:
        push.queue_and_push(conn, student_user_id, "Confirm your flight",
                            f"You're booked in {plane_tag} on {scheduled_date} at {when}. Open OpsHub to confirm.",
                            tag=f"confirm-{scheduled_id}", url=url_for("flight.dashboard"))
    except Exception:
        current_app.logger.exception("Booking-confirm push failed")


def _within_24h_of_slot(scheduled_date, scheduled_time):
    """Whether a booking's slot is less than 24 hours away (or already
    passed) - used to require the cancellation-fee acknowledgement on a
    student's own Cancel. An unparseable date doesn't block cancelling."""
    try:
        if scheduled_time:
            slot = datetime.strptime(f"{scheduled_date} {scheduled_time}", "%Y-%m-%d %H:%M")
        else:
            slot = datetime.strptime(scheduled_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    return slot - datetime.now() <= timedelta(hours=24)


def _notify_schedule_request(conn, sched, verb, note):
    """Pushes the assigned CFI (if any) and every active master admin that a
    student acted on their own booking - a change request or a self-cancel,
    both of which need someone at the school to actually look at it. Best
    effort, same as the other scheduling pushes above."""
    student = conn.execute("SELECT name FROM students WHERE id = ?", (sched["student_id"],)).fetchone()
    plane = conn.execute("SELECT tag FROM assets WHERE id = ?", (sched["asset_id"],)).fetchone()
    student_name = student["name"] if student else "A student"
    plane_tag = plane["tag"] if plane else "a plane"
    when = _slot_label(sched["scheduled_date"], sched["scheduled_time"])
    body = f"{student_name} {verb} their {when} flight in {plane_tag}: \"{note}\""
    recipients = set()
    if sched["cfi_id"]:
        cfi = conn.execute("SELECT user_id FROM cfis WHERE id = ?", (sched["cfi_id"],)).fetchone()
        if cfi and cfi["user_id"]:
            recipients.add(cfi["user_id"])
    admins = conn.execute("SELECT id FROM users WHERE active = 1 AND is_master_admin = 1").fetchall()
    recipients.update(a["id"] for a in admins)
    for uid in recipients:
        try:
            push.queue_and_push(conn, uid, "Flight schedule update", body,
                                tag=f"schedule-{sched['id']}-{verb.split()[0]}",
                                url=url_for("flight.schedule_calendar"))
        except Exception:
            current_app.logger.exception("Schedule-request push failed")


PAST_BOOKING_GRACE_MIN = 5  # a booking for "right now" typed a few minutes late still goes through


def _past_booking_error(scheduled_date, scheduled_time):
    """An error message when a booking's date (and time, if given) has
    already passed, else None. Past flights aren't booked - they're logged
    with Schedule Flight's "Flight Already Complete?" option instead."""
    try:
        d = datetime.strptime(scheduled_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    now = datetime.now()
    past = d < now.date()
    if not past and d == now.date() and scheduled_time:
        try:
            t = datetime.strptime(scheduled_time, "%H:%M").time()
        except ValueError:
            t = None
        past = t is not None and datetime.combine(d, t) < now - timedelta(minutes=PAST_BOOKING_GRACE_MIN)
    if not past:
        return None
    when = _us_date(scheduled_date) + (f" at {_format_time_12h(scheduled_time)}" if scheduled_time else "")
    return (f"{when} is in the past - a flight can't be booked before now. If it already happened, "
            f"tick \"Flight Already Complete?\" and log it with its date and time.")


def _tsa_gate_error(conn, student_id):
    """A heads-up message (not a block - see schedule_form.html's TSA
    banner and log_tsa_verify()) for a student's SECOND (or later) lesson
    if they haven't been TSA Verified yet on their profile, else None. A
    student's very first lesson is exempt (nothing to verify before
    they've ever flown), and so are the Guest / Intro placeholder and
    station accounts - neither is a real trainee."""
    if str(student_id) == str(_guest_student_id(conn)):
        return None
    student = conn.execute("SELECT name, is_station, tsa_verified_date FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student or student["is_station"] or student["tsa_verified_date"]:
        return None
    prior = conn.execute("SELECT COUNT(*) c FROM scheduled_flights WHERE student_id = ? AND status NOT IN ('cancelled', 'denied')",
                         (student_id,)).fetchone()["c"]
    if prior < 1:
        return None
    return f"{student['name']} isn't TSA Verified yet."


def tsa_gate_needed(student_id):
    """Template helper (registered on flight_bp below): whether the TSA
    heads-up banner applies to this student, for the scheduling form's
    student picker (see schedule_form.html)."""
    conn = get_db()
    try:
        return bool(_tsa_gate_error(conn, student_id))
    finally:
        conn.close()


def _scheduling_conflicts(conn, asset_id, cfi_id, student_id, scheduled_date, scheduled_time, duration_hours, exclude_id=None):
    """Checks the same day's other scheduled (not cancelled) flights for a
    double-booking: the same plane, the same instructor, or the same
    student at an overlapping time. Returns a list of dicts - empty if the
    booking is clear - each with a human-readable 'message' plus the
    conflicting flight's 'id' and 'date' so a caller can point the person
    straight at the specific booking in the way (not just tell them one
    exists)."""
    window = _time_window(scheduled_time, duration_hours)
    sql = """SELECT sf.*, a.tag as plane_tag, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name, c.name as cfi_name
             FROM scheduled_flights sf
             JOIN assets a ON a.id = sf.asset_id
             JOIN students s ON s.id = sf.student_id
             LEFT JOIN cfis c ON c.id = sf.cfi_id
             WHERE sf.status = 'scheduled' AND sf.scheduled_date = ?
               AND (sf.asset_id = ? OR sf.student_id = ? OR (sf.cfi_id IS NOT NULL AND sf.cfi_id = ?))"""
    params = [scheduled_date, asset_id, student_id, cfi_id]
    if exclude_id:
        sql += " AND sf.id != ?"
        params.append(exclude_id)
    rows = conn.execute(sql, params).fetchall()

    conflicts = []
    # A CFI with an expired medical can't be booked at all - reported like a
    # conflict so every save/approve/edit/reschedule path stops on it (a
    # repeating series skips just the dates past the expiry).
    med_problem = _cfi_medical_problem(conn, cfi_id, scheduled_date)
    if med_problem:
        conflicts.append({"message": med_problem, "id": 0, "date": scheduled_date, "plane_tag": None,
                          "student_name": None, "cfi_name": None, "time": None})
    # A CFI who's put in time off for this date/time can't be booked over it -
    # reported the same way so every save/approve/edit/reschedule path stops on it.
    time_off_problem = _cfi_time_off_conflict(conn, cfi_id, scheduled_date, scheduled_time, duration_hours)
    if time_off_problem:
        conflicts.append({"message": time_off_problem, "id": 0, "date": scheduled_date, "plane_tag": None,
                          "student_name": None, "cfi_name": None, "time": None})
    for r in rows:
        other_window = _time_window(r["scheduled_time"], r["duration_hours"])
        if not _windows_overlap(window, other_window):
            continue
        when = _format_time_12h(r["scheduled_time"]) or "an unspecified time"
        msg = None
        if r["asset_id"] == int(asset_id):
            msg = (f"{r['plane_tag']} is already booked at {when} ({r['student_name']}"
                   f"{' with ' + r['cfi_name'] if r['cfi_name'] else ' solo'}).")
        elif cfi_id and r["cfi_id"] == int(cfi_id):
            msg = f"{r['cfi_name']} is already booked at {when} ({r['plane_tag']}, {r['student_name']})."
        elif r["student_id"] == int(student_id) and r["student_id"] != _guest_student_id(conn):
            msg = (f"{r['student_name']} is already booked at {when} ({r['plane_tag']}"
                   f"{' with ' + r['cfi_name'] if r['cfi_name'] else ' solo'}).")
        if msg:
            conflicts.append({"message": msg, "id": r["id"], "date": r["scheduled_date"],
                              "plane_tag": r["plane_tag"], "student_name": r["student_name"],
                              "cfi_name": r["cfi_name"], "time": when})
    return conflicts


def _conflict_highlight_url(conflicts):
    """A link straight to the day on the schedule calendar holding the
    conflicting booking(s), with those specific blocks/rows highlighted (see
    the `highlight` query param on schedule_calendar) so the person can see
    exactly what they're up against instead of just a text description."""
    if not conflicts:
        return None
    ids = ",".join(str(c["id"]) for c in conflicts)
    return url_for("flight.schedule_calendar", view="day", date=conflicts[0]["date"], highlight=ids)

flight_bp = Blueprint("flight", __name__, url_prefix="/flight")
flight_bp.add_app_template_global(guest_student_id)
flight_bp.add_app_template_global(tsa_gate_needed)


def _parse_float(val):
    try:
        return float(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_int(val):
    try:
        return int(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


_MAX_RECURRENCE_OCCURRENCES = 104  # safety cap (~2 years of weekly bookings)


def _add_months(d, n):
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar_mod.monthrange(year, month)[1])
    return date(year, month, day)


def _generate_recurrence_dates(start_str, repeat, until_str):
    """Expands a start date + a repeat pattern (daily/weekly/biweekly/monthly)
    into the list of date strings to book, from the start date through (and
    including) the until date. Returns just [start_str] when repeat is
    'none' or blank, so callers can treat every booking - recurring or not -
    the same way. Capped at _MAX_RECURRENCE_OCCURRENCES so a mistyped
    far-future "until" date can't hang the request or flood the calendar."""
    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return [start_str]
    if not repeat or repeat == "none":
        return [start_str]
    try:
        until_date = datetime.strptime(until_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return [start_str]
    if until_date < start_date:
        return [start_str]
    step_days = {"daily": 1, "weekly": 7, "biweekly": 14}.get(repeat)
    dates = []
    cur = start_date
    n = 0
    while cur <= until_date and len(dates) < _MAX_RECURRENCE_OCCURRENCES:
        dates.append(cur.strftime("%Y-%m-%d"))
        n += 1
        if repeat == "monthly":
            cur = _add_months(start_date, n)
        elif step_days:
            cur = start_date + timedelta(days=step_days * n)
        else:
            break
    return dates


def _flight_hours(f):
    """Hours flown for one flight row: Hobbs time if both readings are on
    the record, else Tach time, else 0 (nothing billable yet)."""
    if f["hobbs_start"] is not None and f["hobbs_end"] is not None:
        return max(0.0, f["hobbs_end"] - f["hobbs_start"])
    if f["tach_start"] is not None and f["tach_end"] is not None:
        return max(0.0, f["tach_end"] - f["tach_start"])
    return 0.0


BILLING_ROUND_UP_HOURS = 0.1  # billed hours always round UP to this increment (6 min)

DEFAULT_SOLO_CURRENCY_DAYS = 90  # a student who hasn't flown dual (with a CFI) in this many days
                                  # needs a CFI checkout flight before they can be scheduled solo
                                  # again - used for any student without their own override set
                                  # (students.solo_currency_days) - e.g. a newer/lower-time student
                                  # can be held to a shorter interval than a more experienced one


def _last_dual_flight_date(conn, student_id):
    """Most recent flight_date this student flew WITH an instructor aboard
    (cfi_id set) - a CFI checkout resets their solo currency clock."""
    row = conn.execute(
        "SELECT MAX(flight_date) as d FROM flights WHERE student_id = ? AND cfi_id IS NOT NULL",
        (student_id,)
    ).fetchone()
    return row["d"] if row else None


def _solo_currency_interval(conn, student_id):
    """This student's own solo currency interval (days) if an admin/CFI set
    one on their profile, else the program-wide default."""
    row = conn.execute("SELECT solo_currency_days FROM students WHERE id = ?", (student_id,)).fetchone()
    if row and row["solo_currency_days"]:
        return row["solo_currency_days"]
    return DEFAULT_SOLO_CURRENCY_DAYS


def _solo_currency_status(conn, student_id):
    """Whether a student is currently OK to solo. A student with no dual
    flights on record yet is never current (needs an initial checkout);
    otherwise current as long as their last dual flight was within their
    own solo currency interval (their override, or the program default)."""
    interval_days = _solo_currency_interval(conn, student_id)
    last_dual = _last_dual_flight_date(conn, student_id)
    if not last_dual:
        return {"ok": False, "last_dual_date": None, "days_since": None, "interval_days": interval_days}
    try:
        days_since = (date.today() - datetime.strptime(last_dual, "%Y-%m-%d").date()).days
    except ValueError:
        return {"ok": False, "last_dual_date": last_dual, "days_since": None, "interval_days": interval_days}
    return {"ok": days_since <= interval_days, "last_dual_date": last_dual, "days_since": days_since,
            "interval_days": interval_days}


SOLO_SIGNOFF_VALID_DAYS = 90  # a student pilot's solo endorsement is good for 90 days from the
                              # CFI's sign-off - the expiry defaults to sign-off + this many days
                              # (students.solo_signoff_expires can still be typed in by hand)


def _us_date(iso):
    """YYYY-MM-DD -> DD-MM-YYYY (same display format as the usdate
    template filter in app.py) for dates baked into flash/review text."""
    try:
        y, m, d = str(iso)[:10].split("-")
        return f"{d}-{m}-{y}"
    except ValueError:
        return iso


def _solo_signoff_expiry_default(signoff_date):
    """Sign-off date (YYYY-MM-DD) + SOLO_SIGNOFF_VALID_DAYS, or None if the
    date is blank/unreadable."""
    try:
        return (datetime.strptime(signoff_date, "%Y-%m-%d").date()
                + timedelta(days=SOLO_SIGNOFF_VALID_DAYS)).isoformat()
    except (TypeError, ValueError):
        return None


def _solo_signoff_status(conn, student_id, on_date=None):
    """The student's 90-day solo sign-off as of on_date (YYYY-MM-DD,
    default today). "ok" is None when no sign-off/expiry is on file yet
    (nothing to check, so no flag), True when on_date is on or before the
    expiry, False when it's past it. days_left counts from on_date."""
    row = conn.execute("SELECT solo_signoff_date, solo_signoff_expires FROM students WHERE id = ?",
                       (student_id,)).fetchone()
    signoff = row["solo_signoff_date"] if row else None
    expires = (row["solo_signoff_expires"] if row else None) or _solo_signoff_expiry_default(signoff)
    out = {"ok": None, "signoff_date": signoff, "expires": expires, "days_left": None}
    if not expires:
        return out
    try:
        exp = datetime.strptime(expires, "%Y-%m-%d").date()
        check = datetime.strptime(on_date, "%Y-%m-%d").date() if on_date else date.today()
    except ValueError:
        return out
    out["days_left"] = (exp - check).days
    out["ok"] = check <= exp
    return out


MEDICAL_CLASSES = [("first", "First Class"), ("second", "Second Class"), ("third", "Third Class"), ("basicmed", "BasicMed")]
MEDICAL_WARN_DAYS = 30  # "expiring soon" window for the dashboard / list badges

# How a student usually pays - option text matches End Flight's own "How
# Paid" select (log_active.html) so a saved preference can pre-select it
# there. No Venmo/Zelle here - Frank asked for just Cash/Check/Card/Other
# on this one; End Flight's own "How Paid" picker is unaffected.
PAY_PREFERENCES = ["Cash", "Check", "Card", "Other"]


def _medical_status(row, on_date=None):
    """A student's or CFI's medical as of on_date (YYYY-MM-DD, default
    today): {"class", "label", "expires", "ok", "days_left"}. ok is None when
    no expiry is on file (not tracked - nothing is checked), True through
    the expiry date, False after it."""
    cls = row["medical_class"] if row is not None and "medical_class" in row.keys() else None
    expires = row["medical_expires"] if row is not None and "medical_expires" in row.keys() else None
    out = {"class": cls, "label": dict(MEDICAL_CLASSES).get(cls, "Medical"), "expires": expires,
           "ok": None, "days_left": None}
    if not expires:
        return out
    try:
        exp = datetime.strptime(expires, "%Y-%m-%d").date()
        check = datetime.strptime(on_date, "%Y-%m-%d").date() if on_date else date.today()
    except ValueError:
        return out
    out["days_left"] = (exp - check).days
    out["ok"] = check <= exp
    return out


def _medical_from_form(form):
    """(medical_class, medical_expires) from a student/CFI form - an unknown
    class or unreadable date is dropped rather than stored."""
    cls = (form.get("medical_class") or "").strip()
    if cls not in dict(MEDICAL_CLASSES):
        cls = None
    exp = (form.get("medical_expires") or "").strip()
    try:
        exp = datetime.strptime(exp, "%Y-%m-%d").date().isoformat() if exp else None
    except ValueError:
        exp = None
    return cls, exp


def _medical_expired_phrase(med):
    """'expired 09-21-2026' if it's already past, else 'runs out
    10-05-2026, before this flight' (a booking further out than the expiry)."""
    if med["days_left"] is not None and med["expires"] < date.today().isoformat():
        return f"expired {_us_date(med['expires'])}"
    return f"runs out {_us_date(med['expires'])}, before this flight"


def _cfi_time_off_conflict(conn, cfi_id, on_date, scheduled_time, duration_hours):
    """A blocking message if this CFI has requested time off that overlaps
    this booking's date/time, else None. A time-off entry with no
    start/end time blocks the whole day. A recurring entry (recurs_weekly)
    blocks every occurrence of its weekday from its anchor off_date onward,
    not just that one date."""
    if not cfi_id:
        return None
    window = _time_window(scheduled_time, duration_hours)
    on_weekday = datetime.strptime(on_date, "%Y-%m-%d").weekday()
    rows = conn.execute(
        """SELECT * FROM cfi_time_off WHERE cfi_id = ?
           AND ((recurs_weekly = 0 AND off_date = ?) OR (recurs_weekly = 1 AND off_date <= ?))""",
        (cfi_id, on_date, on_date)).fetchall()
    for r in rows:
        if r["recurs_weekly"] and datetime.strptime(r["off_date"], "%Y-%m-%d").weekday() != on_weekday:
            continue
        if r["start_time"]:
            start_h, start_m = (int(x) for x in r["start_time"].split(":"))
            if r["end_time"]:
                end_h, end_m = (int(x) for x in r["end_time"].split(":"))
                end_min = end_h * 60 + end_m
            else:
                end_min = 24 * 60
            off_window = (start_h * 60 + start_m, end_min)
        else:
            off_window = None  # no time given - the whole day is off
        if not _windows_overlap(window, off_window):
            continue
        name = conn.execute("SELECT name FROM cfis WHERE id = ?", (cfi_id,)).fetchone()["name"]
        when = (f" from {_format_time_12h(r['start_time'])} to {_format_time_12h(r['end_time'])}"
                if r["start_time"] and r["end_time"] else "")
        return f"{name} has time off on {_us_date(on_date)}{when}{' - ' + r['note'] if r['note'] else ''}."
    return None


def _cfi_medical_problem(conn, cfi_id, on_date):
    """A blocking message if this CFI's medical has expired as of on_date
    (CFIs can't fly without a current one), else None."""
    if not cfi_id:
        return None
    row = conn.execute("SELECT name, medical_class, medical_expires FROM cfis WHERE id = ?", (cfi_id,)).fetchone()
    med = _medical_status(row, on_date)
    if med["ok"] is False:
        return (f"{row['name']}'s {med['label'].lower()} medical {_medical_expired_phrase(med)} - "
                f"a CFI can't fly without a current medical. Update it on their CFI profile, or pick another instructor.")
    return None


def _medical_alerts(conn):
    """Active students and CFIs whose medical is expired or runs out within
    MEDICAL_WARN_DAYS - for the dashboard. Each: kind, id, name, status, url."""
    out = []
    for kind, table, endpoint, key in (("CFI", "cfis", "flight.cfi_edit", "cfi_id"),
                                      ("Student", "students", "flight.student_edit", "student_id")):
        extra = " AND is_station = 0" if table == "students" else ""
        for r in conn.execute(f"SELECT id, name, medical_class, medical_expires FROM {table} "
                              f"WHERE active = 1 AND medical_expires IS NOT NULL AND medical_expires != ''{extra}").fetchall():
            med = _medical_status(r)
            if med["ok"] is None or (med["ok"] and med["days_left"] > MEDICAL_WARN_DAYS):
                continue
            out.append({"kind": kind, "id": r["id"], "name": r["name"], "med": med,
                        "url": url_for(endpoint, **{key: r["id"]}, _anchor="medical")})
    out.sort(key=lambda a: a["med"]["days_left"])
    return out


# Pilot certificates, lowest to highest - the badge tier on the Students
# list and profile. Each: (code, label, short, color, icon).
PILOT_CERTIFICATES = [
    ("student", "Student Pilot", "Student", "#6c757d", "bi-mortarboard"),
    ("sport", "Sport Pilot", "Sport", "#20c997", "bi-send"),
    ("recreational", "Recreational Pilot", "Rec", "#198754", "bi-feather"),
    ("private", "Private Pilot", "PPL", "#0d6efd", "bi-airplane"),
    ("commercial", "Commercial Pilot", "CPL", "#6f42c1", "bi-airplane-engines"),
    ("atp", "Airline Transport Pilot", "ATP", "#b8860b", "bi-award-fill"),
]
# Add-on ratings/endorsements - each one adds a star to the badge; the
# instructor ones also add a "CFI" wing tag.
PILOT_RATINGS = [
    ("instrument", "Instrument"), ("complex", "Complex"), ("high_performance", "High Performance"),
    ("tailwheel", "Tailwheel"), ("multi_engine", "Multi-Engine"),
    ("cfi", "CFI"), ("cfii", "CFII"), ("mei", "MEI"),
]
INSTRUCTOR_RATINGS = ("cfi", "cfii", "mei")
# Each rating's own badge: (short text, icon, color). Instructor ratings
# share a purple so they read as a set.
RATING_BADGES = {
    "instrument": ("IFR", "bi-cloud-fog2-fill", "#495057"),
    "complex": ("Complex", "bi-gear-wide-connected", "#e8590c"),
    "high_performance": ("High Perf", "bi-speedometer2", "#c92a2a"),
    "tailwheel": ("Tailwheel", "bi-record-circle", "#8d5524"),
    "multi_engine": ("Multi", "bi-fan", "#0b7285"),
    "cfi": ("CFI", "bi-mortarboard-fill", "#5f3dc4"),
    "cfii": ("CFII", "bi-cloud-check-fill", "#5f3dc4"),
    "mei": ("MEI", "bi-people-fill", "#5f3dc4"),
}

# CFI credential/endorsement checkboxes (admin-set, hidden from students):
# instructor certificates plus aircraft-category endorsements, so it's easy
# to see which planes in the fleet a given CFI can fly/instruct in. Column
# names double as the form field names.
CFI_CREDENTIALS = [
    ("cred_cfi", "CFI"), ("cred_cfii", "CFII"), ("cred_mei", "MEI"),
    ("cred_agi", "AGI"), ("cred_bgi", "BGI"), ("cred_igi", "IGI"),
    ("cred_high_performance", "High Performance"), ("cred_complex", "Complex"), ("cred_tailwheel", "Tailwheel"),
]


def _cfi_creds_from_form(form):
    """{column_name: 0/1} for every CFI_CREDENTIALS checkbox in a submitted
    form - shared by cfi_new() and cfi_edit() so both save the exact same
    set of credentials/endorsements."""
    return {code: (1 if form.get(code) else 0) for code, _label in CFI_CREDENTIALS}


def _pilot_ratings_list(row):
    raw = row["pilot_ratings"] if row is not None and "pilot_ratings" in row.keys() else None
    known = dict(PILOT_RATINGS)
    return [r for r in (raw or "").split(",") if r in known]


def _row_get(row, key):
    return row[key] if row is not None and key in row.keys() else None


# Not a real tier in PILOT_CERTIFICATES (no certificate at all yet) - a
# synthetic badge, one step below Student Pilot, for a trainee who hasn't
# soloed and has nothing on file yet. Otherwise this student showed no
# badge anywhere at all, which read as "nothing to see" rather than
# "brand new" - see pilot_badge_info below.
PRE_SOLO_BADGE = {"code": "pre_solo", "label": "Pre-Solo", "short": "Pre-Solo", "color": "#adb5bd",
                  "icon": "bi-hourglass-split", "tier": 0}


@flight_bp.app_template_global()
def pilot_badge_info(row):
    """Badge for a student: tier from their certificate (the higher the
    certificate, the higher the tier), one star per add-on rating, and an
    instructor tag if they hold CFI/CFII/MEI. score orders students by it
    (certificate first, then ratings). A generic station account (not a
    real trainee) never gets one. No certificate at all, and not yet
    soloed, gets the synthetic Pre-Solo badge (PRE_SOLO_BADGE) instead of
    no badge; no certificate but already soloed still gets none - solo
    without ever recording a certificate is an existing-data gap, not
    something to relabel."""
    if _row_get(row, "is_station") or _row_get(row, "student_is_station"):
        return None
    cert = row["pilot_certificate"] if row is not None and "pilot_certificate" in row.keys() else None
    tiers = {c[0]: (i, c) for i, c in enumerate(PILOT_CERTIFICATES)}
    if cert not in tiers:
        if not _row_get(row, "first_solo_date"):
            b = PRE_SOLO_BADGE
            return {"code": b["code"], "label": b["label"], "short": b["short"], "color": b["color"],
                    "icon": b["icon"], "tier": b["tier"], "rating_items": [], "stars": 0, "ratings": [],
                    "instructor": [], "score": -10, "title": b["label"]}
        return None
    rank, (code, label, short, color, icon) = tiers[cert]
    ratings = _pilot_ratings_list(row)
    labels = dict(PILOT_RATINGS)
    instructor = [labels[r] for r in ratings if r in INSTRUCTOR_RATINGS]
    rating_items = [{"code": r, "label": labels[r], "short": RATING_BADGES[r][0], "icon": RATING_BADGES[r][1],
                     "color": RATING_BADGES[r][2]} for r in ratings if r in RATING_BADGES]
    return {"code": code, "label": label, "short": short, "color": color, "icon": icon, "tier": rank + 1,
            "rating_items": rating_items,
            "stars": len(ratings), "ratings": [labels[r] for r in ratings], "instructor": instructor,
            "score": rank * 10 + len(ratings),
            "title": label + (" - " + ", ".join(labels[r] for r in ratings) if ratings else "")}


@flight_bp.app_template_global()
def rating_badge_items(row):
    """Just the rating badges (works even with no certificate set)."""
    labels = dict(PILOT_RATINGS)
    return [{"code": r, "label": labels[r], "short": RATING_BADGES[r][0], "icon": RATING_BADGES[r][1],
             "color": RATING_BADGES[r][2]} for r in _pilot_ratings_list(row) if r in RATING_BADGES]


@flight_bp.app_template_global()
def student_training_info(row):
    """For the schedule/booking form: the certificate this student is
    working towards next (Flight Academy's NEXT_TRACK, keyed off their
    current pilot_certificate) and whether they've soloed yet
    (first_solo_date set). None for a station account (not a real trainee)
    or a row that isn't a student at all, so callers only show this for
    actual student pilots."""
    if row is None or (row["is_station"] if "is_station" in row.keys() else 0):
        return None
    cert = row["pilot_certificate"] if "pilot_certificate" in row.keys() else None
    track = TRACK_MAP.get(NEXT_TRACK.get(cert, "private"))
    soloed = bool(row["first_solo_date"]) if "first_solo_date" in row.keys() else False
    return {"working_towards": track["name"] if track else None, "soloed": soloed}


def _pilot_from_form(form):
    """(pilot_certificate, pilot_ratings) from the student form."""
    cert = (form.get("pilot_certificate") or "").strip()
    if cert not in {c[0] for c in PILOT_CERTIFICATES}:
        cert = None
    ratings = [code for code, _label in PILOT_RATINGS if form.get("rating_" + code)]
    return cert, (",".join(ratings) or None)


def _needs_review_flag(r):
    """Whether a booking should show the caution triangle on the Schedule
    and dashboard chips: flagged for review and still just booked. Unlike
    needs_review_active (unacknowledged only - the red outline and the
    dashboard's Needs Review banner), this stays on after someone
    acknowledges it, until the issue is actually resolved (a CFI added,
    sign-off renewed, currency back in date) or the booking is flown or
    cancelled."""
    return bool(r["needs_review"]) and r["status"] == "scheduled"


def _alert_fix_links(reason, scheduled_id, student_id):
    """The "Resolve" buttons for a flagged booking - each one goes straight
    to the page where that particular problem gets fixed: an expired solo
    sign-off to the student's sign-off dates, a missing instructor or an
    over-currency solo to the booking (add a CFI). A list of dicts
    (label, icon, url, hint), most useful first."""
    reason = reason or ""
    links = []
    edit_url = url_for("flight.schedule_edit", scheduled_id=scheduled_id)
    if "medical" in reason and student_id:
        links.append(dict(label="Update medical", icon="bi-heart-pulse",
                          url=url_for("flight.student_edit", student_id=student_id, _anchor="medical"),
                          hint="Enter the student's new medical expiration"))
    if "sign-off" in reason and student_id:
        links.append(dict(label="Renew solo sign-off", icon="bi-pen",
                          url=url_for("flight.student_edit", student_id=student_id, _anchor="solo-signoff"),
                          hint="Enter the new sign-off date on the student's profile"))
    if ("currency" in reason or "No dual" in reason or "medical" in reason):
        links.append(dict(label="Add an instructor", icon="bi-person-plus", url=edit_url,
                          hint="Make it a dual flight - or log a dual flight for this student to bring them current"))
        if student_id:
            links.append(dict(label="Check currency", icon="bi-person-lines-fill",
                              url=url_for("flight.student_edit", student_id=student_id),
                              hint="The student's solo currency and recent flights"))
    if not any(l["url"] == edit_url for l in links):
        links.append(dict(label="Assign CFI" if not links else "Open booking", icon="bi-person-plus" if not links else "bi-pencil",
                          url=edit_url, hint="Edit the booking"))
    return links


def _review_resolution(sf):
    """One line for the resolved archive saying how the flag went away,
    from the booking's current state (None = the booking was deleted)."""
    if sf is None:
        return "Booking deleted"
    status = sf["status"]
    if status == "cancelled":
        return "Booking cancelled"
    if status in ("in_progress", "completed"):
        return "Flight flown" if status == "completed" else "Flight started"
    if status != "scheduled":
        return f"Booking {status.replace('_', ' ')}"
    if sf["cfi_id"]:
        return f"Instructor assigned ({sf['cfi_name']})" if sf["cfi_name"] else "Instructor assigned"
    if sf["solo"]:
        return "Student cleared for solo (sign-off / currency now OK)"
    return "No longer flagged"


def _notify_student(conn, student_id, category, message, link=None, scheduled_flight_id=None):
    """Queues one row in the student's notification feed (student_notifications
    table) - shown as a banner on their dashboard and listed in full on
    their Alerts tab (my_alerts()) until they've seen it. Doesn't commit -
    callers insert this alongside whatever else they're already committing
    in the same transaction (e.g. schedule_approve/schedule_deny below).

    scheduled_flight_id, when the notification is about one particular
    booking, lets schedule_dismiss clear this notification too when the
    student dismisses that booking from "Your Requests"."""
    if not student_id:
        return
    conn.execute("""INSERT INTO student_notifications (student_id, category, message, link, created_at, scheduled_flight_id)
                     VALUES (?, ?, ?, ?, ?, ?)""", (student_id, category, message, link, now_iso(), scheduled_flight_id))


def _sync_flight_alerts(conn, who=None):
    """Keeps the Alerts tab (flight_alerts) in step with the bookings'
    needs_review flags. Runs after every Flight School form post (see
    _sync_alerts_after_post) and whenever the Alerts page opens, so it
    doesn't need hooking into each place a booking gets flagged/unflagged.

    1. Solo bookings still flagged are re-checked, so one that's now OK
       (new sign-off entered, or a dual flight logged that brings the
       student back into currency) clears on its own. Only ever clears -
       it never rewrites a still-flagged reason, which would reset the
       acknowledgment every day as the "N days ago" count ticks up.
    2. Every flagged, still-booked flight has exactly one open alert
       (opened if new; reason and acknowledged-by/when copied over).
    3. An open alert whose booking is no longer flagged (or was flown,
       cancelled or deleted) is resolved: resolved_at/by + how.

    Returns the list of resolution lines for alerts resolved just now (step
    3), so a caller acting on a live request can flash them - see
    _sync_alerts_after_post()."""
    today = date.today().isoformat()
    changed = False
    resolved_now = []
    solos = conn.execute("""SELECT id, student_id, scheduled_date FROM scheduled_flights
                            WHERE status = 'scheduled' AND needs_review = 1 AND solo = 1 AND cfi_id IS NULL""").fetchall()
    for r in solos:
        needs_review, _reason = _schedule_review_flag(conn, 1, None, r["student_id"], r["scheduled_date"])
        if not needs_review:
            conn.execute("""UPDATE scheduled_flights SET needs_review = 0, review_reason = NULL,
                             needs_review_acknowledged_at = NULL, needs_review_acknowledged_by = NULL WHERE id = ?""",
                         (r["id"],))
            changed = True

    flagged = conn.execute("""SELECT id, student_id, review_reason, needs_review_acknowledged_at, needs_review_acknowledged_by
                              FROM scheduled_flights WHERE status = 'scheduled' AND needs_review = 1""").fetchall()
    open_alerts = {a["scheduled_flight_id"]: a for a in
                   conn.execute("SELECT * FROM flight_alerts WHERE resolved_at IS NULL ORDER BY id").fetchall()}
    for f in flagged:
        a = open_alerts.pop(f["id"], None)
        if a is None:
            conn.execute("""INSERT INTO flight_alerts (scheduled_flight_id, student_id, reason, created_at,
                                                       acknowledged_at, acknowledged_by)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                         (f["id"], f["student_id"], f["review_reason"], now_iso(),
                          f["needs_review_acknowledged_at"], f["needs_review_acknowledged_by"]))
            changed = True
        elif (a["reason"], a["acknowledged_at"], a["acknowledged_by"], a["student_id"]) != (
                f["review_reason"], f["needs_review_acknowledged_at"], f["needs_review_acknowledged_by"], f["student_id"]):
            conn.execute("""UPDATE flight_alerts SET reason = ?, acknowledged_at = ?, acknowledged_by = ?, student_id = ?
                            WHERE id = ?""",
                         (f["review_reason"], f["needs_review_acknowledged_at"], f["needs_review_acknowledged_by"],
                          f["student_id"], a["id"]))
            changed = True
    for sf_id, a in open_alerts.items():
        sf = conn.execute("""SELECT sf.status, sf.cfi_id, sf.solo, c.name as cfi_name FROM scheduled_flights sf
                             LEFT JOIN cfis c ON c.id = sf.cfi_id WHERE sf.id = ?""", (sf_id,)).fetchone()
        resolution = _review_resolution(sf)
        conn.execute("UPDATE flight_alerts SET resolved_at = ?, resolved_by = ?, resolution = ? WHERE id = ?",
                     (now_iso(), who, resolution, a["id"]))
        changed = True
        resolved_now.append(resolution)
    if changed:
        conn.commit()
    return resolved_now


def _schedule_review_flag(conn, solo, cfi_id, student_id, flight_date=None):
    """Whether a booking needs admin/CFI review, and why - instead of
    refusing to save a flight that's over currency or missing an
    instructor, it saves but gets flagged so someone can go back in, and
    (for both cases) the flag clears itself once a CFI is added. A solo
    booked on a date past the student's 90-day solo sign-off expiry gets
    the same flag treatment (checked against flight_date, the booking's
    own date, so a solo booked weeks out is judged on that day). Returns
    (needs_review: 0|1, review_reason: str|None)."""
    if cfi_id:
        return 0, None
    if solo:
        reasons = []
        med_row = conn.execute("SELECT medical_class, medical_expires FROM students WHERE id = ?",
                               (student_id,)).fetchone()
        med = _medical_status(med_row, flight_date)
        if med["ok"] is False:
            reasons.append(f"Student's {med['label'].lower()} medical {_medical_expired_phrase(med)} - can't fly "
                           f"solo without a current medical. Add an instructor (dual is fine) or update the medical.")
        signoff = _solo_signoff_status(conn, student_id, flight_date)
        if signoff["ok"] is False:
            reasons.append(f"Solo sign-off expired {_us_date(signoff['expires'])} - this flight is past the "
                           f"student's 90-day solo endorsement. Needs a new CFI sign-off (or add an instructor).")
        currency = _solo_currency_status(conn, student_id)
        if not currency["ok"]:
            if currency["last_dual_date"]:
                reasons.append(f"Solo currency exceeded - last flew dual {currency['days_since']} days ago "
                               f"(over their {currency['interval_days']}-day limit). Needs CFI review "
                               f"(add an instructor) before this student flies solo.")
            else:
                reasons.append("No dual (CFI) flights on record yet for this student. Needs CFI review before flying solo.")
        if reasons:
            return 1, " ".join(reasons)
        return 0, None
    return 1, "No instructor assigned yet - go back in and assign one."


def _round_up_hours(hours, increment=BILLING_ROUND_UP_HOURS):
    """Rounds billable time UP to the next increment (default 0.1 hr / 6
    min) so a short flight or a few minutes of instructor clock time still
    bills something instead of vanishing into a fraction of a cent - e.g.
    3 minutes (0.05 hr) becomes 0.1 hr billed, not ~0. Zero stays zero
    (nothing flown/no instructor time = nothing billed). round() first
    to kill float noise (0.29999999999 shouldn't round up to 0.3->0.4)."""
    if not hours:
        return 0.0
    return math.ceil(round(hours, 6) / increment - 1e-9) * increment


def _flight_cost(f):
    """f needs hobbs_start/end, tach_start/end, solo, ground_time_hours,
    plane_rate (the effective rate for this student/plane) and
    instructor_rate (the effective rate for this student/CFI, ignored when
    solo). Returns a dict with the billable hours and a
    plane/instructor/ground/total cost breakdown - a solo flight has no
    instructor or ground charge since no instructor was involved. Ground
    time is billed separately at the instructor rate only - no plane cost,
    since some instructors charge for ground briefings on their own.

    When the flight was logged with the start/stop clock (f has an
    instructor_clock_hours value), the INSTRUCTOR is billed for that
    elapsed real time instead of the Hobbs/Tach hours - it covers the
    preflight briefing and postflight debrief around the actual time in
    the air, which is what the instructor's time is actually worth. The
    plane is still billed on Hobbs/Tach hours either way, since that's
    what the plane actually burned.

    Every billed-hours figure is rounded UP to the next tenth of an hour
    (see _round_up_hours) before rates are applied, so a handful of
    minutes still results in a real charge instead of rounding away to
    nothing."""
    hours = _round_up_hours(_flight_hours(f))
    raw_instructor_hours = f["instructor_clock_hours"] if f["instructor_clock_hours"] is not None else _flight_hours(f)
    # Part dual, part solo (flights.solo_hours): the instructor isn't billed
    # for the part the student flew alone.
    try:
        solo_part = 0.0 if f["solo"] else max(0.0, float(f["solo_hours"] or 0))
    except (KeyError, IndexError):
        solo_part = 0.0
    raw_instructor_hours = max(0.0, raw_instructor_hours - solo_part)
    instructor_hours = _round_up_hours(raw_instructor_hours)
    ground_hours = _round_up_hours(f["ground_time_hours"] or 0)
    plane_rate = f["plane_rate"] or 0
    instructor_rate = 0 if f["solo"] else (f["instructor_rate"] or 0)
    plane_cost = hours * plane_rate
    instructor_cost = 0 if f["solo"] else instructor_hours * instructor_rate
    ground_cost = 0 if f["solo"] else ground_hours * instructor_rate
    return {"hours": hours, "instructor_hours": instructor_hours, "ground_hours": ground_hours,
            "plane_rate": plane_rate, "instructor_rate": instructor_rate,
            "plane_cost": plane_cost, "instructor_cost": instructor_cost, "ground_cost": ground_cost,
            "total": plane_cost + instructor_cost + ground_cost}


def _log_field_change(conn, entity_type, entity_id, field_name, old_value, new_value, changed_by=None):
    """Records one change to a click-to-drill-down field (a student's rate
    overrides, a CFI's pay rate, etc.) for the field-history view. Only
    records an actual change, not a no-op save."""
    if old_value == new_value:
        return
    conn.execute("""INSERT INTO field_change_log (entity_type, entity_id, field_name, old_value, new_value, changed_by, changed_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                 (entity_type, entity_id, field_name, "" if old_value is None else str(old_value),
                  "" if new_value is None else str(new_value), changed_by, now_iso()))


def _ledger_entry(conn, student_id, entry_type, amount, note=None, flight_id=None, created_by=None):
    """Records one balance-changing event (funds added, a flight's cost
    auto-deducted, or a manual adjustment) and keeps students.balance - the
    denormalized running total used everywhere else (Students list, the
    finance row, the edit page) - in sync with it. amount is signed:
    positive = credit added, negative = amount owed/deducted."""
    conn.execute("""INSERT INTO student_ledger (student_id, entry_type, amount, flight_id, note, created_by, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                 (student_id, entry_type, amount, flight_id, note, created_by, now_iso()))
    conn.execute("UPDATE students SET balance = balance + ? WHERE id = ?", (amount, student_id))


def _deduct_flight_cost(conn, flight_row_dict, created_by=None):
    """Auto-deducts a completed flight's total cost from its student's
    balance, once, via the ledger. Guarded against double-deduction (e.g. a
    flight edited/re-saved after it was already logged) by checking for an
    existing student_ledger row for this flight_id first. Solo flights and
    flights with a $0 total (e.g. a plane/instructor rate never set) still
    get a $0 ledger entry - not skipped - so the flight's billing status is
    still traceable, but they don't change the balance."""
    flight_id = flight_row_dict.get("id")
    student_id = flight_row_dict.get("student_id")
    if not flight_id or not student_id:
        return
    existing = conn.execute("SELECT id FROM student_ledger WHERE entry_type = 'flight_deduction' AND flight_id = ?",
                            (flight_id,)).fetchone()
    if existing:
        return
    total = _flight_cost(flight_row_dict)["total"]
    _ledger_entry(conn, student_id, "flight_deduction", -total, note="Flight logged", flight_id=flight_id, created_by=created_by)


def _rededuct_flight_cost(conn, flight_row_dict, created_by=None):
    """Like _deduct_flight_cost, but for editing an already-logged flight:
    reverses the flight's previous ledger deduction (if any - the student
    may have a different balance effect after the edit than before) and
    re-deducts fresh from the now-current numbers."""
    flight_id = flight_row_dict.get("id")
    existing = conn.execute("SELECT id, student_id, amount FROM student_ledger WHERE entry_type = 'flight_deduction' AND flight_id = ?",
                            (flight_id,)).fetchone()
    if existing:
        conn.execute("UPDATE students SET balance = balance - ? WHERE id = ?", (existing["amount"], existing["student_id"]))
        conn.execute("DELETE FROM student_ledger WHERE id = ?", (existing["id"],))
    _deduct_flight_cost(conn, flight_row_dict, created_by=created_by)


def cfi_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("cfi_id"):
            flash("Log in as a CFI to do that.", "danger")
            return redirect(url_for("home_launcher"))
        return f(*args, **kwargs)
    return wrapper


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("cfi_id") and not session.get("student_id"):
            return redirect(url_for("home_launcher"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Master admins only - see auth.py. Kept here (rather than importing
    auth.master_admin_required directly) so it redirects back into Flight
    School instead of the shop dashboard."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("home_launcher"))
        if not session.get("is_master_admin"):
            flash("That's admin-only.", "danger")
            return redirect(url_for("flight.dashboard"))
        return f(*args, **kwargs)
    return wrapper


def billing_required(f):
    """Billing ($ totals, outstanding balances, marking paid) is only for
    CFIs an admin has specifically granted that to - see auth.can_manage_billing."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("home_launcher"))
        if not can_manage_billing():
            flash("Billing isn't turned on for your account - ask an admin.", "danger")
            return redirect(url_for("flight.dashboard"))
        return f(*args, **kwargs)
    return wrapper


def current_cfi(conn):
    cid = session.get("cfi_id")
    return conn.execute("SELECT * FROM cfis WHERE id = ?", (cid,)).fetchone() if cid else None


def current_student(conn):
    sid = session.get("student_id")
    return conn.execute("SELECT * FROM students WHERE id = ?", (sid,)).fetchone() if sid else None


@flight_bp.route("/")
def index():
    if session.get("cfi_id") or session.get("student_id"):
        return redirect(url_for("flight.dashboard"))
    return redirect(url_for("home_launcher"))


@flight_bp.route("/login", methods=["GET", "POST"])
def login():
    """Login now lives on the master homepage - this just sends people
    there (and still handles a posted form for anything old pointing here)."""
    if request.method == "POST":
        user_row = authenticate(request.form.get("username", ""), request.form.get("password", ""))
        if not user_row:
            flash("Incorrect username or password.", "danger")
            return redirect(url_for("home_launcher"))
        log_in_user(user_row)
        return redirect(url_for("flight.dashboard"))
    return redirect(url_for("home_launcher"))


@flight_bp.route("/logout")
def logout():
    log_out_user()
    return redirect(url_for("home_launcher"))


@flight_bp.route("/signup", methods=["GET", "POST"])
def signup():
    """CFI (admin) self-signup. Students don't sign up themselves - a CFI
    creates their profile from the Students page. Creates a master login
    account (flight_role='cfi') plus the linked CFI profile."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        rate = _parse_float(request.form.get("rate_per_hour")) or 0
        if not name or not username or not password:
            flash("Name, username, and password are all required.", "danger")
            return render_template("flight/signup.html", name=name, username=username)
        conn = get_db()
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            conn.close()
            flash(f"Username '{username}' is already taken.", "danger")
            return render_template("flight/signup.html", name=name, username="")
        cur = conn.execute(
            "INSERT INTO users (name, username, password_hash, password_plain, flight_role, active, created_at) "
            "VALUES (?, ?, ?, ?, 'cfi', 1, ?)",
            (name, username, generate_password_hash(password, method="pbkdf2:sha256"), password, now_iso()))
        conn.commit()
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        conn.close()
        log_in_user(user_row)
        if rate:
            conn = get_db()
            conn.execute("UPDATE cfis SET rate_per_hour = ? WHERE user_id = ?", (rate, user_row["id"]))
            conn.commit()
            conn.close()
        flash(f"Welcome, {name}!", "success")
        return redirect(url_for("flight.dashboard"))
    return render_template("flight/signup.html", name="", username="")


def _plane_maint_warnings(conn):
    """Oil-change and 100-hour status for every Flight School plane, pulled
    from the same maintenance_items the Maintenance tile tracks (category
    'oil_change' / '100hour') - so there's one source of truth instead of a
    second, separately-tracked interval. Sorted worst-first (overdue, then
    due soon, then ok) so the things that need attention show up first."""
    rows = conn.execute("""
        SELECT mi.*, a.tag as plane_tag, a.hobbs_hours, a.tach_hours
        FROM maintenance_items mi
        JOIN assets a ON a.id = mi.asset_id
        WHERE a.deleted_at IS NULL AND a.is_flight_asset = 1 AND mi.active = 1
          AND mi.category IN ('oil_change', '100hour')
        ORDER BY a.tag, mi.category
    """).fetchall()
    urgency_order = {"overdue": 0, "due_soon": 1, "unknown": 2, "ok": 3}
    items = []
    for m in rows:
        status = maintenance_status(m, asset_meter(m, m["hour_type"]))
        items.append({"plane_tag": m["plane_tag"], "name": m["name"], "category": m["category"], "status": status})
    items.sort(key=lambda x: urgency_order.get(x["status"]["urgency"], 4))
    return items


def _group_by_time(rows):
    """Groups a list of flight rows (already sorted by time - every caller
    of this is) into one bucket per overlapping cluster of bookings, so two
    or more flights whose scheduled windows overlap - not just ones that
    happen to start at the exact same minute - render as chips sharing one
    line (each in its own plane lane) instead of one row per distinct start
    time, which drew overlapping bookings as if they simply followed one
    another with a gap in between. Bookings with no time at all keep the
    old behavior of sharing a single "Not set" row with each other, since
    there's no window to compare them against."""
    clusters = []
    for r in rows:
        window = _time_window(r["scheduled_time"], r["duration_hours"])
        prev = clusters[-1] if clusters else None
        if window is None:
            if prev and prev["window"] is None:
                prev["flights"].append(r)
                continue
        elif prev and prev["window"] is not None and window[0] < prev["window"][1]:
            prev["flights"].append(r)
            prev["window"] = (prev["window"][0], max(prev["window"][1], window[1]))
            continue
        clusters.append({"window": window, "flights": [r]})
    groups = []
    for c in clusters:
        labels = list(dict.fromkeys((f["time_label"] or "Not set") for f in c["flights"]))
        time_label = labels[0] if len(labels) == 1 else f"{labels[0]} – {labels[-1]}"
        groups.append({"time_label": time_label, "flights": c["flights"]})
    return groups


def _with_dashboard_row_fields(rows):
    """Attaches time_label/end_time_label/display_color to each dashboard
    schedule row - the same computed fields _decorate_schedule_row() adds
    for the Schedule calendar, so a flight-chip's "Details" popup (which
    reuses that same modal - see _schedule_detail_modal.html) shows
    identical info whichever page it was opened from."""
    out = []
    for r in rows:
        time_label = _format_time_12h(r["scheduled_time"])
        window = _time_window(r["scheduled_time"], r["duration_hours"])
        end_time_label = (_format_time_12h(f"{window[1] // 60:02d}:{window[1] % 60:02d}")
                           if window and r["scheduled_time"] else None)
        # Instructor stripe = the CFI's own picked color (same as the
        # Schedule). No CFI on the booking (solo / needs CFI) gets a plain
        # grey stripe instead of a made-up color that could look like
        # another instructor's.
        cfi_stripe = (_cfi_display_color(r["cfi_name"], r["cfi_color"])
                      if r["cfi_id"] else NO_CFI_STRIPE_COLOR)
        # See the matching comment in _decorate_schedule_row() - same
        # "still flagged but already acknowledged" distinction, so a
        # dashboard chip's warning triangle agrees with the calendar's.
        needs_review_active = bool(r["needs_review"]) and not r["needs_review_acknowledged_at"]
        notes_visible, private_notes_visible = _note_visibility(r)
        out.append(dict(r, time_label=time_label, end_time_label=end_time_label,
                         display_color=cfi_stripe,
                         needs_review_active=needs_review_active,
                         needs_review_flag=_needs_review_flag(r),
                         notes_visible=notes_visible, private_notes_visible=private_notes_visible))
    return out


def _dashboard_context(conn, cfi, student):
    """Gathers every piece of data the dashboard template needs. Shared by
    the full-page dashboard() route and the live-refresh fragment route
    (dashboard_live()) below, so the two never drift out of sync."""
    active_flights = []
    upcoming = []
    pending_requests = []
    change_requests = []
    my_requests = []
    my_notifications = []
    my_unconfirmed = []
    plane_maint = _plane_maint_warnings(conn)
    # All / Mine toggle (CFIs): "all" (default) shows the whole school's
    # Today's Schedule, Upcoming and Recent Flights; "mine" narrows those
    # three to flights this instructor is on. Remembered in the session
    # (see dashboard()) so the live refresh keeps it.
    dash_scope = session.get("dash_scope", "all") if cfi else "all"
    mine_id = cfi["id"] if (cfi and dash_scope == "mine") else None
    if cfi:
        # Shows every instructor's recent flights, not just this CFI's own -
        # so anyone logging in sees the whole school's activity at a glance.
        recent_flights = conn.execute("""
            SELECT f.*, COALESCE(NULLIF(f.guest_name, '') || ' (guest)', s.name) as student_name, a.tag as plane_tag, a.name as plane_name,
                   c.name as cfi_name
            FROM flights f
            JOIN students s ON s.id = f.student_id
            JOIN assets a ON a.id = f.asset_id
            LEFT JOIN cfis c ON c.id = f.cfi_id
            WHERE (? IS NULL OR f.cfi_id = ?)
            ORDER BY f.created_at DESC LIMIT 25
        """, (mine_id, mine_id)).fetchall()
        # Every flight in progress school-wide (not just this CFI's own),
        # same as the Active Flights board (log_active) - so whoever is
        # logged in sees every plane that's out, including solos. Ending
        # one is still limited to its CFI/solo student/admin there.
        active_flights = conn.execute("""
            SELECT f.*, COALESCE(NULLIF(f.guest_name, '') || ' (guest)', s.name) as student_name, a.tag as plane_tag, c.name as cfi_name
            FROM flights f
            JOIN students s ON s.id = f.student_id
            JOIN assets a ON a.id = f.asset_id
            LEFT JOIN cfis c ON c.id = f.cfi_id
            WHERE f.started_at IS NOT NULL AND f.ended_at IS NULL
            ORDER BY f.started_at
        """).fetchall()
        # No cap - Upcoming groups everything it gets by day (below), so any
        # limit here was quietly cutting whole days off the bottom of the
        # list on a busy school even though the exact same bookings show
        # fine on the Schedule calendar, which has no such cap either.
        upcoming = conn.execute("""
            SELECT sf.*, a.tag as plane_tag, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name,
                   s.pilot_certificate as pilot_certificate, c.name as cfi_name, c.color as cfi_color
            FROM scheduled_flights sf
            JOIN assets a ON a.id = sf.asset_id
            JOIN students s ON s.id = sf.student_id
            LEFT JOIN cfis c ON c.id = sf.cfi_id
            WHERE sf.status = 'scheduled' AND sf.scheduled_date >= ?
              AND (? IS NULL OR sf.cfi_id = ?)
            ORDER BY sf.scheduled_date, sf.scheduled_time IS NULL, sf.scheduled_time
        """, (date.today().strftime("%Y-%m-%d"), mine_id, mine_id)).fetchall()
        upcoming = _with_dashboard_row_fields(upcoming)
        for u in upcoming:
            can_start, opens_at = _start_window(u["scheduled_date"], u["scheduled_time"])
            u["can_start"] = can_start
            u["start_opens_label"] = _opens_label(opens_at)
            u["slot_label"] = _slot_label(u["scheduled_date"], u["scheduled_time"])
        # Every student-submitted request awaiting an instructor's OK -
        # school-wide, same reasoning as recent_flights above, so whichever
        # CFI is logged in can act on it (not just one the student asked for).
        pending_requests = conn.execute("""
            SELECT sf.*, a.tag as plane_tag, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name, c.name as cfi_name
            FROM scheduled_flights sf
            JOIN assets a ON a.id = sf.asset_id
            JOIN students s ON s.id = sf.student_id
            LEFT JOIN cfis c ON c.id = sf.cfi_id
            WHERE sf.status = 'pending_approval'
            ORDER BY sf.scheduled_date, sf.scheduled_time IS NULL, sf.scheduled_time
        """).fetchall()
        pending_requests = [dict(p, time_label=_format_time_12h(p["scheduled_time"])) for p in pending_requests]
        # A student's own request to change one of their already-confirmed
        # bookings (schedule_request_change) - school-wide, same reasoning
        # as pending_requests above. Cleared by rescheduling the booking
        # (schedule_reschedule) or by an explicit Dismiss once it's been
        # handled another way (schedule_change_request_dismiss).
        change_requests = conn.execute("""
            SELECT sf.*, a.tag as plane_tag, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name, c.name as cfi_name
            FROM scheduled_flights sf
            JOIN assets a ON a.id = sf.asset_id
            JOIN students s ON s.id = sf.student_id
            LEFT JOIN cfis c ON c.id = sf.cfi_id
            WHERE sf.status = 'scheduled' AND sf.change_requested_at IS NOT NULL
            ORDER BY sf.scheduled_date, sf.scheduled_time IS NULL, sf.scheduled_time
        """).fetchall()
        change_requests = [dict(r, time_label=_format_time_12h(r["scheduled_time"])) for r in change_requests]
        # Confirmed bookings flagged for review (over-currency solo, or a
        # dual flight with no instructor assigned yet) - school-wide, same
        # reasoning as recent_flights/pending_requests above.
        needs_review_flights = conn.execute("""
            SELECT sf.*, a.tag as plane_tag, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name, c.name as cfi_name
            FROM scheduled_flights sf
            JOIN assets a ON a.id = sf.asset_id
            JOIN students s ON s.id = sf.student_id
            LEFT JOIN cfis c ON c.id = sf.cfi_id
            WHERE sf.status = 'scheduled' AND sf.needs_review = 1 AND sf.needs_review_acknowledged_at IS NULL
            ORDER BY sf.scheduled_date, sf.scheduled_time IS NULL, sf.scheduled_time
        """).fetchall()
        needs_review_flights = [dict(n, time_label=_format_time_12h(n["scheduled_time"]),
                                     fix_links=_alert_fix_links(n["review_reason"], n["id"], n["student_id"]))
                                for n in needs_review_flights]
        # Expired / soon-to-expire medicals (students + CFIs).
        medical_alerts = _medical_alerts(conn)
        # Bookings a late flight's updated ETA runs into (see _eta_impacts).
        eta_impacts = _eta_impacts(conn)
        eta_delayed_flights = []
        if eta_impacts:
            eta_delayed_flights = conn.execute(_SCHEDULE_ROW_SQL + " AND sf.id IN (%s) ORDER BY sf.scheduled_time"
                                               % ",".join("?" * len(eta_impacts)), list(eta_impacts.keys())).fetchall()
            eta_delayed_flights = [dict(e, time_label=_format_time_12h(e["scheduled_time"]),
                                        eta_reason=eta_impacts[e["id"]]["reason"],
                                        eta_flight_id=eta_impacts[e["id"]]["flight_id"])
                                   for e in eta_delayed_flights]
        # Today's agenda + a live "flights booked today" counter - school-wide,
        # so any admin/CFI looking at the dashboard sees the whole day's board
        # and watches the completed count climb as flights get logged/ended.
        today_flights = conn.execute("""
            SELECT sf.*, a.tag as plane_tag, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name,
                   s.pilot_certificate as pilot_certificate, c.name as cfi_name, c.color as cfi_color,
                   fl.id as flight_id
            FROM scheduled_flights sf
            JOIN assets a ON a.id = sf.asset_id
            JOIN students s ON s.id = sf.student_id
            LEFT JOIN cfis c ON c.id = sf.cfi_id
            LEFT JOIN flights fl ON fl.scheduled_flight_id = sf.id AND fl.ended_at IS NULL
            WHERE sf.scheduled_date = ? AND sf.status IN ('scheduled', 'in_progress', 'completed')
              AND (? IS NULL OR sf.cfi_id = ?)
            ORDER BY sf.scheduled_time IS NULL, sf.scheduled_time
        """, (date.today().strftime("%Y-%m-%d"), mine_id, mine_id)).fetchall()
        today_flights = _with_dashboard_row_fields(today_flights)
        # Same early-start check as `upcoming` above, so a chip's Start on
        # today's board asks before starting a flight that isn't due yet.
        for t in today_flights:
            can_start, opens_at = _start_window(t["scheduled_date"], t["scheduled_time"])
            t["can_start"] = can_start
            t["start_opens_label"] = _opens_label(opens_at)
            t["slot_label"] = _slot_label(t["scheduled_date"], t["scheduled_time"])
        today_flights_total = len(today_flights)
        today_flights_completed = sum(1 for t in today_flights if t["status"] == "completed")
        # Admin-only watch list: bookings a CFI/admin made for a student that
        # the student hasn't confirmed yet, and flights cancelled in the last
        # few days (self-cancelled by a student or cancelled outright) - both
        # easy to miss otherwise since neither shows up on Today's Schedule
        # or Upcoming once cancelled.
        unconfirmed_flights = []
        recently_cancelled_flights = []
        if session.get("is_master_admin"):
            unconfirmed_flights = conn.execute("""
                SELECT sf.*, a.tag as plane_tag, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name, c.name as cfi_name
                FROM scheduled_flights sf
                JOIN assets a ON a.id = sf.asset_id
                JOIN students s ON s.id = sf.student_id
                LEFT JOIN cfis c ON c.id = sf.cfi_id
                WHERE sf.status = 'scheduled' AND sf.confirm_required = 1 AND sf.confirmed_at IS NULL
                ORDER BY sf.scheduled_date, sf.scheduled_time IS NULL, sf.scheduled_time
            """).fetchall()
            unconfirmed_flights = [dict(u, time_label=_format_time_12h(u["scheduled_time"])) for u in unconfirmed_flights]
            cutoff = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
            recently_cancelled_flights = conn.execute("""
                SELECT sf.*, a.tag as plane_tag, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name, c.name as cfi_name
                FROM scheduled_flights sf
                JOIN assets a ON a.id = sf.asset_id
                JOIN students s ON s.id = sf.student_id
                LEFT JOIN cfis c ON c.id = sf.cfi_id
                WHERE sf.status = 'cancelled' AND sf.scheduled_date >= ?
                ORDER BY sf.scheduled_date DESC, sf.scheduled_time IS NULL, sf.scheduled_time DESC
                LIMIT 15
            """, (cutoff,)).fetchall()
            recently_cancelled_flights = [dict(c, time_label=_format_time_12h(c["scheduled_time"]))
                                          for c in recently_cancelled_flights]
    else:
        needs_review_flights = []
        eta_delayed_flights = []
        medical_alerts = []
        today_flights = []
        today_flights_total = 0
        today_flights_completed = 0
        unconfirmed_flights = []
        recently_cancelled_flights = []
        recent_flights = conn.execute("""
            SELECT f.*, c.name as cfi_name, a.tag as plane_tag, a.name as plane_name
            FROM flights f
            LEFT JOIN cfis c ON c.id = f.cfi_id
            JOIN assets a ON a.id = f.asset_id
            WHERE f.student_id = ?
            ORDER BY f.created_at DESC LIMIT 10
        """, (student["id"],)).fetchall()
        upcoming = conn.execute("""
            SELECT sf.*, a.tag as plane_tag, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name, c.name as cfi_name, c.color as cfi_color
            FROM scheduled_flights sf
            JOIN assets a ON a.id = sf.asset_id
            JOIN students s ON s.id = sf.student_id
            LEFT JOIN cfis c ON c.id = sf.cfi_id
            WHERE sf.status = 'scheduled' AND sf.scheduled_date >= ? AND sf.student_id = ?
            ORDER BY sf.scheduled_date, sf.scheduled_time IS NULL, sf.scheduled_time
            LIMIT 300
        """, (date.today().strftime("%Y-%m-%d"), student["id"])).fetchall()
        upcoming = _with_dashboard_row_fields(upcoming)
        # Bookings a CFI/admin made for this student that still need their
        # OK (see schedule_confirm_booking) - otherwise the only sign of
        # this is a small icon on the flight chip, easy to miss since
        # Upcoming starts collapsed. Surfaced as its own banner below.
        my_unconfirmed = [u for u in upcoming if u["confirm_required"] and not u["confirmed_at"]]
        # The student's own requests that are still pending or got denied,
        # plus any change they've asked for on an already-confirmed booking
        # (schedule_request_change) that hasn't been handled yet, so they
        # can see where things stand instead of them just vanishing once
        # sent. A change request drops off here the same way it drops off
        # the school's Change Requests queue - rescheduled or dismissed.
        my_requests = conn.execute("""
            SELECT sf.*, a.tag as plane_tag, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name, c.name as cfi_name
            FROM scheduled_flights sf
            JOIN assets a ON a.id = sf.asset_id
            JOIN students s ON s.id = sf.student_id
            LEFT JOIN cfis c ON c.id = sf.cfi_id
            WHERE sf.student_id = ? AND sf.scheduled_date >= ?
              AND ((sf.status IN ('pending_approval', 'denied') AND sf.student_dismissed_at IS NULL)
                   OR (sf.status = 'scheduled' AND sf.change_requested_at IS NOT NULL))
            ORDER BY sf.scheduled_date, sf.scheduled_time IS NULL, sf.scheduled_time
        """, (student["id"], date.today().strftime("%Y-%m-%d"))).fetchall()
        my_requests = [dict(m, time_label=_format_time_12h(m["scheduled_time"])) for m in my_requests]
        # Unread notifications ("your flight was approved", etc. - see
        # _notify_student) for the dashboard banner. Capped at 5 so a
        # student who hasn't looked in a while gets a banner, not a wall -
        # the Alerts tab (my_alerts()) has the full history.
        my_notifications = conn.execute("""SELECT * FROM student_notifications WHERE student_id = ? AND read_at IS NULL
                                           ORDER BY created_at DESC LIMIT 5""", (student["id"],)).fetchall()

    # Weather + NOTAMs - school-wide, not CFI-specific, so a student sees
    # exactly the same strip as an instructor (a student flying solo or
    # planning a lesson needs this at least as much). Cached (see
    # weather.py/notams.py), and never allowed to break the dashboard if
    # every outside API is unreachable.
    try:
        dashboard_wx = weather.get_dashboard_weather(conn)
    except Exception:
        dashboard_wx = None
        current_app.logger.exception("Dashboard weather fetch failed")
    try:
        dashboard_notams = notams.get_dashboard_notams(conn)
    except Exception:
        dashboard_notams = None
        current_app.logger.exception("Dashboard NOTAM fetch failed")

    # Big "time to next lesson" tile with a live countdown - the single next
    # block that hasn't started yet, for whoever's looking: a CFI sees their
    # own next lesson, a student theirs. Master admins can flip it to the
    # whole school's next block (?next=school / ?next=mine, remembered for
    # this login - see dashboard()).
    next_scope = "school" if (session.get("is_master_admin") and session.get("next_scope") == "school") else "mine"
    next_lesson = None
    now_dt = datetime.now()
    today_s = now_dt.strftime("%Y-%m-%d")
    if cfi or student or next_scope == "school":
        nl_sql = """
            SELECT sf.*, a.tag as plane_tag, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name, c.name as cfi_name
            FROM scheduled_flights sf
            JOIN assets a ON a.id = sf.asset_id
            JOIN students s ON s.id = sf.student_id
            LEFT JOIN cfis c ON c.id = sf.cfi_id
            WHERE sf.status = 'scheduled'
              AND (sf.scheduled_date > ? OR (sf.scheduled_date = ? AND (sf.scheduled_time IS NULL OR sf.scheduled_time >= ?)))"""
        nl_args = [today_s, today_s, now_dt.strftime("%H:%M")]
        if next_scope == "school":
            pass
        elif cfi:
            nl_sql += " AND sf.cfi_id = ?"
            nl_args.append(cfi["id"])
        else:
            nl_sql += " AND sf.student_id = ?"
            nl_args.append(student["id"])
        nl_sql += " ORDER BY sf.scheduled_date, sf.scheduled_time IS NULL, sf.scheduled_time LIMIT 1"
        row = conn.execute(nl_sql, nl_args).fetchone()
        if row:
            nl_notes_visible, nl_private_notes_visible = _note_visibility(row)
            next_lesson = dict(row, countdown=_countdown_label(row["scheduled_date"]),
                               time_label=_format_time_12h(row["scheduled_time"]),
                               starts_at=(f"{row['scheduled_date']}T{row['scheduled_time']}:00"
                                          if row["scheduled_time"] else None),
                               notes_visible=nl_notes_visible, private_notes_visible=nl_private_notes_visible)

    # "Time since last flight" tile - student dashboard only.
    last_flight_ago = None
    solo_currency = None
    if student and not student["is_station"]:
        if recent_flights and recent_flights[0]["flight_date"]:
            last_flight_ago = _days_ago_label(recent_flights[0]["flight_date"])
        solo_currency = _solo_currency_status(conn, student["id"])

    # The merged "Today's Schedule" + "Upcoming" section groups `upcoming`
    # by date so each future day gets its own compact one-row-per-flight
    # table under a heading in the same style as today's. For a CFI, today
    # itself is skipped here since the Today's Schedule block above
    # (today_flights) is already the richer, status-aware view of that same
    # day; a student has no such block, so their own today's bookings (if
    # any) get a same-styled "Today's Schedule" group here instead, or
    # they'd silently vanish from the page. groupby relies on `upcoming`
    # already being sorted by scheduled_date (the query is), which is
    # required for it to group correctly instead of splitting a day into
    # multiple buckets.
    today_str = date.today().strftime("%Y-%m-%d")
    upcoming_future_days = []
    for day, group in itertools.groupby(upcoming, key=lambda u: u["scheduled_date"]):
        if day == today_str and cfi:
            continue
        day_flights = list(group)
        if day == today_str:
            weekday_label = "Today"
        else:
            try:
                weekday_label = datetime.strptime(day, "%Y-%m-%d").strftime("%A")
            except Exception:
                weekday_label = ""
        # Chips for this day are further grouped by time slot (see
        # _group_by_time) so simultaneous bookings share one line.
        upcoming_future_days.append(dict(date=day, weekday=weekday_label, flights=day_flights,
                                          time_groups=_group_by_time(day_flights)))

    # Today's Schedule chip list only shows what's still upcoming (scheduled
    # or in progress) - a completed flight has nothing left to act on, and
    # the "X of Y complete" badge above already reports the completed count,
    # so repeating every finished flight as a chip too just added length to
    # a section that's supposed to be a quick compact glance at the board.
    today_time_groups = _group_by_time([t for t in today_flights if t["status"] != "completed"])
    # Current time (HH:MM, 24h) for the "now" line/dimming on Today's
    # Schedule - see time_group_row()/flight_chip() in _dashboard_live.html.
    # Naive local time, same as everywhere else in this file (the Pi's
    # clock is the shop's local time).
    today_now_time = datetime.now().strftime("%H:%M")

    # Stable per-plane column ("lane") for the same-time chip rows
    # (time_group_row() in _dashboard_live.html) - based on the WHOLE
    # fleet's tag order (not just who happens to be flying today), so a
    # given plane always renders in the same lane everywhere on the
    # dashboard instead of shifting around depending on which flights
    # happen to be booked or how they sorted that particular poll. The
    # accent color for each plane's stripe comes from its own profile
    # (Planes > Edit color, the same one the Schedule uses) rather than
    # this ordering - see _plane_display_color().
    fleet = conn.execute(
        "SELECT tag, color, schedule_color FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()
    plane_lane = {r["tag"]: i for i, r in enumerate(fleet)}
    plane_color = {r["tag"]: _plane_display_color(r["tag"], r["color"], r["schedule_color"]) for r in fleet}
    plane_lane_count = len(fleet) or 1

    return dict(cfi=cfi, student=student, recent_flights=recent_flights,
                active_flights=active_flights, upcoming=upcoming, plane_maint=plane_maint,
                pending_requests=pending_requests, change_requests=change_requests,
                my_requests=my_requests, my_notifications=my_notifications,
                my_unconfirmed=my_unconfirmed,
                needs_review_flights=needs_review_flights, eta_delayed_flights=eta_delayed_flights,
                medical_alerts=medical_alerts,
                unconfirmed_flights=unconfirmed_flights, recently_cancelled_flights=recently_cancelled_flights,
                next_lesson=next_lesson, last_flight_ago=last_flight_ago,
                solo_currency=solo_currency,
                solo_currency_days=solo_currency["interval_days"] if solo_currency else DEFAULT_SOLO_CURRENCY_DAYS,
                today_flights=today_flights, today_flights_total=today_flights_total,
                today_flights_completed=today_flights_completed, today_str=today_str,
                today_time_groups=today_time_groups, today_now_time=today_now_time,
                upcoming_future_days=upcoming_future_days,
                plane_lane=plane_lane, plane_color=plane_color, plane_lane_count=plane_lane_count,
                dashboard_wx=dashboard_wx, dashboard_notams=dashboard_notams,
                sun_times=sun.get_sun_times(), dash_scope=dash_scope, next_scope=next_scope)


@flight_bp.route("/dashboard")
@login_required
def dashboard():
    # ?scope=all|mine - the dashboard's All / Mine toggle, remembered for
    # this login (see _dashboard_context).
    if request.args.get("scope") in ("all", "mine"):
        session["dash_scope"] = request.args["scope"]
    # ?next=mine|school - master admins' toggle for the Next Lesson countdown.
    if request.args.get("next") in ("mine", "school") and session.get("is_master_admin"):
        session["next_scope"] = request.args["next"]
    conn = get_db()
    cfi = current_cfi(conn)
    student = current_student(conn)
    ctx = _dashboard_context(conn, cfi, student)
    conn.close()
    return render_template("flight/dashboard.html", **ctx)


@flight_bp.route("/dashboard/live")
@login_required
def dashboard_live():
    """Polled by the dashboard page (see dashboard.html) to refresh the
    fast-changing sections - active flights, today's schedule, upcoming,
    review/approval queues, recent flights - without a full page reload.
    Renders the exact same partial template dashboard.html includes, built
    from the exact same _dashboard_context() query logic as the full page,
    so a shop-TV/kiosk browser left open on /flight/dashboard stays current
    when a flight is ended or a booking is made from another device."""
    conn = get_db()
    cfi = current_cfi(conn)
    student = current_student(conn)
    ctx = _dashboard_context(conn, cfi, student)
    conn.close()
    return render_template("flight/_dashboard_live.html", **ctx)


_FINISHED_FLIGHT_SQL = "(f.ended_at IS NOT NULL OR f.started_at IS NULL)"  # logged/ended, not still in the air


def _landing_total(f):
    return sum((f[k] or 0) for k in ("day_landings_fs", "day_landings_tg", "night_landings_fs", "night_landings_tg"))


def _student_activity(conn, student_id=None):
    """Last lesson, last landing, and 90-day landing counts per student, for
    the Students list and the student's own page. A "lesson" is any finished
    flight on record (dual or solo); a "landing" is the most recent finished
    flight with any landings entered. Returns {student_id: {...}}."""
    sql = f"""SELECT f.id, f.student_id, f.flight_date, f.solo, f.cfi_id, c.name as cfi_name, a.tag as plane_tag,
                     f.day_landings_fs, f.day_landings_tg, f.night_landings_fs, f.night_landings_tg
              FROM flights f JOIN assets a ON a.id = f.asset_id LEFT JOIN cfis c ON c.id = f.cfi_id
              WHERE {_FINISHED_FLIGHT_SQL}"""
    params = []
    if student_id:
        sql += " AND f.student_id = ?"
        params.append(student_id)
    sql += " ORDER BY f.flight_date DESC, f.id DESC"
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    out = {}
    for f in conn.execute(sql, params).fetchall():
        a = out.setdefault(f["student_id"], {"last_lesson": None, "last_landing": None,
                                             "landings_90": 0, "night_fs_90": 0})
        if a["last_lesson"] is None:
            a["last_lesson"] = dict(f)
        total = _landing_total(f)
        if total and a["last_landing"] is None:
            a["last_landing"] = dict(f, landings_total=total)
        if f["flight_date"] >= cutoff:
            a["landings_90"] += (f["day_landings_fs"] or 0) + (f["day_landings_tg"] or 0) \
                + (f["night_landings_fs"] or 0) + (f["night_landings_tg"] or 0)
            a["night_fs_90"] += (f["night_landings_fs"] or 0)
    # Landings entered by hand (manual_landings - another school, a rental,
    # before this system) also count toward the 90-day currency total.
    ml_sql = "SELECT student_id, landing_date, day_landings, night_landings FROM manual_landings"
    ml_params = []
    if student_id:
        ml_sql += " WHERE student_id = ?"
        ml_params.append(student_id)
    for m in conn.execute(ml_sql, ml_params).fetchall():
        if m["landing_date"] >= cutoff:
            a = out.setdefault(m["student_id"], {"last_lesson": None, "last_landing": None,
                                                 "landings_90": 0, "night_fs_90": 0})
            a["landings_90"] += (m["day_landings"] or 0) + (m["night_landings"] or 0)
            a["night_fs_90"] += (m["night_landings"] or 0)
    for sid, a in out.items():
        for key in ("last_lesson", "last_landing"):
            if a[key]:
                try:
                    a[key]["days_ago"] = (date.today() - datetime.strptime(a[key]["flight_date"], "%Y-%m-%d").date()).days
                except ValueError:
                    a[key]["days_ago"] = None
    return out


@flight_bp.route("/students")
@cfi_required
def students_list():
    conn = get_db()
    students = conn.execute("SELECT * FROM students ORDER BY active DESC, name").fetchall()
    activity = _student_activity(conn)
    currency = {s["id"]: _solo_currency_status(conn, s["id"]) for s in students if not s["is_station"]}
    signoff = {s["id"]: _solo_signoff_status(conn, s["id"]) for s in students if not s["is_station"]}
    medical = {s["id"]: _medical_status(s) for s in students if not s["is_station"]}
    conn.close()
    return render_template("flight/students.html", students=students, activity=activity, currency=currency,
                           signoff=signoff, medical=medical, medical_warn_days=MEDICAL_WARN_DAYS)


def _refresh_solo_review_flags(conn, student_id):
    """Re-checks this student's upcoming solo bookings (no instructor on
    them) after their solo sign-off dates change, so a solo already on the
    calendar past the new expiry gets flagged the same way a new booking
    would - and one that's now covered clears. Only rows whose flag or
    reason actually changes are touched (a changed flag starts
    unacknowledged again). Returns how many are now newly flagged."""
    rows = conn.execute("""SELECT id, scheduled_date, needs_review, review_reason FROM scheduled_flights
                           WHERE student_id = ? AND solo = 1 AND cfi_id IS NULL
                             AND status = 'scheduled' AND scheduled_date >= ?""",
                        (student_id, date.today().isoformat())).fetchall()
    newly_flagged = 0
    for r in rows:
        needs_review, review_reason = _schedule_review_flag(conn, 1, None, student_id, r["scheduled_date"])
        if (needs_review, review_reason) == (r["needs_review"], r["review_reason"]):
            continue
        if needs_review and not r["needs_review"]:
            newly_flagged += 1
        conn.execute("""UPDATE scheduled_flights SET needs_review = ?, review_reason = ?,
                         needs_review_acknowledged_at = NULL, needs_review_acknowledged_by = NULL WHERE id = ?""",
                     (needs_review, review_reason, r["id"]))
    conn.commit()
    return newly_flagged


def _solo_signoff_from_form(form):
    """(solo_signoff_date, solo_signoff_expires) from the student form. A
    blank expiry fills itself in as sign-off + 90 days; a date that doesn't
    parse is dropped rather than stored as junk."""
    def _clean(v):
        v = (v or "").strip()
        try:
            return datetime.strptime(v, "%Y-%m-%d").date().isoformat() if v else None
        except ValueError:
            return None
    signoff = _clean(form.get("solo_signoff_date"))
    expires = _clean(form.get("solo_signoff_expires")) or _solo_signoff_expiry_default(signoff)
    return signoff, expires


@flight_bp.route("/students/new", methods=["GET", "POST"])
@cfi_required
def student_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        rate_override = _parse_float(request.form.get("rate_override"))
        plane_rate_override = _parse_float(request.form.get("plane_rate_override"))
        is_station = 1 if request.form.get("is_station") else 0
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        # Only a master admin can set/change a student's solo currency
        # interval - a regular CFI can't loosen (or tighten) how long their
        # own students can go without a checkout. Silently ignored (not
        # flashed as an error) for anyone else, same as the field just not
        # being on the form for them.
        solo_currency_days = _parse_int(request.form.get("solo_currency_days")) if session.get("is_master_admin") else None
        solo_signoff_date, solo_signoff_expires = _solo_signoff_from_form(request.form)
        if not name or not username or not password:
            flash("Name, username, and a starting password are all required.", "danger")
            return render_template("flight/student_form.html", student=None, default_solo_currency_days=DEFAULT_SOLO_CURRENCY_DAYS, medical_classes=MEDICAL_CLASSES, pilot_certificates=PILOT_CERTIFICATES, pilot_ratings=PILOT_RATINGS, pay_preferences=PAY_PREFERENCES)
        # A generic station account (e.g. "Shop") isn't a real trainee, so
        # it doesn't need a personal email/phone on file - everyone else does.
        if not is_station and (not email or not phone):
            flash("Email and phone number are required for a student profile.", "danger")
            return render_template("flight/student_form.html", student=None, default_solo_currency_days=DEFAULT_SOLO_CURRENCY_DAYS, medical_classes=MEDICAL_CLASSES, pilot_certificates=PILOT_CERTIFICATES, pilot_ratings=PILOT_RATINGS, pay_preferences=PAY_PREFERENCES)
        conn = get_db()
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            conn.close()
            flash(f"Username '{username}' is already taken.", "danger")
            return render_template("flight/student_form.html", student=None, default_solo_currency_days=DEFAULT_SOLO_CURRENCY_DAYS, medical_classes=MEDICAL_CLASSES, pilot_certificates=PILOT_CERTIFICATES, pilot_ratings=PILOT_RATINGS, pay_preferences=PAY_PREFERENCES)
        # A master login account (flight_role='student') plus the linked
        # student profile that holds their rate overrides - what THIS
        # student pays for the plane and for instruction, since both can
        # vary student to student rather than following one flat rate.
        cur = conn.execute(
            "INSERT INTO users (name, username, password_hash, password_plain, flight_role, active, email, phone, created_at) "
            "VALUES (?, ?, ?, ?, 'student', 1, ?, ?, ?)",
            (name, username, generate_password_hash(password, method="pbkdf2:sha256"), password, email or None, phone or None, now_iso()))
        conn.commit()
        user_id = cur.lastrowid
        conn.execute("""INSERT INTO students (name, username, password_hash, rate_override, plane_rate_override,
                         solo_currency_days, solo_signoff_date, solo_signoff_expires, is_station, created_by_cfi_id,
                         user_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (name, username, generate_password_hash(password, method="pbkdf2:sha256"), rate_override,
                      plane_rate_override, solo_currency_days, solo_signoff_date, solo_signoff_expires, is_station,
                      session.get("cfi_id"), user_id, now_iso()))
        medical_class, medical_expires = _medical_from_form(request.form)
        pilot_certificate, pilot_ratings = _pilot_from_form(request.form)
        conn.execute("UPDATE students SET medical_class = ?, medical_expires = ?, pilot_certificate = ?, pilot_ratings = ? WHERE user_id = ?",
                     (medical_class, medical_expires, pilot_certificate, pilot_ratings, user_id))
        conn.execute("UPDATE students SET first_solo_date = ? WHERE user_id = ?",
                     (_first_solo_from_form(request.form), user_id))
        conn.execute("UPDATE students SET tsa_verified_date = ? WHERE user_id = ?",
                     (_tsa_verified_from_form(request.form), user_id))
        conn.execute("UPDATE students SET pay_preference = ? WHERE user_id = ?",
                     (_pay_preference_from_form(request.form), user_id))
        conn.execute("UPDATE students SET notify_booking_confirm = ? WHERE user_id = ?",
                     (1 if request.form.get("notify_booking_confirm") else 0, user_id))
        conn.execute("UPDATE students SET sim_rate_override = ? WHERE user_id = ?",
                     (_parse_float(request.form.get("sim_rate_override")), user_id))
        conn.commit()
        conn.close()
        flash(f"Student '{name}' added. Give them their username and starting password to log in.", "success")
        return redirect(url_for("flight.students_list"))
    return render_template("flight/student_form.html", student=None, default_solo_currency_days=DEFAULT_SOLO_CURRENCY_DAYS, medical_classes=MEDICAL_CLASSES, pilot_certificates=PILOT_CERTIFICATES, pilot_ratings=PILOT_RATINGS, pay_preferences=PAY_PREFERENCES)


@flight_bp.route("/students/<int:student_id>/tsa_verify", methods=["POST"])
@cfi_required
def student_tsa_verify(student_id):
    """Quick TSA Verified check-off from the scheduling screen's heads-up
    banner (see schedule_form.html) - doesn't block booking the flight,
    just updates the student's profile right there so the banner clears."""
    conn = get_db()
    student = conn.execute("SELECT id FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        conn.close()
        return jsonify({"error": "Student not found."}), 404
    verified_date = (request.form.get("tsa_verified_date") or "").strip()[:10] or date.today().isoformat()
    conn.execute("UPDATE students SET tsa_verified_date = ? WHERE id = ?", (verified_date, student_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "tsa_verified_date": verified_date})


@flight_bp.route("/students/<int:student_id>/edit", methods=["GET", "POST"])
@cfi_required
def student_edit(student_id):
    conn = get_db()
    student = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        conn.close()
        flash("Student not found.", "danger")
        return redirect(url_for("flight.students_list"))
    if request.method == "POST" and owner_locked(student["user_id"], conn):
        conn.close()
        flash(f"{student['name']} is the owner account - only {student['name']} can change this profile.", "warning")
        return redirect(url_for("flight.students_list"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        rate_override = _parse_float(request.form.get("rate_override"))
        plane_rate_override = _parse_float(request.form.get("plane_rate_override"))
        # Admin-only field (see student_new) - a non-admin CFI editing other
        # fields on this student leaves the existing interval untouched
        # rather than having it silently wiped back to the default.
        if session.get("is_master_admin"):
            solo_currency_days = _parse_int(request.form.get("solo_currency_days"))
        else:
            solo_currency_days = student["solo_currency_days"]
        solo_signoff_date, solo_signoff_expires = _solo_signoff_from_form(request.form)
        active = 1 if request.form.get("active") else 0
        is_station = 1 if request.form.get("is_station") else 0
        new_password = request.form.get("password", "")
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        if not name:
            flash("Name is required.", "danger")
            conn.close()
            return render_template("flight/student_form.html", student=student, default_solo_currency_days=DEFAULT_SOLO_CURRENCY_DAYS, medical_classes=MEDICAL_CLASSES, pilot_certificates=PILOT_CERTIFICATES, pilot_ratings=PILOT_RATINGS, pay_preferences=PAY_PREFERENCES)
        if not is_station and (not email or not phone):
            flash("Email and phone number are required for a student profile.", "danger")
            conn.close()
            return render_template("flight/student_form.html", student=student, default_solo_currency_days=DEFAULT_SOLO_CURRENCY_DAYS, medical_classes=MEDICAL_CLASSES, pilot_certificates=PILOT_CERTIFICATES, pilot_ratings=PILOT_RATINGS, pay_preferences=PAY_PREFERENCES,
                                   student_email=email, student_phone=phone)
        if new_password:
            conn.execute("UPDATE students SET name=?, rate_override=?, plane_rate_override=?, solo_currency_days=?, solo_signoff_date=?, solo_signoff_expires=?, is_station=?, active=?, password_hash=? WHERE id=?",
                         (name, rate_override, plane_rate_override, solo_currency_days, solo_signoff_date, solo_signoff_expires,
                          is_station, active, generate_password_hash(new_password, method="pbkdf2:sha256"), student_id))
        else:
            conn.execute("UPDATE students SET name=?, rate_override=?, plane_rate_override=?, solo_currency_days=?, solo_signoff_date=?, solo_signoff_expires=?, is_station=?, active=? WHERE id=?",
                         (name, rate_override, plane_rate_override, solo_currency_days, solo_signoff_date, solo_signoff_expires,
                          is_station, active, student_id))
        who = session.get("user_name")
        _log_field_change(conn, "student", student_id, "rate_override", student["rate_override"], rate_override, who)
        _log_field_change(conn, "student", student_id, "plane_rate_override", student["plane_rate_override"], plane_rate_override, who)
        _log_field_change(conn, "student", student_id, "solo_signoff_date", student["solo_signoff_date"], solo_signoff_date, who)
        _log_field_change(conn, "student", student_id, "solo_signoff_expires", student["solo_signoff_expires"], solo_signoff_expires, who)
        medical_class, medical_expires = _medical_from_form(request.form)
        conn.execute("UPDATE students SET medical_class = ?, medical_expires = ? WHERE id = ?",
                     (medical_class, medical_expires, student_id))
        _log_field_change(conn, "student", student_id, "medical_class", student["medical_class"], medical_class, who)
        _log_field_change(conn, "student", student_id, "medical_expires", student["medical_expires"], medical_expires, who)
        pilot_certificate, pilot_ratings = _pilot_from_form(request.form)
        conn.execute("UPDATE students SET pilot_certificate = ?, pilot_ratings = ? WHERE id = ?",
                     (pilot_certificate, pilot_ratings, student_id))
        _log_field_change(conn, "student", student_id, "pilot_certificate", student["pilot_certificate"], pilot_certificate, who)
        _log_field_change(conn, "student", student_id, "pilot_ratings", student["pilot_ratings"], pilot_ratings, who)
        first_solo_date = _first_solo_from_form(request.form)
        conn.execute("UPDATE students SET first_solo_date = ? WHERE id = ?", (first_solo_date, student_id))
        _log_field_change(conn, "student", student_id, "first_solo_date", student["first_solo_date"], first_solo_date, who)
        tsa_verified_date = _tsa_verified_from_form(request.form)
        conn.execute("UPDATE students SET tsa_verified_date = ? WHERE id = ?", (tsa_verified_date, student_id))
        _log_field_change(conn, "student", student_id, "tsa_verified_date", student["tsa_verified_date"], tsa_verified_date, who)
        pay_preference = _pay_preference_from_form(request.form)
        conn.execute("UPDATE students SET pay_preference = ? WHERE id = ?", (pay_preference, student_id))
        _log_field_change(conn, "student", student_id, "pay_preference", student["pay_preference"], pay_preference, who)
        sim_rate_override = _parse_float(request.form.get("sim_rate_override"))
        conn.execute("UPDATE students SET sim_rate_override = ? WHERE id = ?", (sim_rate_override, student_id))
        _log_field_change(conn, "student", student_id, "sim_rate_override", student["sim_rate_override"], sim_rate_override, who)
        notify_booking_confirm = 1 if request.form.get("notify_booking_confirm") else 0
        conn.execute("UPDATE students SET notify_booking_confirm = ? WHERE id = ?", (notify_booking_confirm, student_id))
        if student["user_id"]:
            conn.execute("UPDATE users SET email = ?, phone = ? WHERE id = ?",
                         (email or None, phone or None, student["user_id"]))
        conn.commit()
        if ((solo_signoff_date, solo_signoff_expires) != (student["solo_signoff_date"], student["solo_signoff_expires"])
                or (medical_class, medical_expires) != (student["medical_class"], student["medical_expires"])):
            reflagged = _refresh_solo_review_flags(conn, student_id)
            if reflagged:
                flash(f"{reflagged} upcoming solo flight{'s' if reflagged != 1 else ''} for this student "
                      f"{'are' if reflagged != 1 else 'is'} now flagged for review - past the solo sign-off or medical expiry.", "warning")
        # Keep the master login account (the actual place passwords/active
        # are checked at sign-in) in step with this edit.
        if student["user_id"]:
            if new_password:
                conn.execute("UPDATE users SET name=?, active=?, password_hash=?, password_plain=? WHERE id=?",
                             (name, active, generate_password_hash(new_password, method="pbkdf2:sha256"), new_password, student["user_id"]))
            else:
                conn.execute("UPDATE users SET name=?, active=? WHERE id=?", (name, active, student["user_id"]))
            conn.commit()
        conn.close()
        flash("Student updated.", "success")
        return redirect(url_for("flight.students_list"))
    ledger = conn.execute("SELECT * FROM student_ledger WHERE student_id = ? ORDER BY created_at DESC, id DESC LIMIT 25",
                          (student_id,)).fetchall()
    activity = _student_activity(conn, student_id).get(student_id)
    recent_flights = conn.execute(f"""SELECT f.*, c.name as cfi_name, a.tag as plane_tag FROM flights f
                                      JOIN assets a ON a.id = f.asset_id LEFT JOIN cfis c ON c.id = f.cfi_id
                                      WHERE f.student_id = ? AND {_FINISHED_FLIGHT_SQL}
                                      ORDER BY f.flight_date DESC, f.id DESC LIMIT 10""", (student_id,)).fetchall()
    solo_currency = None if student["is_station"] else _solo_currency_status(conn, student_id)
    solo_signoff = None if student["is_station"] else _solo_signoff_status(conn, student_id)
    medical = None if student["is_station"] else _medical_status(student)
    user_row = conn.execute("SELECT email, phone FROM users WHERE id = ?", (student["user_id"],)).fetchone() if student["user_id"] else None
    manual_landings = conn.execute("SELECT * FROM manual_landings WHERE student_id = ? ORDER BY landing_date DESC, id DESC LIMIT 10",
                                    (student_id,)).fetchall()
    conn.close()
    return render_template("flight/student_form.html", student=student, default_solo_currency_days=DEFAULT_SOLO_CURRENCY_DAYS,
                           ledger=ledger, can_bill=can_manage_billing(), activity=activity,
                           recent_flights=recent_flights, solo_currency=solo_currency, solo_signoff=solo_signoff,
                           medical=medical, medical_classes=MEDICAL_CLASSES, pilot_certificates=PILOT_CERTIFICATES, pilot_ratings=PILOT_RATINGS, pay_preferences=PAY_PREFERENCES,
                           student_email=user_row["email"] if user_row else None, student_phone=user_row["phone"] if user_row else None,
                           manual_landings=manual_landings, today=date.today().isoformat())


@flight_bp.route("/students/<int:student_id>/landings/add", methods=["POST"])
@cfi_required
def student_landing_add(student_id):
    """A landing that happened outside a logged flight (another school, a
    rental, before this system) but still counts toward the student's
    90-day landing currency - see manual_landings in schema.sql."""
    conn = get_db()
    student = conn.execute("SELECT id FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        conn.close()
        flash("Student not found.", "danger")
        return redirect(url_for("flight.students_list"))
    landing_date = request.form.get("landing_date") or date.today().isoformat()
    day_landings = _parse_int(request.form.get("day_landings")) or 0
    night_landings = _parse_int(request.form.get("night_landings")) or 0
    note = (request.form.get("note") or "").strip() or None
    if day_landings <= 0 and night_landings <= 0:
        flash("Enter at least one landing.", "danger")
    else:
        conn.execute("""INSERT INTO manual_landings (student_id, landing_date, day_landings, night_landings, note, created_by)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                     (student_id, landing_date, day_landings, night_landings, note, session.get("user_name")))
        conn.commit()
        flash("Landings added.", "success")
    conn.close()
    return redirect(url_for("flight.student_edit", student_id=student_id))


@flight_bp.route("/students/<int:student_id>/landings/<int:landing_id>/delete", methods=["POST"])
@cfi_required
def student_landing_delete(student_id, landing_id):
    conn = get_db()
    conn.execute("DELETE FROM manual_landings WHERE id = ? AND student_id = ?", (landing_id, student_id))
    conn.commit()
    conn.close()
    flash("Removed.", "success")
    return redirect(url_for("flight.student_edit", student_id=student_id))


@flight_bp.route("/students/<int:student_id>/add_funds", methods=["POST"])
@billing_required
def student_add_funds(student_id):
    """Records a payment/credit onto a student's balance - reached from the
    Add Funds control on the student Edit page. Billing-gated (same as the
    Billing page itself) rather than just cfi_required, since this moves
    money the same way marking a flight paid does."""
    conn = get_db()
    student = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        conn.close()
        flash("Student not found.", "danger")
        return redirect(url_for("flight.students_list"))
    amount = _parse_float(request.form.get("amount"))
    note = request.form.get("note", "").strip() or None
    if not amount or amount <= 0:
        flash("Enter a positive dollar amount to add.", "danger")
        conn.close()
        return redirect(url_for("flight.student_edit", student_id=student_id))
    _ledger_entry(conn, student_id, "funds_added", amount, note=note, created_by=session.get("user_name"))
    conn.commit()
    conn.close()
    flash(f"Added ${amount:.2f} to {student['name']}'s balance.", "success")
    return redirect(url_for("flight.student_edit", student_id=student_id))


_STUDENT_FIELD_INFO = {
    "plane_rate": {"label": "Plane Rate", "column": "plane_rate_override", "log_field": "plane_rate_override"},
    "instructor_rate": {"label": "Instructor Rate", "column": "rate_override", "log_field": "rate_override"},
    "sim_rate": {"label": "Sim Rate", "column": "sim_rate_override", "log_field": "sim_rate_override"},
}


@flight_bp.route("/students/<int:student_id>/field/<field_key>", methods=["GET", "POST"])
@cfi_required
def student_field_detail(student_id, field_key):
    """Click-to-drill-down view for one field on a student: current value,
    an edit form (admin only), and a full change history (who/when) from
    field_change_log. 'account' is a special case - it has no single
    editable value, so this shows the ledger (the account's own audit
    trail) and a link to Add Funds instead."""
    conn = get_db()
    student = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        conn.close()
        flash("Student not found.", "danger")
        return redirect(url_for("flight.students_list"))

    if field_key == "account":
        ledger = conn.execute("SELECT * FROM student_ledger WHERE student_id = ? ORDER BY created_at DESC, id DESC LIMIT 100",
                              (student_id,)).fetchall()
        conn.close()
        return render_template("flight/student_field_detail.html", student=student, field_key=field_key,
                               label="Account", ledger=ledger, can_bill=can_manage_billing())

    info = _STUDENT_FIELD_INFO.get(field_key)
    if not info:
        conn.close()
        flash("Unknown field.", "danger")
        return redirect(url_for("flight.student_edit", student_id=student_id))

    if request.method == "POST":
        if not session.get("is_master_admin"):
            flash("Only an admin can change this.", "danger")
            conn.close()
            return redirect(url_for("flight.student_field_detail", student_id=student_id, field_key=field_key))
        new_value = _parse_float(request.form.get("value"))
        old_value = student[info["column"]]
        conn.execute(f"UPDATE students SET {info['column']} = ? WHERE id = ?", (new_value, student_id))
        _log_field_change(conn, "student", student_id, info["log_field"], old_value, new_value, session.get("user_name"))
        conn.commit()
        conn.close()
        flash(f"{info['label']} updated.", "success")
        return redirect(url_for("flight.student_field_detail", student_id=student_id, field_key=field_key))

    history = conn.execute("""SELECT * FROM field_change_log WHERE entity_type = 'student' AND entity_id = ? AND field_name = ?
                              ORDER BY changed_at DESC, id DESC LIMIT 50""", (student_id, info["log_field"])).fetchall()
    conn.close()
    return render_template("flight/student_field_detail.html", student=student, field_key=field_key,
                           label=info["label"], current_value=student[info["column"]], history=history)


@flight_bp.route("/cfis")
@admin_required
def cfis_list():
    """Admin-only CFI roster - the flight-school equivalent of the shop
    side's Laborers page: each instructor's rate and active status, kept
    separate from account creation (still done from /admin/users) the same
    way rate_override/plane_rate_override live on the Students page rather
    than the account form."""
    conn = get_db()
    q = request.args.get("q", "").strip()
    query = "SELECT c.*, u.can_bill as can_bill FROM cfis c LEFT JOIN users u ON u.id = c.user_id WHERE 1=1"
    params = []
    if q:
        query += " AND c.name LIKE ?"
        params.append(f"%{q}%")
    query += " ORDER BY c.active DESC, c.name"
    cfis = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("flight/cfis.html", cfis=cfis, q=q, medical={c["id"]: _medical_status(c) for c in cfis}, medical_warn_days=MEDICAL_WARN_DAYS)


def _used_cfi_colors(conn, exclude_cfi_id=None):
    """Colors already picked by another active CFI or by any plane, so the
    form can gray those swatches out - two instructors (or an instructor
    and a plane) sharing a color defeats the point of color-coding."""
    query = "SELECT color FROM cfis WHERE color IS NOT NULL AND active = 1"
    params = []
    if exclude_cfi_id:
        query += " AND id != ?"
        params.append(exclude_cfi_id)
    return {r["color"] for r in conn.execute(query, params).fetchall()} | _used_plane_colors(conn)


def _used_plane_colors(conn, exclude_asset_id=None):
    """Colors already picked for a flight-school plane (optionally not
    counting one plane - the one being edited)."""
    query = "SELECT schedule_color FROM assets WHERE schedule_color IS NOT NULL AND deleted_at IS NULL AND is_flight_asset = 1"
    params = []
    if exclude_asset_id:
        query += " AND id != ?"
        params.append(exclude_asset_id)
    return {r["schedule_color"] for r in conn.execute(query, params).fetchall()}


def _used_colors_for_plane(conn, asset_id):
    """What a plane can't pick: every active CFI's color plus every other
    plane's color."""
    cfi_colors = {r["color"] for r in conn.execute(
        "SELECT color FROM cfis WHERE color IS NOT NULL AND active = 1").fetchall()}
    return cfi_colors | _used_plane_colors(conn, exclude_asset_id=asset_id)


def _used_solo_colors_for_plane(conn, asset_id):
    """Solo Colors already picked by other planes - its own namespace
    (doesn't need to avoid CFI/Schedule colors, since it only ever shows on
    that plane's own solo bookings, not next to an instructor's stripe)."""
    rows = conn.execute(
        "SELECT solo_color FROM assets WHERE solo_color IS NOT NULL AND deleted_at IS NULL "
        "AND is_flight_asset = 1 AND id != ?", (asset_id,)).fetchall()
    return {r["solo_color"] for r in rows}


@flight_bp.route("/cfis/new", methods=["GET", "POST"])
@admin_required
def cfi_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        rate_per_hour = _parse_float(request.form.get("rate_per_hour")) or 0
        can_bill = 1 if request.form.get("can_bill") else 0
        color = request.form.get("color", "").strip() or None
        conn = get_db()
        if color and color not in SCHEDULE_COLORS:
            color = None
        if color and color in _used_cfi_colors(conn):
            flash("That color is already taken by another instructor or a plane - pick a different one.", "danger")
            return render_template("flight/cfi_form.html", medical_classes=MEDICAL_CLASSES, cfi=None, used_colors=_used_cfi_colors(conn),
                                    instructor_colors=SCHEDULE_COLORS, name=name, username=username)
        if not name or not username or not password:
            flash("Name, username, and a starting password are all required.", "danger")
            return render_template("flight/cfi_form.html", medical_classes=MEDICAL_CLASSES, cfi=None, used_colors=_used_cfi_colors(conn),
                                    instructor_colors=SCHEDULE_COLORS)
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            conn.close()
            flash(f"Username '{username}' is already taken.", "danger")
            return render_template("flight/cfi_form.html", medical_classes=MEDICAL_CLASSES, cfi=None, instructor_colors=SCHEDULE_COLORS)
        cur = conn.execute(
            "INSERT INTO users (name, username, password_hash, password_plain, flight_role, can_bill, active, created_at) "
            "VALUES (?, ?, ?, ?, 'cfi', ?, 1, ?)",
            (name, username, generate_password_hash(password, method="pbkdf2:sha256"), password, can_bill, now_iso()))
        conn.commit()
        user_id = cur.lastrowid
        conn.execute(
            "INSERT INTO cfis (name, username, password_hash, rate_per_hour, color, active, user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (name, username, generate_password_hash(password, method="pbkdf2:sha256"), rate_per_hour, color, user_id, now_iso()))
        medical_class, medical_expires = _medical_from_form(request.form)
        conn.execute("UPDATE cfis SET medical_class = ?, medical_expires = ? WHERE user_id = ?",
                     (medical_class, medical_expires, user_id))
        conn.execute("UPDATE cfis SET cfi_cert_number = ?, cfi_cert_expires = ? WHERE user_id = ?",
                     (*_cfi_cert_from_form(request.form), user_id))
        # The New CFI form shows the same Credentials/Endorsements
        # checkboxes as Edit, so save them here too instead of silently
        # dropping whatever was ticked on create.
        creds = _cfi_creds_from_form(request.form)
        cred_cols = ", ".join(f"{code}=?" for code, _label in CFI_CREDENTIALS)
        conn.execute(f"UPDATE cfis SET {cred_cols} WHERE user_id = ?",
                     [*(creds[code] for code, _label in CFI_CREDENTIALS), user_id])
        gender = request.form.get("gender", "").strip() or None
        if gender not in ("M", "F", None):
            gender = None
        conn.execute("UPDATE cfis SET gender = ? WHERE user_id = ?", (gender, user_id))
        conn.commit()
        conn.close()
        flash(f"CFI '{name}' added. Give them their username and starting password to log in.", "success")
        return redirect(url_for("flight.cfis_list"))
    conn = get_db()
    used_colors = _used_cfi_colors(conn)
    conn.close()
    return render_template("flight/cfi_form.html", medical_classes=MEDICAL_CLASSES, cfi=None, used_colors=used_colors, instructor_colors=SCHEDULE_COLORS)


@flight_bp.route("/cfis/<int:cfi_id>/edit", methods=["GET", "POST"])
@admin_required
def cfi_edit(cfi_id):
    conn = get_db()
    cfi_row = conn.execute("SELECT * FROM cfis WHERE id = ?", (cfi_id,)).fetchone()
    if not cfi_row:
        conn.close()
        flash("CFI not found.", "danger")
        return redirect(url_for("flight.cfis_list"))
    user_row = conn.execute("SELECT * FROM users WHERE id = ?", (cfi_row["user_id"],)).fetchone() if cfi_row["user_id"] else None
    if request.method == "POST" and owner_locked(cfi_row["user_id"], conn):
        # Same rule as Admin > Accounts: the owner's own instructor profile
        # (name, password, active, billing) can only be changed by the owner.
        conn.close()
        flash(f"{cfi_row['name']} is the owner account - only {cfi_row['name']} can change this profile.", "warning")
        return redirect(url_for("flight.cfis_list"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        rate_per_hour = _parse_float(request.form.get("rate_per_hour")) or 0
        pay_rate_per_hour = _parse_float(request.form.get("pay_rate_per_hour"))
        can_bill = 1 if request.form.get("can_bill") else 0
        active = 1 if request.form.get("active") else 0
        is_station = 1 if request.form.get("is_station") else 0
        new_password = request.form.get("password", "")
        color = request.form.get("color", "").strip() or None
        creds = _cfi_creds_from_form(request.form)
        cred_cols = ", ".join(f"{code}=?" for code, _label in CFI_CREDENTIALS)
        cred_vals = [creds[code] for code, _label in CFI_CREDENTIALS]
        gender = request.form.get("gender", "").strip() or None
        if gender not in ("M", "F", None):
            gender = None
        if color and color not in SCHEDULE_COLORS:
            color = None
        if not name:
            flash("Name is required.", "danger")
            conn.close()
            return render_template("flight/cfi_form.html", medical_classes=MEDICAL_CLASSES, cfi=cfi_row, used_colors=_used_cfi_colors(conn, cfi_id),
                                    instructor_colors=SCHEDULE_COLORS)
        if color and color in _used_cfi_colors(conn, exclude_cfi_id=cfi_id):
            flash("That color is already taken by another instructor or a plane - pick a different one.", "danger")
            conn.close()
            return redirect(url_for("flight.cfi_edit", cfi_id=cfi_id))
        if new_password:
            conn.execute(f"""UPDATE cfis SET name=?, rate_per_hour=?, pay_rate_per_hour=?, color=?, active=?, is_station=?, password_hash=?,
                             gender=?, {cred_cols} WHERE id=?""",
                         [name, rate_per_hour, pay_rate_per_hour, color, active, is_station, generate_password_hash(new_password, method="pbkdf2:sha256"),
                          gender, *cred_vals, cfi_id])
        else:
            conn.execute(f"""UPDATE cfis SET name=?, rate_per_hour=?, pay_rate_per_hour=?, color=?, active=?, is_station=?,
                             gender=?, {cred_cols} WHERE id=?""",
                         [name, rate_per_hour, pay_rate_per_hour, color, active, is_station, gender, *cred_vals, cfi_id])
        _log_field_change(conn, "cfi", cfi_id, "pay_rate_per_hour", cfi_row["pay_rate_per_hour"], pay_rate_per_hour, session.get("user_name"))
        medical_class, medical_expires = _medical_from_form(request.form)
        conn.execute("UPDATE cfis SET medical_class = ?, medical_expires = ? WHERE id = ?",
                     (medical_class, medical_expires, cfi_id))
        conn.execute("UPDATE cfis SET cfi_cert_number = ?, cfi_cert_expires = ? WHERE id = ?",
                     (*_cfi_cert_from_form(request.form), cfi_id))
        # Signature pad on Edit CFI (the CFI can also sign on My CFI Profile).
        sig_changed, signature = _signature_from_form(request.form)
        if request.form.get("clear_signature"):
            sig_changed, signature = True, None
        if sig_changed and (signature or cfi_row["signature"]):
            _save_cfi_signature(conn, cfi_id, signature)
            _log_field_change(conn, "cfi", cfi_id, "signature", "on file" if cfi_row["signature"] else "",
                              "on file" if signature else "", session.get("user_name"))
        _log_field_change(conn, "cfi", cfi_id, "medical_expires", cfi_row["medical_expires"], medical_expires, session.get("user_name"))
        conn.commit()
        med_now = _medical_status(conn.execute("SELECT medical_class, medical_expires FROM cfis WHERE id = ?",
                                               (cfi_id,)).fetchone())
        if med_now["ok"] is False:
            upcoming = conn.execute("""SELECT COUNT(*) c FROM scheduled_flights WHERE cfi_id = ? AND status = 'scheduled'
                                       AND scheduled_date > ?""", (cfi_id, medical_expires)).fetchone()["c"]
            if upcoming:
                flash(f"Heads up: {name} has {upcoming} booking{'s' if upcoming != 1 else ''} after their medical "
                      f"expired ({_us_date(medical_expires)}) - they can't be started until the medical is updated.", "warning")
        # Keep the master login account (the actual place passwords/active/
        # can_bill are checked) in step with this edit.
        if cfi_row["user_id"]:
            if new_password:
                conn.execute("UPDATE users SET name=?, active=?, can_bill=?, password_hash=?, password_plain=? WHERE id=?",
                             (name, active, can_bill, generate_password_hash(new_password, method="pbkdf2:sha256"),
                              new_password, cfi_row["user_id"]))
            else:
                conn.execute("UPDATE users SET name=?, active=?, can_bill=? WHERE id=?",
                             (name, active, can_bill, cfi_row["user_id"]))
            conn.commit()
        conn.close()
        flash("CFI updated.", "success")
        return redirect(url_for("flight.cfis_list"))
    used_colors = _used_cfi_colors(conn, exclude_cfi_id=cfi_id)
    conn.close()
    return render_template("flight/cfi_form.html", medical_classes=MEDICAL_CLASSES, cfi=cfi_row, cfi_user=user_row,
                           used_colors=used_colors, instructor_colors=SCHEDULE_COLORS)


@flight_bp.route("/cfis/me", methods=["GET", "POST"])
@cfi_required
def cfi_me():
    """A CFI's own profile - the only CFI profile a non-admin can see (the
    CFIs list and Edit CFI are master-admin only; students can't reach any
    of them). Read-only except their instructor certificate # and
    expiration, which print on students' logbook entries. Master admins
    edit everything else from Manage > CFIs."""
    conn = get_db()
    cfi_row = conn.execute("SELECT * FROM cfis WHERE id = ?", (session["cfi_id"],)).fetchone()
    if not cfi_row:
        conn.close()
        flash("No CFI profile is linked to your login.", "danger")
        return redirect(url_for("flight.dashboard"))
    if request.method == "POST":
        number, expires = _cfi_cert_from_form(request.form)
        conn.execute("UPDATE cfis SET cfi_cert_number = ?, cfi_cert_expires = ? WHERE id = ?",
                     (number, expires, cfi_row["id"]))
        sig_changed, signature = _signature_from_form(request.form)
        if sig_changed:
            _save_cfi_signature(conn, cfi_row["id"], signature)
            _log_field_change(conn, "cfi", cfi_row["id"], "signature", "on file" if cfi_row["signature"] else "",
                              "on file" if signature else "", session.get("user_name"))
        _log_field_change(conn, "cfi", cfi_row["id"], "cfi_cert_number", cfi_row["cfi_cert_number"], number,
                          session.get("user_name"))
        _log_field_change(conn, "cfi", cfi_row["id"], "cfi_cert_expires", cfi_row["cfi_cert_expires"], expires,
                          session.get("user_name"))
        conn.commit()
        conn.close()
        flash("Signature and certificate details saved." if sig_changed and signature else
              ("Signature removed." if sig_changed else "Certificate details saved."), "success")
        return redirect(url_for("flight.cfi_me"))
    conn.close()
    return render_template("flight/cfi_me.html", cfi=cfi_row, credentials=CFI_CREDENTIALS,
                           medical_labels=dict(MEDICAL_CLASSES))


@flight_bp.route("/cfis/<int:cfi_id>/pay")
@login_required
def cfi_pay(cfi_id):
    """Auto-tallied hours/amount owed to this CFI, from their logged
    (non-solo) flights x their pay rate (separate from what students are
    billed) - clickable breakdown per flight. Visible only to a master
    admin or the CFI themselves."""
    cfi_row = None
    conn = get_db()
    cfi_row = conn.execute("SELECT * FROM cfis WHERE id = ?", (cfi_id,)).fetchone()
    if not cfi_row:
        conn.close()
        flash("CFI not found.", "danger")
        return redirect(url_for("flight.dashboard"))
    is_self = session.get("cfi_id") == cfi_id
    if not (session.get("is_master_admin") or is_self):
        conn.close()
        flash("You can only view your own pay.", "danger")
        return redirect(url_for("flight.dashboard"))
    rows = conn.execute(_LOG_ROW_SQL + " WHERE f.cfi_id = ? AND f.solo = 0 ORDER BY f.flight_date DESC, f.id DESC",
                        (cfi_id,)).fetchall()
    conn.close()
    pay_rate = cfi_row["pay_rate_per_hour"] or 0
    breakdown = []
    total_hours = 0.0
    total_owed = 0.0
    for r in rows:
        d = _row_with_cost(r)
        hrs = (d["instructor_hours"] or 0) + (d["ground_hours"] or 0)
        amount = hrs * pay_rate
        total_hours += hrs
        total_owed += amount
        breakdown.append(dict(d, pay_hours=hrs, pay_amount=amount))
    return render_template("flight/cfi_pay.html", cfi=cfi_row, breakdown=breakdown,
                           total_hours=total_hours, total_owed=total_owed, pay_rate=pay_rate)


# The My Schedule timeline's window - 6am-9pm covers virtually every real
# lesson, and clamping to it keeps one early/late outlier from squashing
# every other day's bar down to a sliver.
_CFI_SCHEDULE_DAY_START = 6 * 60
_CFI_SCHEDULE_DAY_END = 21 * 60


def _time_off_label(row):
    """'All day', '9:00 AM - 3:00 PM', or '9:00 AM onward' for a cfi_time_off
    row, with '- every <weekday>' appended for a recurring one."""
    if not row["start_time"]:
        label = "All day"
    else:
        start = _format_time_12h(row["start_time"])
        label = f"{start} - {_format_time_12h(row['end_time'])}" if row["end_time"] else f"{start} onward"
    if row["recurs_weekly"] if "recurs_weekly" in row.keys() else False:
        weekday_name = datetime.strptime(row["off_date"], "%Y-%m-%d").strftime("%A")
        label += f" - every {weekday_name}"
    return label


def _cfi_schedule_pct(start_min, end_min):
    span = _CFI_SCHEDULE_DAY_END - _CFI_SCHEDULE_DAY_START
    left = max(0, min(100, (start_min - _CFI_SCHEDULE_DAY_START) / span * 100))
    right = max(0, min(100, (end_min - _CFI_SCHEDULE_DAY_START) / span * 100))
    return left, max(right - left, 0.5)


def _cfi_schedule_week(conn, cfi_id, week_dates):
    """One row per day of the week for the My Schedule page: the CFI's own
    booked span (first flight start to last flight end) with a colored
    timeline bar - blue for booked, amber for an unassigned gap between two
    bookings, red for time off - built from _CFI_SCHEDULE_DAY_START/END.
    Doesn't include flight/student/plane details, just the shape of the
    day."""
    date_strs = [d.strftime("%Y-%m-%d") for d in week_dates]
    flights = conn.execute(
        """SELECT scheduled_date, scheduled_time, duration_hours FROM scheduled_flights
           WHERE cfi_id = ? AND status = 'scheduled' AND scheduled_date BETWEEN ? AND ?
           ORDER BY scheduled_date, scheduled_time""",
        (cfi_id, date_strs[0], date_strs[-1])).fetchall()
    time_off = conn.execute(
        """SELECT * FROM cfi_time_off WHERE cfi_id = ? AND recurs_weekly = 0 AND off_date BETWEEN ? AND ?
           ORDER BY off_date, start_time""",
        (cfi_id, date_strs[0], date_strs[-1])).fetchall()
    # A recurring entry's anchor off_date can be any date on or before this
    # week that falls on the repeated weekday, so it's matched by weekday
    # below instead of a plain date-range query.
    recurring_time_off = conn.execute(
        """SELECT * FROM cfi_time_off WHERE cfi_id = ? AND recurs_weekly = 1 AND off_date <= ?
           ORDER BY start_time""",
        (cfi_id, date_strs[-1])).fetchall()

    today_str = date.today().strftime("%Y-%m-%d")
    days = []
    for ds, d in zip(date_strs, week_dates):
        day_off = [t for t in time_off if t["off_date"] == ds]
        day_off += [t for t in recurring_time_off
                    if datetime.strptime(t["off_date"], "%Y-%m-%d").weekday() == d.weekday()]
        all_day_off = any(not t["start_time"] for t in day_off)
        windows = sorted(_time_window(f["scheduled_time"], f["duration_hours"])
                         for f in flights if f["scheduled_date"] == ds
                         and _time_window(f["scheduled_time"], f["duration_hours"]))
        segments = []
        on_start = on_end = None
        if not all_day_off and windows:
            merged = [list(windows[0])]
            for w in windows[1:]:
                if w[0] <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], w[1])
                else:
                    merged.append(list(w))
            on_start, on_end = merged[0][0], merged[-1][1]
            cursor = merged[0][0]
            for seg_start, seg_end in merged:
                if seg_start > cursor:
                    left, width = _cfi_schedule_pct(cursor, seg_start)
                    segments.append({"kind": "gap", "left": left, "width": width})
                left, width = _cfi_schedule_pct(seg_start, seg_end)
                segments.append({"kind": "flight", "left": left, "width": width})
                cursor = seg_end
        for t in day_off:
            if t["start_time"]:
                start_h, start_m = (int(x) for x in t["start_time"].split(":"))
                if t["end_time"]:
                    end_h, end_m = (int(x) for x in t["end_time"].split(":"))
                    end_min = end_h * 60 + end_m
                else:
                    end_min = _CFI_SCHEDULE_DAY_END
                left, width = _cfi_schedule_pct(start_h * 60 + start_m, end_min)
                segments.append({"kind": "off", "left": left, "width": width})
        days.append({
            "date": ds, "date_label": f"{d.strftime('%a')} {d.month}/{d.day}",
            "is_today": ds == today_str,
            "on_label": (f"{_format_time_12h(f'{on_start // 60:02d}:{on_start % 60:02d}')} - "
                        f"{_format_time_12h(f'{on_end // 60:02d}:{on_end % 60:02d}')}") if on_start is not None else None,
            "all_day_off": all_day_off,
            "segments": segments,
            "time_off": [dict(t, when_label=_time_off_label(t)) for t in day_off],
        })
    return days


@flight_bp.route("/cfis/schedule", methods=["GET", "POST"])
@cfi_required
def cfi_schedule():
    """A CFI's own "My Schedule" tab: a week-at-a-glance of when they're
    booked (not what/who - just the shape of the day, see
    _cfi_schedule_week) plus a place to put in their own time off, which
    then blocks new bookings over it (see _cfi_time_off_conflict, checked
    everywhere _scheduling_conflicts is)."""
    conn = get_db()
    cfi_id = session["cfi_id"]
    if request.method == "POST":
        off_date = request.form.get("off_date", "").strip()
        start_time = request.form.get("start_time", "").strip()
        end_time = request.form.get("end_time", "").strip()
        note = request.form.get("note", "").strip()
        recurs_weekly = 1 if request.form.get("recurs_weekly") else 0
        anchor_date = request.form.get("view_date", "").strip()
        if not off_date:
            flash("Pick a date for the time off.", "danger")
        elif start_time and end_time and end_time <= start_time:
            flash("End time has to be after start time.", "danger")
        else:
            conn.execute("INSERT INTO cfi_time_off (cfi_id, off_date, start_time, end_time, note, recurs_weekly) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (cfi_id, off_date, start_time or None, end_time or None, note or None, recurs_weekly))
            conn.commit()
            weekday_name = datetime.strptime(off_date, "%Y-%m-%d").strftime("%A")
            msg = (f"Time off added for every {weekday_name} - it now blocks new bookings for you on that day going forward."
                   if recurs_weekly else "Time off added - it now blocks new bookings for you over that time.")
            flash(msg, "success")
        conn.close()
        return redirect(url_for("flight.cfi_schedule", date=anchor_date or off_date))

    today = date.today()
    day_str = request.args.get("date", "").strip() or today.strftime("%Y-%m-%d")
    try:
        day_date = datetime.strptime(day_str, "%Y-%m-%d").date()
    except ValueError:
        day_date = today
        day_str = today.strftime("%Y-%m-%d")
    week_start = day_date - timedelta(days=day_date.weekday())
    week_dates = [week_start + timedelta(days=i) for i in range(7)]

    days = _cfi_schedule_week(conn, cfi_id, week_dates)
    # A recurring entry stays "upcoming" forever (its anchor off_date can be
    # long past while it still blocks every future occurrence of that
    # weekday), so it's included regardless of date.
    upcoming_time_off = [dict(t, when_label=_time_off_label(t)) for t in conn.execute(
        "SELECT * FROM cfi_time_off WHERE cfi_id = ? AND (off_date >= ? OR recurs_weekly = 1) "
        "ORDER BY recurs_weekly DESC, off_date, start_time",
        (cfi_id, today.strftime("%Y-%m-%d"))).fetchall()]
    conn.close()
    return render_template("flight/cfi_schedule.html", days=days, day_str=day_str,
                           week_label=f"Week of {week_dates[0].month}/{week_dates[0].day}",
                           week_prev=(week_start - timedelta(days=7)).strftime("%Y-%m-%d"),
                           week_next=(week_start + timedelta(days=7)).strftime("%Y-%m-%d"),
                           upcoming_time_off=upcoming_time_off, today_str=today.strftime("%Y-%m-%d"))


@flight_bp.route("/cfis/schedule/time-off/<int:off_id>/delete", methods=["POST"])
@cfi_required
def cfi_time_off_delete(off_id):
    """Removes one of the current CFI's own time-off entries - CFIs can
    only delete their own (no admin override needed; it's just their
    personal availability, same trust level as picking their own
    signature)."""
    conn = get_db()
    row = conn.execute("SELECT * FROM cfi_time_off WHERE id = ?", (off_id,)).fetchone()
    if not row or row["cfi_id"] != session["cfi_id"]:
        conn.close()
        flash("Time off not found.", "danger")
        return redirect(url_for("flight.cfi_schedule"))
    conn.execute("DELETE FROM cfi_time_off WHERE id = ?", (off_id,))
    conn.commit()
    conn.close()
    flash("Time off removed.", "success")
    return redirect(url_for("flight.cfi_schedule", date=request.form.get("view_date", "")))


@flight_bp.route("/planes")
@cfi_required
def planes_list():
    conn = get_db()
    planes = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()
    conn.close()
    return render_template("flight/planes.html", planes=planes)


@flight_bp.route("/planes/simulator/new", methods=["GET", "POST"])
@admin_required
def simulator_new():
    """Adds a flight simulator to the Planes list - a lighter-weight path
    than the Maintenance tile's full aircraft form (no Hobbs/Tach, engine,
    prop, or maintenance items to fill in for something that isn't a real
    airplane). Still an is_flight_asset row so it shows up for booking and
    logging the same as any plane; is_simulator just tells the Planes list
    and schedule to skip the meter columns for it."""
    if request.method == "POST":
        tag = request.form.get("tag", "").strip()
        name = request.form.get("name", "").strip()
        sim_rate = _parse_float(request.form.get("sim_rate"))
        if not tag:
            flash("Give the simulator an ID (e.g. \"SIM1\").", "danger")
            return render_template("flight/simulator_form.html")
        conn = get_db()
        existing = conn.execute("SELECT id FROM assets WHERE tag = ?", (tag,)).fetchone()
        if existing:
            conn.close()
            flash(f"An asset with tag '{tag}' already exists.", "danger")
            return render_template("flight/simulator_form.html")
        conn.execute("""INSERT INTO assets (tag, name, is_flight_asset, is_simulator, sim_rate, created_at, updated_at)
                         VALUES (?, ?, 1, 1, ?, ?, ?)""",
                     (tag, name or tag, sim_rate, now_iso(), now_iso()))
        conn.commit()
        conn.close()
        flash(f"Simulator '{tag}' added.", "success")
        return redirect(url_for("flight.planes_list"))
    return render_template("flight/simulator_form.html")


@flight_bp.route("/planes/simulator/<int:asset_id>/edit", methods=["GET", "POST"])
@admin_required
def simulator_edit(asset_id):
    """Editing a simulator's own lightweight profile (tag, name, rate) -
    kept separate from the Maintenance tile's full aircraft edit form
    (asset_edit) since a sim has none of that form's fields and shouldn't
    route an admin over to the Maintenance side at all."""
    conn = get_db()
    sim = conn.execute("SELECT * FROM assets WHERE id = ? AND is_simulator = 1", (asset_id,)).fetchone()
    if not sim:
        conn.close()
        flash("Simulator not found.", "danger")
        return redirect(url_for("flight.planes_list"))
    if request.method == "POST":
        tag = request.form.get("tag", "").strip()
        name = request.form.get("name", "").strip()
        sim_rate = _parse_float(request.form.get("sim_rate"))
        if not tag:
            conn.close()
            flash("Give the simulator an ID (e.g. \"SIM1\").", "danger")
            return render_template("flight/simulator_form.html", sim=sim)
        clash = conn.execute("SELECT id FROM assets WHERE tag = ? AND id != ?", (tag, asset_id)).fetchone()
        if clash:
            conn.close()
            flash(f"An asset with tag '{tag}' already exists.", "danger")
            return render_template("flight/simulator_form.html", sim=sim)
        conn.execute("UPDATE assets SET tag = ?, name = ?, sim_rate = ?, updated_at = ? WHERE id = ?",
                     (tag, name or tag, sim_rate, now_iso(), asset_id))
        conn.commit()
        conn.close()
        flash(f"Simulator '{tag}' updated.", "success")
        return redirect(url_for("flight.planes_list"))
    conn.close()
    return render_template("flight/simulator_form.html", sim=sim)


@flight_bp.route("/planes/<int:asset_id>/rate", methods=["GET", "POST"])
@admin_required
def plane_rate_edit(asset_id):
    """A plane's Schedule color, set here by an admin on the Flight School
    side. Planes no longer have a rental rate - what a student pays for the
    plane is their own Plane Rate on the Students page."""
    conn = get_db()
    plane = conn.execute(
        "SELECT * FROM assets WHERE id = ? AND deleted_at IS NULL AND is_flight_asset = 1", (asset_id,)
    ).fetchone()
    if not plane:
        conn.close()
        flash("Plane not found.", "danger")
        return redirect(url_for("flight.planes_list"))
    if request.method == "POST":
        color = request.form.get("color", "").strip() or None
        if color and color not in SCHEDULE_COLORS:
            color = None
        if color and color in _used_colors_for_plane(conn, asset_id):
            flash("That color is already taken by an instructor or another plane - pick a different one.", "danger")
            conn.close()
            return redirect(url_for("flight.plane_rate_edit", asset_id=asset_id))
        solo_color = request.form.get("solo_color", "").strip() or None
        if solo_color and solo_color not in SCHEDULE_COLORS:
            solo_color = None
        if solo_color and solo_color in _used_solo_colors_for_plane(conn, asset_id):
            flash("That Solo Color is already taken by another plane - pick a different one.", "danger")
            conn.close()
            return redirect(url_for("flight.plane_rate_edit", asset_id=asset_id))
        conn.execute("UPDATE assets SET schedule_color = ?, solo_color = ?, updated_at = ? WHERE id = ?",
                     (color, solo_color, now_iso(), asset_id))
        conn.commit()
        conn.close()
        flash(f"Color updated for {plane['tag']}.", "success")
        return redirect(url_for("flight.planes_list"))
    used_colors = _used_colors_for_plane(conn, asset_id)
    used_solo_colors = _used_solo_colors_for_plane(conn, asset_id)
    conn.close()
    return render_template("flight/plane_rate_form.html", plane=plane, used_colors=used_colors,
                           used_solo_colors=used_solo_colors, schedule_colors=SCHEDULE_COLORS)


_SCHEDULE_ROW_SQL = """SELECT sf.*, a.tag as plane_tag, a.name as plane_name, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name,
                     s.is_station as student_is_station, s.pilot_certificate as pilot_certificate,
                     s.pilot_ratings as pilot_ratings, s.first_solo_date as first_solo_date,
                     c.name as cfi_name, c.color as cfi_color,
                     a.schedule_color as plane_color, a.solo_color as plane_solo_color
              FROM scheduled_flights sf
              JOIN assets a ON a.id = sf.asset_id
              JOIN students s ON s.id = sf.student_id
              LEFT JOIN cfis c ON c.id = sf.cfi_id
              WHERE sf.status IN ('scheduled', 'in_progress', 'completed')"""


def _note_visibility(r):
    """Who can see this booking's two note fields. Notes (the plain one)
    shows only to admin and the specific CFI on this booking - not the
    student, and not a different CFI just browsing the schedule.
    private_notes still shows to every CFI and admin same as before, plus
    now also the specific student on this booking (never a different
    student)."""
    is_admin = bool(session.get("is_master_admin"))
    my_cfi_id = session.get("cfi_id")
    my_student_id = session.get("student_id")
    notes_visible = is_admin or bool(my_cfi_id and my_cfi_id == r["cfi_id"])
    private_notes_visible = is_admin or bool(my_cfi_id) or bool(my_student_id and my_student_id == r["student_id"])
    return notes_visible, private_notes_visible


def _decorate_schedule_row(r):
    """Common per-row computed fields (instructor color, 12h time labels)
    shared by every schedule view - day/month/quarter/year/list/custom all
    build on this so a booking looks/behaves identically no matter which
    view it's shown in."""
    r = dict(r)
    r["notes_visible"], r["private_notes_visible"] = _note_visibility(r)
    # Two colors per booking: the plane's (block background) and the
    # instructor's (thick left stripe). A solo booking shows the plane's
    # solo color if one's been picked (Planes > Edit), otherwise the plane's
    # color in neon, with no instructor stripe.
    r["cfi_display_color"] = _cfi_display_color(r["cfi_name"], r["cfi_color"]) if r["cfi_id"] else None
    base_plane = r.get("plane_color") or PLANE_FALLBACK_COLOR
    r["plane_display_color"] = (r.get("plane_solo_color") or _neon_color(base_plane)) if r["solo"] else base_plane
    r["display_color"] = r["plane_display_color"]
    r["display_text_color"] = _text_on(r["display_color"])
    r["time_label"] = _format_time_12h(r["scheduled_time"])
    # Short form for the calendar grid blocks: "2:30 PM" -> "2:30p".
    r["short_time"] = (r["time_label"].replace(" AM", "a").replace(" PM", "p")
                       if r["time_label"] else None)
    window = _time_window(r["scheduled_time"], r["duration_hours"])
    r["end_time_label"] = _format_time_12h(f"{window[1] // 60:02d}:{window[1] % 60:02d}") if window and r["scheduled_time"] else None
    # Whether this flight's needs_review flag is still an "active" alert
    # (unacknowledged) - the warning-triangle icon and red outline on the
    # calendar/dashboard only show for this, not for a flight that's still
    # technically flagged but has already been acknowledged from the
    # dashboard's Needs Review banner (see schedule_review_acknowledge()).
    r["needs_review_active"] = bool(r["needs_review"]) and not r["needs_review_acknowledged_at"]
    # The caution triangle itself stays on until the issue is actually
    # resolved (acknowledged or not) - see _needs_review_flag().
    r["needs_review_flag"] = _needs_review_flag(r)
    # A still-pending self-service request - not a real booking yet, so the
    # calendar views (include_pending=True below) draw it as a 50%-see-
    # through, dashed-outline box rather than a normal solid block. Plane
    # availability (_build_availability_day) never sees these at all - a
    # request awaiting approval must not block someone else's slot.
    r["is_pending_approval"] = r["status"] == "pending_approval"
    return r


def _schedule_rows(conn, date_from, date_to, plane_id=None, cfi_id=None, include_pending=False):
    """Every scheduled_flights row (decorated) between two dates inclusive,
    optionally narrowed to one plane/instructor - the shared fetch behind
    every schedule view. include_pending also pulls in still-pending
    self-service requests (status='pending_approval'), for the Schedule
    calendar's own views only - see is_pending_approval above."""
    sql = _SCHEDULE_ROW_SQL
    if include_pending:
        sql = sql.replace("WHERE sf.status IN ('scheduled', 'in_progress', 'completed')",
                           "WHERE sf.status IN ('scheduled', 'in_progress', 'completed', 'pending_approval')")
    sql += " AND sf.scheduled_date BETWEEN ? AND ?"
    params = [date_from, date_to]
    if plane_id:
        sql += " AND sf.asset_id = ?"
        params.append(plane_id)
    if cfi_id:
        sql += " AND sf.cfi_id = ?"
        params.append(cfi_id)
    sql += " ORDER BY sf.scheduled_date, sf.scheduled_time IS NULL, sf.scheduled_time"
    rows = conn.execute(sql, params).fetchall()
    # Bookings a late flight's updated ETA runs into (see _eta_impacts) -
    # an hourglass icon + amber outline on the block.
    delays = _eta_impacts(conn)
    out = []
    for r in rows:
        d = _decorate_schedule_row(r)
        d["eta_delay"] = delays.get(d["id"])
        out.append(d)
    return out


def _build_schedule_month(conn, year, month, plane_id=None, cfi_id=None, plane_order=None):
    """One month's schedule grid: weeks of day-numbers plus a by_day map of
    that day's scheduled flights, each colored by instructor and labeled
    with its plane and time so it's identifiable at a glance either way.
    Flights that have since started or completed stay on the calendar
    (greyed out in the template) rather than disappearing - only cancelled
    ones drop off, so the day still shows what actually happened there.

    plane_order, when given (only the real Month view passes it - Quarter
    and Year stay on the older, more compact rendering), also computes
    by_day_layout: each day's timed flights laid out via
    _layout_month_cell_timeline so the cell can grow to fit everything and
    same-time bookings sit side by side instead of piling up."""
    first_weekday, days_in_month = calendar_mod.monthrange(year, month)
    month_start = f"{year:04d}-{month:02d}-01"
    month_end = f"{year:04d}-{month:02d}-{days_in_month:02d}"

    by_day = {d: [] for d in range(1, days_in_month + 1)}
    for r in _schedule_rows(conn, month_start, month_end, plane_id, cfi_id, include_pending=True):
        day = int(r["scheduled_date"][8:10])
        by_day.setdefault(day, []).append(r)

    by_day_layout = {}
    if plane_order is not None:
        for day, flights in by_day.items():
            timed = [f for f in flights if f["scheduled_time"]]
            untimed = [f for f in flights if not f["scheduled_time"]]
            placements, container_h = _layout_month_cell_timeline(timed, plane_order)
            by_day_layout[day] = {"placements": placements, "container_h": container_h, "untimed": untimed}

    weeks = []
    week = [None] * first_weekday
    for d in range(1, days_in_month + 1):
        week.append(d)
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        week += [None] * (7 - len(week))
        weeks.append(week)

    return {"year": year, "month": month, "month_name": calendar_mod.month_name[month],
            "weeks": weeks, "by_day": by_day, "by_day_layout": by_day_layout}


# The Month view's mini per-day timeline: same 6am-8pm reference window as
# before, but now a fixed PIXEL scale (not a percentage of a capped-height
# box) so a day cell's real height can grow to fit whatever it actually
# needs instead of silently clipping content - see _layout_month_cell_timeline.
# 0.25 px/min matches the ~210px cell height already in use for a normal
# (non-packed) day, so a typical day looks the same as before; a packed one
# just grows a bit past that instead of hiding blocks behind "+N more".
MONTH_TIMELINE_WINDOW_START = 6 * 60
MONTH_TIMELINE_WINDOW_END = 20 * 60
MONTH_TIMELINE_PX_PER_MIN = 0.45  # was 0.25 - bigger blocks with room for 3 lines (see MONTH_BLOCK_PX_PER_MIN)
MONTH_TIMELINE_BLOCK_PX = 30  # approx rendered height of one (2-line) block
# Month blocks are sized by the booking's length on the same px-per-minute
# scale as the cell's time axis (a standard 1.5-hr block = 40px with room
# for 3 lines, 1 hr = 27px, 2 hr = 54px), never shorter than
# MONTH_BLOCK_MIN_PX so a short one still shows its time and plane.
MONTH_BLOCK_PX_PER_MIN = MONTH_TIMELINE_PX_PER_MIN  # same scale as the time axis, so back-to-back bookings touch instead of overlapping
MONTH_BLOCK_MIN_PX = 22


def _layout_month_cell_timeline(flights, plane_order,
                                 window_start=MONTH_TIMELINE_WINDOW_START, window_end=MONTH_TIMELINE_WINDOW_END,
                                 px_per_min=MONTH_TIMELINE_PX_PER_MIN, block_px=MONTH_TIMELINE_BLOCK_PX):
    """Positions one day cell's timed flights on its to-scale mini timeline
    using a fixed lane per plane, not a per-collision cluster.

    Every plane with at least one booking that day gets its own vertical
    lane (equal width, touching the next one), ordered left-to-right by
    plane_order - the same alphabetical-by-tag rank as the Planes legend -
    so a plane is ALWAYS in the same lane relative to another plane
    whenever both are booked that day, in that order, day after day
    (267 then Rosie then Redbird then Comanche, say), never shuffling.
    This is a stronger guarantee than only keeping order within a single
    time collision: two bookings for the same two planes land in the same
    left/right relationship even when their times don't overlap at all.

    Each booking is placed at its start time within its plane's lane, and
    its height follows its length (MONTH_BLOCK_PX_PER_MIN; no booking
    length = the standard 1.5-hr block), so a longer booking reads as a
    bigger block. A plane double-booked the same day (rare) stacks its own
    bookings vertically within its own lane by start time rather than
    overlapping.

    plane_order is a {plane_tag: rank} map (see _schedule_calendar_context).

    Returns (placements, container_height_px) where placements is a list
    of (flight, {top_px, height_px, left_pct, width_pct})."""
    base_height = (window_end - window_start) * px_per_min
    if not flights:
        return [], round(base_height + 4, 1)

    # Lanes: every plane booked today, in stable global rank order - not
    # just the planes involved in one time collision.
    tags_today = sorted({f["plane_tag"] for f in flights}, key=lambda t: plane_order.get(t, 999))
    lane_index = {tag: i for i, tag in enumerate(tags_today)}
    col_count = len(tags_today)
    width_pct = 100.0 / col_count

    by_lane = {tag: [] for tag in tags_today}
    for f in flights:
        mins = int(f["scheduled_time"][:2]) * 60 + int(f["scheduled_time"][3:5])
        clamped = min(max(mins, window_start), window_end)
        by_lane[f["plane_tag"]].append(((clamped - window_start) * px_per_min, f))

    placements = []
    max_bottom = 0.0
    for tag in tags_today:
        items = sorted(by_lane[tag], key=lambda t: t[0])
        claimed_bottom = None
        prev_style = None
        for top, f in items:
            # Rounded corners (see .month-lane-block CSS) look fine on a
            # block with real space above/below it, but on two bookings
            # that butt up against each other with no actual time gap, the
            # curve on each touching edge leaves a sliver of the cell
            # background showing through - reading as a gap that isn't
            # really there. touches_prev/touches_next (below) tell the
            # template to square off just that shared edge so back-to-back
            # bookings read as touching. A small tolerance absorbs the
            # independent px rounding on top_px vs the previous claimed_bottom.
            touches_prev = claimed_bottom is not None and top <= claimed_bottom + 0.5
            if claimed_bottom is not None and top < claimed_bottom:
                top = claimed_bottom
            dur_hours = f["duration_hours"] if f["duration_hours"] else DEFAULT_SCHEDULE_BLOCK_HOURS
            height = max(MONTH_BLOCK_MIN_PX, round(dur_hours * 60 * MONTH_BLOCK_PX_PER_MIN, 1))
            style = {"top_px": round(top, 1), "height_px": height,
                     "left_pct": round(lane_index[tag] * width_pct, 2),
                     "width_pct": round(width_pct, 2),
                     "touches_prev": touches_prev, "touches_next": False}
            if touches_prev and prev_style is not None:
                prev_style["touches_next"] = True
            placements.append((f, style))
            prev_style = style
            claimed_bottom = top + height
            max_bottom = max(max_bottom, top + height)
    container_h = round(max(max_bottom, base_height) + 4, 1)
    return placements, container_h


def _build_schedule_day(conn, date_str, plane_id=None, cfi_id=None):
    """A single day's flights, sorted by time - the Day view."""
    return _schedule_rows(conn, date_str, date_str, plane_id, cfi_id, include_pending=True)


# The Availability view's window: 8am-8pm sliced into 1.5-hour slots - the
# same length as a normal booking's default duration (see
# DEFAULT_SCHEDULE_BLOCK_HOURS above), so a slot lines up with how a
# booking actually blocks the plane. 720 minutes / 90 = 8 whole slots, no
# partial slot at the end.
AVAILABILITY_START_MIN = 8 * 60
AVAILABILITY_END_MIN = 20 * 60
AVAILABILITY_SLOT_MIN = int(DEFAULT_SCHEDULE_BLOCK_HOURS * 60)


def _build_availability_day(conn, date_str, plane_id=None):
    """Plane availability for one day: every active plane (or just one,
    when plane_id narrows it) x every 8am-8pm slot, each marked booked or
    open. This is PLANE availability only - a slot only looks at whether
    that plane itself is tied up, not whether an instructor is also free -
    using the same overlap check schedule_new() uses to flag a
    double-booking (_time_window/_windows_overlap), against that plane's
    scheduled/in_progress/completed flights that day (see _SCHEDULE_ROW_SQL
    - cancelled and not-yet-approved bookings don't block a slot).

    Returns a dict with:
      planes: the plane rows being shown
      slots: [{start_min, end_min, label}, ...] - the day's slot headers
      grid: [{plane, cells: [{start_min, label, booked, flight}, ...]}, ...]
            - one row per plane, in the same order as `planes`
      open_list: flat [{plane_tag, plane_id, label, start_min}, ...] of
                 every open (plane, slot) pair, for the List sub-view."""
    if plane_id:
        planes = conn.execute(
            "SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 AND id = ? ORDER BY tag",
            (plane_id,)).fetchall()
    else:
        planes = conn.execute(
            "SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()

    day_flights = _schedule_rows(conn, date_str, date_str, plane_id or None)
    flights_by_asset = {}
    for f in day_flights:
        flights_by_asset.setdefault(f["asset_id"], []).append(f)

    # A slot that's already started (or, for a wholly past day, every slot
    # on it) is still technically "open" in the sense that nothing's booked
    # there, but booking it would fail the server's own past-booking check
    # anyway - see is_past below, greyed out and unclickable in the
    # template instead of looking like a real option.
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    now_min = now.hour * 60 + now.minute

    slots = []
    for start in range(AVAILABILITY_START_MIN, AVAILABILITY_END_MIN, AVAILABILITY_SLOT_MIN):
        end = start + AVAILABILITY_SLOT_MIN
        label = _format_time_12h(f"{start // 60:02d}:{start % 60:02d}")
        is_past = date_str < today_str or (date_str == today_str and start <= now_min)
        slots.append({"start_min": start, "end_min": end, "label": label, "is_past": is_past})

    grid = []
    open_list = []
    for p in planes:
        cells = []
        for slot in slots:
            window = (slot["start_min"], slot["end_min"])
            booked_flight = None
            for f in flights_by_asset.get(p["id"], []):
                if _windows_overlap(window, _time_window(f["scheduled_time"], f["duration_hours"])):
                    booked_flight = f
                    break
            cells.append({"start_min": slot["start_min"], "label": slot["label"], "is_past": slot["is_past"],
                          "booked": booked_flight is not None, "flight": booked_flight})
            if booked_flight is None:
                open_list.append({"plane_tag": p["tag"], "plane_id": p["id"],
                                  "label": slot["label"], "start_min": slot["start_min"], "is_past": slot["is_past"]})
        grid.append({"plane": p, "cells": cells})

    return {"planes": planes, "slots": slots, "grid": grid, "open_list": open_list}


def _build_availability_counts(conn, date_strs, plane_id=None):
    """Open-slot counts for a list of dates - the Availability page's
    Week/Month sub-views show one number per day (how many (plane, slot)
    pairs are open that day) rather than the full Day view's grid, so a
    week or a month fits on screen; clicking a day jumps into the Day view
    for the real detail. Reuses _build_availability_day per date so the
    count always matches what the Day view would show for that same day.
    Already-past slots (see is_past in _build_availability_day) don't count
    as open - they're not something a student could actually book.
    Returns {date_str: open_count}."""
    return {d: sum(1 for o in _build_availability_day(conn, d, plane_id)["open_list"] if not o["is_past"])
            for d in date_strs}


# The flexible Flight Finder's time-of-day buckets, within the same 8am-8pm
# operating window as the plain Availability view above.
FINDER_TIME_BUCKETS = [
    ("morning", "Morning", 8 * 60, 11 * 60),
    ("afternoon", "Afternoon", 11 * 60, 15 * 60),
    ("evening", "Evening", 15 * 60, 20 * 60),
]


def _build_flight_finder(conn, start_date, num_days, day_types, buckets, plane_id=None, cfi_mode="any"):
    """The flexible Flight Finder (admin-only - see schedule_finder route):
    given a date range, which days of the week, which part of the day, and
    how flexible the plane/instructor can be, returns every open (date,
    slot) combination with the plane(s) and instructor(s) that are
    actually free then. This only searches and lists options - it doesn't
    book anything itself.

    day_types: subset of {'weekday', 'weekend'}.
    buckets: subset of FINDER_TIME_BUCKETS' codes ('morning'/'afternoon'/'evening').
    cfi_mode: 'any' (no preference - any active instructor), 'female' or
    'male' (any active instructor with that gender - see the optional
    Gender field on the CFI profile), 'solo' (no instructor needed - just
    plane availability), or a CFI id (as a string/int) for one specific
    instructor.

    Returns [{"date", "date_label", "slots": [{"start_min", "label",
    "planes": [...], "cfis": [...] or None}]}, ...] - only days that
    actually have at least one open slot are included.
    """
    if plane_id:
        planes = conn.execute(
            "SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 AND id = ? ORDER BY tag",
            (plane_id,)).fetchall()
    else:
        planes = conn.execute(
            "SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()

    need_cfi = cfi_mode != "solo"
    cfis = []
    if need_cfi:
        if cfi_mode == "any":
            cfis = conn.execute("SELECT * FROM cfis WHERE active = 1 AND is_station = 0 ORDER BY name").fetchall()
        elif cfi_mode == "female":
            cfis = conn.execute("SELECT * FROM cfis WHERE active = 1 AND is_station = 0 AND gender = 'F' ORDER BY name").fetchall()
        elif cfi_mode == "male":
            cfis = conn.execute("SELECT * FROM cfis WHERE active = 1 AND is_station = 0 AND gender = 'M' ORDER BY name").fetchall()
        else:
            cfis = conn.execute("SELECT * FROM cfis WHERE active = 1 AND is_station = 0 AND id = ? ORDER BY name", (cfi_mode,)).fetchall()

    end_date = start_date + timedelta(days=num_days - 1)
    day_flights = _schedule_rows(conn, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"),
                                  plane_id or None, None)
    flights_by_date_asset = {}
    flights_by_date_cfi = {}
    for f in day_flights:
        flights_by_date_asset.setdefault((f["scheduled_date"], f["asset_id"]), []).append(f)
        if f["cfi_id"]:
            flights_by_date_cfi.setdefault((f["scheduled_date"], f["cfi_id"]), []).append(f)

    active_buckets = [b for b in FINDER_TIME_BUCKETS if b[0] in buckets]
    days_out = []
    d = start_date
    while d <= end_date:
        is_weekend = d.weekday() >= 5  # Saturday=5, Sunday=6
        if (is_weekend and "weekend" not in day_types) or (not is_weekend and "weekday" not in day_types):
            d += timedelta(days=1)
            continue
        date_str = d.strftime("%Y-%m-%d")
        slots = []
        for _code, _label, bstart, bend in active_buckets:
            for start in range(bstart, bend, AVAILABILITY_SLOT_MIN):
                end = start + AVAILABILITY_SLOT_MIN
                window = (start, end)
                open_planes = [p for p in planes
                               if not any(_windows_overlap(window, _time_window(f["scheduled_time"], f["duration_hours"]))
                                          for f in flights_by_date_asset.get((date_str, p["id"]), []))]
                if not open_planes:
                    continue
                open_cfis = None
                if need_cfi:
                    open_cfis = [c for c in cfis
                                 if not any(_windows_overlap(window, _time_window(f["scheduled_time"], f["duration_hours"]))
                                            for f in flights_by_date_cfi.get((date_str, c["id"]), []))
                                 and not _cfi_medical_problem(conn, c["id"], date_str)]
                    if not open_cfis:
                        continue
                slots.append({"start_min": start, "label": _format_time_12h(f"{start // 60:02d}:{start % 60:02d}"),
                              "planes": open_planes, "cfis": open_cfis})
        if slots:
            days_out.append({"date": date_str, "date_label": f"{d.strftime('%a')} {d.month}/{d.day}", "slots": slots})
        d += timedelta(days=1)
    return days_out


# The Day view's hourly timeline spans 6 AM-9 PM (matches the flight
# school's actual operating hours - the old month-cell mini timeline this
# was modeled after used the same 6am-8pm reference window; widened an
# hour here since a dedicated Day view can afford to show a bit more).
DAY_TIMELINE_START_MIN = 6 * 60
DAY_TIMELINE_END_MIN = 21 * 60


def _layout_day_timeline(flights, window_start=DAY_TIMELINE_START_MIN, window_end=DAY_TIMELINE_END_MIN):
    """Positions each timed flight on a single vertical hour axis for the
    Day view (see the attached reference screenshot this was modeled
    after): top/height as a percent of the axis so a flight lands at its
    real time and is sized by its real duration, and left/width so flights
    that actually overlap in time sit side by side instead of stacking on
    top of each other - non-overlapping flights each still get the full
    width. Returns (timed, untimed): timed is [(flight, style_dict), ...]
    for flights with a start time (clamped into the visible window if they
    fall outside it, so nothing silently disappears); untimed is the plain
    list of flights with no time set at all, which don't belong on a time
    axis.

    Overlap layout is the standard two-pass "day calendar" algorithm:
    1) sweep by start time, growing a cluster's end as long as each next
       flight starts before the cluster's current end (a running "high
       water mark"), so only flights that are actually transitively
       connected by overlap land in the same cluster;
    2) within each cluster, greedily assign each flight to the
       lowest-numbered lane whose last occupant has already ended, giving
       every cluster its own column count - so a cluster of 3 overlapping
       flights gets 3 equal columns, while an unrelated flight elsewhere
       in the day still gets the full width.
    """
    timed = []
    untimed = []
    windows = []
    for f in flights:
        w = _time_window(f["scheduled_time"], f["duration_hours"])
        if not w:
            untimed.append(f)
            continue
        windows.append((w[0], w[1], f))
    windows.sort(key=lambda t: (t[0], t[1]))

    # Pass 1: cluster transitively-overlapping flights together.
    clusters = []  # list of lists of (start, end, flight)
    cluster_end = None
    for start, end, f in windows:
        if cluster_end is None or start >= cluster_end:
            clusters.append([])
            cluster_end = end
        else:
            cluster_end = max(cluster_end, end)
        clusters[-1].append((start, end, f))

    total_span = max(1, window_end - window_start)
    for cluster in clusters:
        # Pass 2: greedy lane assignment within this cluster only.
        lane_ends = []
        placements = []  # (lane, start, end, f)
        for start, end, f in cluster:
            lane = None
            for i, lane_end in enumerate(lane_ends):
                if start >= lane_end:
                    lane = i
                    lane_ends[i] = end
                    break
            if lane is None:
                lane = len(lane_ends)
                lane_ends.append(end)
            placements.append((lane, start, end, f))
        col_count = len(lane_ends)
        for lane, start, end, f in placements:
            clamped_start = min(max(start, window_start), window_end)
            clamped_end = min(max(end, window_start), window_end)
            if clamped_end <= clamped_start:
                clamped_end = min(clamped_start + 15, window_end)  # keep a sliver visible
            top_pct = (clamped_start - window_start) / total_span * 100
            height_pct = max((clamped_end - clamped_start) / total_span * 100, 2.0)  # readable minimum
            width_pct = 100.0 / col_count
            left_pct = lane * width_pct
            timed.append((f, {
                "top_pct": round(top_pct, 3), "height_pct": round(height_pct, 3),
                "left_pct": round(left_pct, 3), "width_pct": round(width_pct, 3),
            }))
    return timed, untimed


def _build_schedule_year_list(conn, year, plane_id=None, cfi_id=None):
    """Every flight in a year, grouped by month - the List view, same shape
    as the Maintenance calendar's List view. Also picks out which flight
    the page should open scrolled to (see list_scroll_anchor_id below) so
    a long year doesn't force scrolling from January just to reach what's
    still coming up."""
    rows = _schedule_rows(conn, f"{year:04d}-01-01", f"{year:04d}-12-31", plane_id, cfi_id, include_pending=True)
    by_month = {m: [] for m in range(1, 13)}
    for r in rows:
        by_month[int(r["scheduled_date"][5:7])].append(r)
    months = [{"month": m, "month_name": calendar_mod.month_name[m], "flights": by_month[m]}
              for m in range(1, 13)]

    # Anchor the list at the most recently completed flight (rows are
    # already in chronological order - see _schedule_rows) - upcoming
    # flights are what someone opening the schedule cares about most, so
    # they land just below the fold instead of needing a scroll past
    # everything already flown; older history is one scroll up instead
    # of the default. If nothing's been completed yet this year, anchor
    # at the first still-upcoming booking instead, so the page doesn't
    # just default back to the very top for no reason.
    anchor_id = None
    for r in rows:
        if r["status"] == "completed":
            anchor_id = r["id"]
    if anchor_id is None:
        today_str = date.today().strftime("%Y-%m-%d")
        for r in rows:
            if r["scheduled_date"] >= today_str:
                anchor_id = r["id"]
                break
    return months, anchor_id


def _schedule_calendar_context():
    """Builds the schedule calendar's template context (all views: Day,
    Month, Quarter, Year, List, Custom Range) from the current request's
    query args. Shared by schedule_calendar() (the full page) and
    schedule_live() (the live-refresh fragment, see below) so a TV/kiosk
    browser left open on the Schedule page - not just the Dashboard - picks
    up new/changed bookings without a manual reload."""
    conn = get_db()
    today = date.today()
    view = request.args.get("view", "month")
    if view not in ("day", "week", "month", "quarter", "year", "list", "custom"):
        view = "month"
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
    except ValueError:
        year, month = today.year, today.month
    if month < 1:
        month, year = 12, year - 1
    elif month > 12:
        month, year = 1, year + 1
    plane_id = request.args.get("plane_id", "").strip()
    cfi_id = request.args.get("cfi_id", "").strip()
    day_str = request.args.get("date", "").strip() or today.strftime("%Y-%m-%d")
    custom_start = request.args.get("start", "").strip()
    custom_end = request.args.get("end", "").strip()
    # A scheduling conflict can redirect here with the specific booking(s)
    # it collided with called out - those blocks/rows render with a red
    # outline front and center, everything else on the page faded back.
    highlight_ids = set()
    for x in request.args.get("highlight", "").split(","):
        x = x.strip()
        if x.isdigit():
            highlight_ids.add(int(x))
    # Just-booked flight(s): after Schedule Flight the page opens on the
    # Month view with the new booking(s) outlined in green (?new=ids). The
    # page's script drops the outline (and the param) on the first click or
    # when you leave, so it only greets you once.
    new_ids = set()
    for x in request.args.get("new", "").split(","):
        x = x.strip()
        if x.isdigit():
            new_ids.add(int(x))

    planes = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()
    cfis = conn.execute("SELECT * FROM cfis WHERE active = 1 AND is_station = 0 ORDER BY name").fetchall()
    # Every active plane's alphabetical rank (matches the Planes legend's
    # own ORDER BY tag) - the Month view's mini timeline uses this so a
    # plane always lands in the same left-to-right position whenever it
    # shares a time slot with another one, day after day, instead of
    # shuffling based on query order. See _layout_month_cell_timeline().
    plane_order = {p["tag"]: i for i, p in enumerate(planes)}

    months_data = []
    day_flights = []
    day_timeline = []
    day_untimed = []
    week_days = []
    week_start_str = week_end_str = None
    week_prev = week_next = None
    year_list = None
    list_scroll_anchor_id = None
    range_flights = None
    day_prev = day_next = None
    if view == "day":
        try:
            day_date = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            day_date = today
            day_str = today.strftime("%Y-%m-%d")
        day_flights = _build_schedule_day(conn, day_str, plane_id or None, cfi_id or None)
        day_timeline, day_untimed = _layout_day_timeline(day_flights)
        day_prev = (day_date - timedelta(days=1)).strftime("%Y-%m-%d")
        day_next = (day_date + timedelta(days=1)).strftime("%Y-%m-%d")
        prev_month, prev_year = month, year
        next_month, next_year = month, year
    elif view == "week":
        try:
            anchor_date = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            anchor_date = today
            day_str = today.strftime("%Y-%m-%d")
        # Monday-start week, same as the Month grid's Mon-Sun columns, so a
        # booking's day-of-week column lines up the same way in both views.
        week_start = anchor_date - timedelta(days=anchor_date.weekday())
        for i in range(7):
            d = week_start + timedelta(days=i)
            d_str = d.strftime("%Y-%m-%d")
            d_flights = _build_schedule_day(conn, d_str, plane_id or None, cfi_id or None)
            d_timed, d_untimed = _layout_day_timeline(d_flights)
            week_days.append({"date_str": d_str, "weekday": calendar_mod.day_abbr[i],
                               "day_num": d.day, "is_today": d == today,
                               "timed": d_timed, "untimed": d_untimed, "flight_count": len(d_flights)})
        week_start_str = week_start.strftime("%Y-%m-%d")
        week_end_str = (week_start + timedelta(days=6)).strftime("%Y-%m-%d")
        week_prev = (week_start - timedelta(days=7)).strftime("%Y-%m-%d")
        week_next = (week_start + timedelta(days=7)).strftime("%Y-%m-%d")
        prev_month, prev_year = month, year
        next_month, next_year = month, year
    elif view == "quarter":
        quarter_start = ((month - 1) // 3) * 3 + 1
        for i in range(3):
            months_data.append(_build_schedule_month(conn, year, quarter_start + i, plane_id or None, cfi_id or None))
        prev_month, prev_year = (quarter_start - 3, year) if quarter_start > 1 else (10, year - 1)
        next_month, next_year = (quarter_start + 3, year) if quarter_start < 10 else (1, year + 1)
    elif view == "year":
        months_data = [_build_schedule_month(conn, year, m, plane_id or None, cfi_id or None) for m in range(1, 13)]
        prev_month, prev_year = month, year - 1
        next_month, next_year = month, year + 1
    elif view == "list":
        year_list, list_scroll_anchor_id = _build_schedule_year_list(conn, year, plane_id or None, cfi_id or None)
        prev_month, prev_year = month, year - 1
        next_month, next_year = month, year + 1
    elif view == "custom":
        if not custom_start or not custom_end:
            custom_start = custom_start or today.strftime("%Y-%m-%d")
            custom_end = custom_end or (today + timedelta(days=13)).strftime("%Y-%m-%d")
        if custom_start > custom_end:
            custom_start, custom_end = custom_end, custom_start
        range_flights = _schedule_rows(conn, custom_start, custom_end, plane_id or None, cfi_id or None, include_pending=True)
        prev_month, prev_year = month, year
        next_month, next_year = month, year
    else:  # month
        months_data = [_build_schedule_month(conn, year, month, plane_id or None, cfi_id or None, plane_order=plane_order)]
        prev_month, prev_year = (12, year - 1) if month == 1 else (month - 1, year)
        next_month, next_year = (1, year + 1) if month == 12 else (month + 1, year)
    conn.close()

    # Backward-compat single-month convenience var, used by the month view.
    month_data = months_data[0] if months_data else None

    instructor_legend = {}
    for cfi in cfis:
        instructor_legend[cfi["name"]] = _cfi_display_color(cfi["name"], cfi["color"])
    plane_legend = []
    for p in planes:
        base = p["schedule_color"] or PLANE_FALLBACK_COLOR
        solo = p["solo_color"] or _neon_color(base)
        plane_legend.append({"tag": p["tag"], "color": base, "neon": solo,
                             "text": _text_on(base), "neon_text": _text_on(solo),
                             "unset": not p["schedule_color"]})

    return dict(view=view, month_data=month_data, months_data=months_data,
                day_str=day_str, day_flights=day_flights, day_timeline=day_timeline, day_untimed=day_untimed,
                day_timeline_start_min=DAY_TIMELINE_START_MIN, day_timeline_end_min=DAY_TIMELINE_END_MIN,
                day_prev=day_prev, day_next=day_next,
                week_days=week_days, week_start_str=week_start_str, week_end_str=week_end_str,
                week_prev=week_prev, week_next=week_next,
                year_list=year_list, list_scroll_anchor_id=list_scroll_anchor_id,
                custom_start=custom_start, custom_end=custom_end, range_flights=range_flights,
                # Past-day graying (Month cells, Day/Week timelines, List/
                # Custom rows - see schedule-row-past and the calendar-day-
                # cell-past / day-timeline-past-overlay styles): today's date
                # and time-of-day in minutes since midnight, so a template
                # can tell a wholly past day from today's already-elapsed
                # portion without doing its own clock math.
                today_str=today.strftime("%Y-%m-%d"), now_min=datetime.now().hour * 60 + datetime.now().minute,
                today=today, year=year, month=month, prev_year=prev_year, prev_month=prev_month,
                next_year=next_year, next_month=next_month,
                planes=planes, cfis=cfis, plane_id=plane_id, cfi_id=cfi_id,
                instructor_legend=instructor_legend, plane_legend=plane_legend, highlight_ids=highlight_ids,
                new_ids=new_ids,
                month_timeline_start_min=MONTH_TIMELINE_WINDOW_START, month_timeline_end_min=MONTH_TIMELINE_WINDOW_END,
                month_timeline_px_per_min=MONTH_TIMELINE_PX_PER_MIN,
                # Day/Week timeline click-to-schedule (schedule.html): the
                # same standard block grid Availability uses, so hovering
                # highlights one real lesson-length slot and clicking snaps
                # to its start instead of a raw 15-minute position.
                slot_grid_start_min=AVAILABILITY_START_MIN, slot_grid_min=AVAILABILITY_SLOT_MIN)


@flight_bp.route("/schedule")
@login_required
def schedule_calendar():
    """Flight School's own schedule calendar - separate from the shop's
    maintenance calendar - showing booked flights (including ones already
    started or completed, greyed out) colored by instructor and labeled
    with plane + time, optionally filtered down to one plane or one
    instructor. Several views share this one route: Day, Week, Month
    (default), Quarter, Year, List (grouped by month, like Maintenance's),
    and a Custom Range someone picks by hand."""
    return render_template("flight/schedule.html", **_schedule_calendar_context())


@flight_bp.route("/schedule/live")
@login_required
def schedule_live():
    """Polled by the schedule page (see schedule.html) to refresh the
    calendar/list content - same view, same Plane/Instructor filters, same
    query args as the page currently has - without a full reload. This is
    what actually fixes a shop-TV/kiosk browser left open on the Schedule
    calendar (as opposed to the Dashboard): starting or ending a flight, or
    booking/approving one from another device, now shows up here too."""
    return render_template("flight/_schedule_live.html", **_schedule_calendar_context())


@flight_bp.route("/schedule/availability")
@login_required
def schedule_availability():
    """The Availability view: which plane(s) are free when - Plane
    availability only (see _build_availability_day()), reached from a
    button next to the Schedule's Day/Week/Month/... view selector rather
    than being one more entry in it (see the button in
    _schedule_live.html). Its own Day/Week/Month view selector (separate
    from the Schedule's) controls how much of the calendar is shown at
    once:
      - Day: the full plane x time-slot grid for one day (Calendar/List
        sub-view, via `mode`), same as before this view selector existed.
      - Week/Month: one open-slot COUNT per day (not the full grid - see
        _build_availability_counts()), so a week or month fits on screen;
        clicking a day jumps into the Day view for the real detail."""
    conn = get_db()
    today = date.today()
    day_str = request.args.get("date", "").strip() or today.strftime("%Y-%m-%d")
    try:
        day_date = datetime.strptime(day_str, "%Y-%m-%d").date()
    except ValueError:
        day_date = today
        day_str = today.strftime("%Y-%m-%d")
    plane_id = request.args.get("plane_id", "").strip()
    mode = request.args.get("mode", "calendar")
    if mode not in ("calendar", "list"):
        mode = "calendar"
    view = request.args.get("view", "day")
    if view not in ("day", "week", "month"):
        view = "day"

    all_planes = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()
    ctx = dict(day_str=day_str, today=today, all_planes=all_planes, plane_id=plane_id, mode=mode, view=view)

    if view == "day":
        availability = _build_availability_day(conn, day_str, plane_id or None)
        ctx.update(availability,
                   day_prev=(day_date - timedelta(days=1)).strftime("%Y-%m-%d"),
                   day_next=(day_date + timedelta(days=1)).strftime("%Y-%m-%d"))
    elif view == "week":
        # Monday-start, matching the Schedule's own Week view.
        week_start = day_date - timedelta(days=day_date.weekday())
        week_dates = [week_start + timedelta(days=i) for i in range(7)]
        date_strs = [d.strftime("%Y-%m-%d") for d in week_dates]
        counts = _build_availability_counts(conn, date_strs, plane_id or None)
        today_str = today.strftime("%Y-%m-%d")
        week_days = [{"date": ds, "date_label": f"{d.strftime('%a')} {d.month}/{d.day}",
                      "count": counts[ds], "is_today": ds == today_str}
                     for ds, d in zip(date_strs, week_dates)]
        ctx.update(week_days=week_days, week_label=f"Week of {week_dates[0].month}/{week_dates[0].day}",
                   week_prev=(week_start - timedelta(days=7)).strftime("%Y-%m-%d"),
                   week_next=(week_start + timedelta(days=7)).strftime("%Y-%m-%d"))
    else:  # month
        year, month = day_date.year, day_date.month
        first_weekday, days_in_month = calendar_mod.monthrange(year, month)
        month_dates = [f"{year:04d}-{month:02d}-{d:02d}" for d in range(1, days_in_month + 1)]
        counts = _build_availability_counts(conn, month_dates, plane_id or None)
        today_str = today.strftime("%Y-%m-%d")
        weeks = []
        week = [None] * first_weekday
        for d in range(1, days_in_month + 1):
            ds = f"{year:04d}-{month:02d}-{d:02d}"
            week.append({"day": d, "date": ds, "count": counts[ds], "is_today": ds == today_str})
            if len(week) == 7:
                weeks.append(week)
                week = []
        if week:
            week += [None] * (7 - len(week))
            weeks.append(week)
        prev_month_date = date(year, month, 1) - timedelta(days=1)
        next_month_date = date(year, month, days_in_month) + timedelta(days=1)
        ctx.update(weeks=weeks, month_name=calendar_mod.month_name[month], year=year,
                   month_prev=prev_month_date.strftime("%Y-%m-%d"),
                   month_next=next_month_date.strftime("%Y-%m-%d"))

    conn.close()
    return render_template("flight/availability.html", **ctx)


@flight_bp.route("/schedule/find")
@admin_required
def schedule_finder():
    """The flexible Flight Finder: enter desired parameters (date window,
    weekday/weekend, morning/afternoon/evening, plane flexible or specific,
    instructor flexible/specific/female-only/male-only/none) and see every open
    (date, slot, plane, instructor) combination that matches - admin-only
    (booking access to this tool is admin-only; everyone else still books
    the normal way via Schedule Flight). Search-and-list only - picking a
    result still goes through the regular Schedule Flight form to actually
    book it, since that form is where the real conflict-check/approval
    logic lives."""
    conn = get_db()
    today = date.today()
    start_str = request.args.get("start_date", "").strip() or today.strftime("%Y-%m-%d")
    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
    except ValueError:
        start_date = today
        start_str = today.strftime("%Y-%m-%d")
    if start_date < today:
        start_date = today
        start_str = today.strftime("%Y-%m-%d")

    try:
        num_days = int(request.args.get("days", "14"))
    except ValueError:
        num_days = 14
    if num_days not in (7, 14, 30):
        num_days = 14

    day_types = [v for v in request.args.getlist("day_type") if v in ("weekday", "weekend")] or ["weekday", "weekend"]
    buckets = [v for v in request.args.getlist("bucket") if v in ("morning", "afternoon", "evening")] \
        or ["morning", "afternoon", "evening"]
    plane_id = request.args.get("plane_id", "").strip()
    cfi_mode = request.args.get("cfi_mode", "any").strip() or "any"

    all_planes = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()
    all_cfis = conn.execute("SELECT * FROM cfis WHERE active = 1 AND is_station = 0 ORDER BY name").fetchall()
    results = _build_flight_finder(conn, start_date, num_days, day_types, buckets, plane_id or None, cfi_mode)
    conn.close()

    return render_template("flight/finder.html", start_str=start_str, num_days=num_days, day_types=day_types,
                           buckets=buckets, plane_id=plane_id, cfi_mode=cfi_mode, all_planes=all_planes,
                           all_cfis=all_cfis, results=results, finder_buckets=FINDER_TIME_BUCKETS, today=today)


@flight_bp.route("/schedule/new", methods=["GET", "POST"])
@login_required
def schedule_new():
    """Booking a flight. A CFI booking it is trusted and goes straight onto
    the calendar ('scheduled'). A student booking their own flight instead
    goes in as 'pending_approval' - it doesn't show on the calendar or
    count against conflict checks until an instructor approves it, so
    students can request flights without being able to just claim a plane
    or instructor's time unchecked."""
    conn = get_db()
    self_service = not session.get("cfi_id")
    self_student = current_student(conn) if self_service else None
    planes = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()
    students = conn.execute("SELECT * FROM students WHERE active = 1 ORDER BY name").fetchall()
    cfis = conn.execute("SELECT * FROM cfis WHERE active = 1 AND is_station = 0 ORDER BY name").fetchall()
    # Clicking a day on the calendar (see the month_table macro in
    # _schedule_live.html) links here with ?date=YYYY-MM-DD to pre-fill the
    # date field, instead of always defaulting to today.
    prefill_date = request.args.get("date", "").strip()
    try:
        datetime.strptime(prefill_date, "%Y-%m-%d")
    except ValueError:
        prefill_date = date.today().strftime("%Y-%m-%d")
    # The Availability finder (finder.html) links here with ?time=HH:MM too,
    # from the specific open slot that was clicked, so the time picker
    # starts on that slot instead of defaulting to 9:00 - still freely
    # adjustable from there like any other booking.
    prefill_time = request.args.get("time", "").strip()
    try:
        datetime.strptime(prefill_time, "%H:%M")
    except ValueError:
        prefill_time = ""
    # The Availability grid/list also links here with ?asset_id=... from the
    # specific plane/simulator whose open slot was clicked, so that plane
    # comes pre-picked too instead of just the time - still freely
    # changeable from there like any other booking.
    prefill_asset_id = request.args.get("asset_id", "").strip()
    if prefill_asset_id and not any(str(p["id"]) == prefill_asset_id for p in planes):
        prefill_asset_id = ""
    form_kwargs = dict(planes=planes, students=students, cfis=cfis,
                       today=prefill_date, prefill_time=prefill_time, prefill_asset_id=prefill_asset_id,
                       current_cfi_id=session.get("cfi_id"),
                       self_service=self_service, self_student=self_student,
                       # ?complete=1 opens the form with "Flight Already
                       # Complete?" already checked (old Log a Flight links).
                       start_complete=(not self_service and request.args.get("complete") == "1"))
    if request.method == "POST":
        # "Flight Already Complete?" (CFI/admin only): the flight already
        # happened and was never put on the schedule, so log it straight
        # into Flight History instead of booking it - same saving, Hobbs/
        # Tach, squawk and auto-deduct as logging a booking (log_new). This
        # replaced the old stand-alone "Log a Flight" page.
        if request.form.get("already_complete") and not self_service:
            error = _save_logged_flight(conn, request.form, date_field="scheduled_date", notes_field="log_notes")
            if error:
                flash(error, "danger")
                conn.close()
                return render_template("flight/schedule_form.html", form=request.form, **form_kwargs)
            conn.close()
            return redirect(url_for("flight.log_history"))
        asset_id = request.form.get("asset_id") or None
        student_id = str(self_student["id"]) if self_service else (request.form.get("student_id") or None)
        guest_name, guest_phone, guest_email = (None, None, None) if self_service else _guest_fields(conn, request.form, student_id)
        solo = 1 if request.form.get("solo") else 0
        # A dual booking where the student also flies part of it solo.
        part_solo = 0 if solo else (1 if request.form.get("part_solo") else 0)
        # Blank now means genuinely unassigned/TBD (see the "-- Unassigned --"
        # option in the form) rather than silently defaulting to whoever's
        # booking it - a booking CFI leaves their own name pre-selected in
        # the dropdown, so this only goes blank when someone deliberately
        # picks "Unassigned" or checks Solo.
        cfi_id = None if solo else (request.form.get("cfi_id") or None)
        scheduled_date = request.form.get("scheduled_date", "").strip()
        scheduled_time = request.form.get("scheduled_time", "").strip() or None
        duration_hours = _parse_float(request.form.get("duration_hours"))
        notes = request.form.get("notes", "").strip() or None
        # Admin/CFI-only - never present in the self-service form at all, but
        # force it to None here too so nothing sneaks through even if a
        # student's request happened to include the field name.
        private_notes = (request.form.get("private_notes", "").strip() or None) if not self_service else None
        created_by = session.get("user_name") or (self_student["name"] if self_student else None)
        if not asset_id or not student_id or not scheduled_date:
            flash("Select a plane, a student, and a date.", "danger")
            conn.close()
            return render_template("flight/schedule_form.html", form=request.form, **form_kwargs)
        past_error = _past_booking_error(scheduled_date, scheduled_time)
        if past_error:
            flash(past_error, "danger")
            conn.close()
            return render_template("flight/schedule_form.html", form=request.form, **form_kwargs)

        # Recurrence is a "repeat the same booking on more dates" option
        # only offered to a CFI/admin actually placing the booking - a
        # student's self-service request always goes in one at a time
        # since each occurrence needs its own instructor approval anyway.
        repeat = (request.form.get("repeat") or "none").strip() if not self_service else "none"
        repeat_until = request.form.get("repeat_until", "").strip()
        occurrence_dates = _generate_recurrence_dates(scheduled_date, repeat, repeat_until) if repeat != "none" else [scheduled_date]

        if len(occurrence_dates) == 1:
            # Single booking - identical to the pre-recurrence behavior,
            # including a hard stop (not a skip) on conflict.
            if not self_service:
                # A student's own request isn't checked for conflicts yet since
                # it isn't confirmed - the approving instructor's conflict check
                # (in schedule_approve) is what actually guards the calendar.
                conflicts = _scheduling_conflicts(conn, asset_id, cfi_id, student_id, scheduled_date, scheduled_time, duration_hours)
                if conflicts:
                    for c in conflicts:
                        flash(c["message"], "danger")
                    flash("Not scheduled - resolve the conflict above (a different time, plane, or instructor) and try again.", "danger")
                    conn.close()
                    return render_template("flight/schedule_form.html", form=request.form, conflicts=conflicts,
                                           conflict_url=_conflict_highlight_url(conflicts), **form_kwargs)
            if solo or not self_service:
                needs_review, review_reason = _schedule_review_flag(conn, solo, cfi_id, student_id, scheduled_date)
            else:
                needs_review, review_reason = 0, None
            status = "pending_approval" if self_service else "scheduled"
            confirmed_at, confirm_required, notify_user_id = _booking_confirm_state(conn, self_service, guest_name, student_id)
            new_booking = conn.execute("""INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date,
                             scheduled_time, duration_hours, notes, private_notes, status, created_by, solo, needs_review,
                             review_reason, part_solo, guest_name, guest_phone, guest_email, created_at, confirmed_at, confirm_required)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (asset_id, cfi_id, student_id, scheduled_date, scheduled_time, duration_hours,
                          notes, private_notes, status, created_by, solo, needs_review, review_reason, part_solo,
                          guest_name, guest_phone, guest_email, now_iso(), confirmed_at, confirm_required))
            conn.commit()
            if notify_user_id:
                notify_pref = conn.execute("SELECT notify_booking_confirm FROM students WHERE id = ?",
                                           (student_id,)).fetchone()
                if not notify_pref or notify_pref["notify_booking_confirm"]:
                    plane_row = conn.execute("SELECT tag FROM assets WHERE id = ?", (asset_id,)).fetchone()
                    _notify_booking_confirm(conn, notify_user_id, new_booking.lastrowid,
                                            plane_row["tag"] if plane_row else "the plane", scheduled_date, scheduled_time)
            conn.close()
            if self_service:
                if needs_review:
                    flash("Flight requested - heads up, this needs a CFI review before it can be soloed (see the reason on your dashboard).", "warning")
                else:
                    flash("Flight requested - your instructor will need to approve it before it's confirmed.", "success")
                return redirect(url_for("flight.dashboard"))
            if needs_review:
                flash(f"Flight scheduled, but flagged for review: {review_reason}", "warning")
            else:
                flash("Flight scheduled.", "success")
            return redirect(url_for("flight.schedule_calendar", view="month", year=scheduled_date[:4],
                                    month=int(scheduled_date[5:7]), new=new_booking.lastrowid))

        # Recurring booking - each occurrence is checked for conflicts on
        # its own date; a conflicting occurrence is skipped (not fatal to
        # the rest of the series) so one busy day doesn't block the whole
        # run, and the CFI/admin sees exactly which dates were skipped.
        created_count = 0
        created_ids = []
        skipped = []  # list of (date_str, conflict dict)
        review_count = 0
        confirmed_at, confirm_required, notify_user_id = _booking_confirm_state(conn, self_service, guest_name, student_id)
        for occ_date in occurrence_dates:
            conflicts = _scheduling_conflicts(conn, asset_id, cfi_id, student_id, occ_date, scheduled_time, duration_hours)
            if conflicts:
                skipped.append((occ_date, conflicts[0]))
                continue
            needs_review, review_reason = _schedule_review_flag(conn, solo, cfi_id, student_id, occ_date)
            occ_cur = conn.execute("""INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date,
                             scheduled_time, duration_hours, notes, private_notes, status, created_by, solo, needs_review,
                             review_reason, part_solo, guest_name, guest_phone, guest_email, created_at, confirmed_at, confirm_required)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (asset_id, cfi_id, student_id, occ_date, scheduled_time, duration_hours,
                          notes, private_notes, created_by, solo, needs_review, review_reason, part_solo,
                          guest_name, guest_phone, guest_email, now_iso(), confirmed_at, confirm_required))
            created_count += 1
            created_ids.append(occ_cur.lastrowid)
            if needs_review:
                review_count += 1
        conn.commit()
        if notify_user_id and created_count:
            # One push for the whole series rather than one per occurrence -
            # a 10-date recurring booking shouldn't mean 10 separate pings.
            plane_row = conn.execute("SELECT tag FROM assets WHERE id = ?", (asset_id,)).fetchone()
            plane_tag = plane_row["tag"] if plane_row else "the plane"
            try:
                push.queue_and_push(conn, notify_user_id, "Confirm your flights",
                                    f"{created_count} new {plane_tag} flights were booked for you, starting "
                                    f"{occurrence_dates[0]}. Open OpsHub to confirm them.",
                                    tag=f"confirm-series-{created_ids[0]}", url=url_for("flight.dashboard"))
            except Exception:
                current_app.logger.exception("Booking-confirm push failed")
        conn.close()

        if created_count:
            msg = f"Scheduled {created_count} flight{'s' if created_count != 1 else ''} ({occurrence_dates[0]} through {occurrence_dates[-1]})."
            if review_count:
                msg += f" {review_count} flagged for review."
            flash(msg, "success" if not review_count else "warning")
        if skipped:
            flash(f"Skipped {len(skipped)} date{'s' if len(skipped) != 1 else ''} due to a conflict: " +
                  ", ".join(f"{d} ({c['message']})" for d, c in skipped[:8]) + (", ..." if len(skipped) > 8 else ""), "danger")
        if not created_count:
            flash("No flights were scheduled - every date in the range conflicted.", "danger")
            return redirect(url_for("flight.schedule_new"))
        if skipped:
            # Point straight at the conflicting bookings instead of just the
            # month the series starts in - a custom range spanning every
            # skipped date, with each conflicting flight highlighted red and
            # everything else on those days faded into the background.
            skip_dates = sorted(d for d, _ in skipped)
            skip_ids = ",".join(str(c["id"]) for _, c in skipped)
            return redirect(url_for("flight.schedule_calendar", view="custom", start=skip_dates[0],
                                    end=skip_dates[-1], highlight=skip_ids))
        first = occurrence_dates[0]
        return redirect(url_for("flight.schedule_calendar", view="month", year=first[:4], month=int(first[5:7]),
                                new=",".join(str(i) for i in created_ids)))
    conn.close()
    return render_template("flight/schedule_form.html", form=None, **form_kwargs)


def _pending_decision_response(ok, message, category, redirect_url=None):
    """Approve/deny reply, either shape a caller wants: the Dashboard's
    Pending Approval buttons call these routes over fetch() so the page
    never reloads (and the person never loses their scroll position) - for
    that, a plain JSON body the button's own JS turns into a floating flag.
    Anything that still posts here the old way (JS failed to load, or a
    future caller) gets the original flash-and-redirect behavior."""
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        payload = {"ok": ok, "message": message, "category": category}
        if redirect_url:
            payload["redirect"] = redirect_url
        return jsonify(payload)
    flash(message, category)
    return redirect(redirect_url or url_for("flight.dashboard"))


@flight_bp.route("/schedule/<int:scheduled_id>/approve", methods=["POST"])
@cfi_required
def schedule_approve(scheduled_id):
    """Confirms a student's flight request. Re-checks for conflicts at
    approval time (the request itself skipped that check, since it wasn't
    confirmed yet) - if the plane/instructor/student got double-booked by
    something else in the meantime, the approving instructor sees exactly
    why and can edit or deny instead."""
    conn = get_db()
    sched = conn.execute("SELECT * FROM scheduled_flights WHERE id = ?", (scheduled_id,)).fetchone()
    if not sched or sched["status"] != "pending_approval":
        conn.close()
        return _pending_decision_response(False, "That request isn't awaiting approval.", "danger")
    past_error = _past_booking_error(sched["scheduled_date"], sched["scheduled_time"])
    if past_error:
        conn.close()
        return _pending_decision_response(False, "Can't approve - the requested time has already passed. Edit it to a new time or deny it.", "danger")
    solo = sched["solo"]
    # A solo request stays solo (no auto-assigned instructor) - only an
    # "Any instructor" dual request (not solo, no cfi_id picked) defaults to
    # whichever CFI is doing the approving.
    cfi_id = sched["cfi_id"] or (None if solo else session["cfi_id"])
    conflicts = _scheduling_conflicts(conn, sched["asset_id"], cfi_id, sched["student_id"],
                                      sched["scheduled_date"], sched["scheduled_time"], sched["duration_hours"],
                                      exclude_id=scheduled_id)
    if conflicts:
        conn.close()
        msg = " ".join(c["message"] for c in conflicts) + " Can't approve as-is - resolve the conflict (edit or deny the request) and try again."
        return _pending_decision_response(False, msg, "danger", redirect_url=_conflict_highlight_url(conflicts))
    needs_review, review_reason = _schedule_review_flag(conn, solo, cfi_id, sched["student_id"], sched["scheduled_date"])
    # A freshly (re)computed review flag always starts unacknowledged - see
    # the needs_review_acknowledged_at/by columns' migration comment in
    # db.py for why.
    conn.execute("""UPDATE scheduled_flights SET status = 'scheduled', cfi_id = ?, needs_review = ?, review_reason = ?,
                     needs_review_acknowledged_at = NULL, needs_review_acknowledged_by = NULL WHERE id = ?""",
                 (cfi_id, needs_review, review_reason, scheduled_id))
    when = f" on {_us_date(sched['scheduled_date'])}" + (f" at {_format_time_12h(sched['scheduled_time'])}" if sched["scheduled_time"] else "")
    msg = f"Your flight request{when} was approved and added to the calendar."
    if needs_review:
        msg += f" (flagged for review: {review_reason})"
    _notify_student(conn, sched["student_id"], "flight_approved", msg, url_for("flight.dashboard"), scheduled_flight_id=scheduled_id)
    conn.commit()
    conn.close()
    if needs_review:
        return _pending_decision_response(True, f"Flight request approved, but flagged for review: {review_reason}", "warning")
    return _pending_decision_response(True, "Flight request approved and added to the calendar.", "success")


@flight_bp.route("/schedule/<int:scheduled_id>/deny", methods=["POST"])
@cfi_required
def schedule_deny(scheduled_id):
    """Denies a student's flight request - requires a reason (unlike an
    approval, which just needs a click) so the student sees directly why
    instead of a generic "contact your instructor"."""
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        return _pending_decision_response(False, "Say why you're denying this request.", "danger")
    conn = get_db()
    sched = conn.execute("SELECT * FROM scheduled_flights WHERE id = ? AND status = 'pending_approval'",
                         (scheduled_id,)).fetchone()
    conn.execute("UPDATE scheduled_flights SET status = 'denied', deny_reason = ? WHERE id = ? AND status = 'pending_approval'",
                 (reason, scheduled_id))
    if sched:
        when = f" on {_us_date(sched['scheduled_date'])}" + (f" at {_format_time_12h(sched['scheduled_time'])}" if sched["scheduled_time"] else "")
        _notify_student(conn, sched["student_id"], "flight_denied",
                        f"Your flight request{when} was denied: {reason}",
                        url_for("flight.schedule_new"), scheduled_flight_id=scheduled_id)
    conn.commit()
    conn.close()
    return _pending_decision_response(True, "Flight request denied.", "info")


@flight_bp.route("/schedule/<int:scheduled_id>/dismiss", methods=["POST"])
@login_required
def schedule_dismiss(scheduled_id):
    """A student clearing a denied request off their own "Your Requests"
    list - just hides it there, the row (and its deny_reason) stays on
    file same as ever. Also clears that denial's own notification banner
    (see scheduled_flight_id on student_notifications) - otherwise the
    banner up top kept showing the same denied flight the student had just
    removed from the list below it."""
    conn = get_db()
    sched = conn.execute("SELECT student_id, status FROM scheduled_flights WHERE id = ?", (scheduled_id,)).fetchone()
    if not sched or sched["student_id"] != session.get("student_id") or sched["status"] != "denied":
        conn.close()
        return _pending_decision_response(False, "That request can't be removed.", "danger", redirect_url=url_for("flight.dashboard"))
    conn.execute("UPDATE scheduled_flights SET student_dismissed_at = ? WHERE id = ?", (now_iso(), scheduled_id))
    conn.execute("""UPDATE student_notifications SET read_at = ? WHERE scheduled_flight_id = ? AND student_id = ? AND read_at IS NULL""",
                 (now_iso(), scheduled_id, session.get("student_id")))
    conn.commit()
    conn.close()
    return _pending_decision_response(True, "Removed.", "info", redirect_url=url_for("flight.dashboard"))


@flight_bp.route("/schedule/<int:scheduled_id>/edit", methods=["GET", "POST"])
@cfi_required
def schedule_edit(scheduled_id):
    conn = get_db()
    sched = conn.execute("SELECT * FROM scheduled_flights WHERE id = ?", (scheduled_id,)).fetchone()
    if not sched:
        conn.close()
        flash("Scheduled flight not found.", "danger")
        return redirect(url_for("flight.schedule_calendar"))
    planes = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()
    students = conn.execute("SELECT * FROM students WHERE active = 1 ORDER BY name").fetchall()
    cfis = conn.execute("SELECT * FROM cfis WHERE active = 1 AND is_station = 0 ORDER BY name").fetchall()
    form_kwargs = dict(planes=planes, students=students, cfis=cfis,
                       today=date.today().strftime("%Y-%m-%d"), current_cfi_id=session["cfi_id"], sched=sched)
    if request.method == "POST":
        asset_id = request.form.get("asset_id") or None
        student_id = request.form.get("student_id") or None
        guest_name, guest_phone, guest_email = _guest_fields(conn, request.form, student_id)
        solo = 1 if request.form.get("solo") else 0
        # A dual booking where the student also flies part of it solo.
        part_solo = 0 if solo else (1 if request.form.get("part_solo") else 0)
        # Blank means genuinely unassigned/TBD now, not "assume it's me" -
        # see schedule_new for the reasoning.
        cfi_id = None if solo else (request.form.get("cfi_id") or None)
        scheduled_date = request.form.get("scheduled_date", "").strip()
        scheduled_time = request.form.get("scheduled_time", "").strip() or None
        duration_hours = _parse_float(request.form.get("duration_hours"))
        if not asset_id or not student_id or not scheduled_date:
            flash("Select a plane, a student, and a date.", "danger")
            conn.close()
            return render_template("flight/schedule_form.html", form=request.form, **form_kwargs)
        # Moving a booking into the past isn't allowed; editing other details
        # of a booking whose time has already passed still is.
        if (scheduled_date, scheduled_time) != (sched["scheduled_date"], sched["scheduled_time"]):
            past_error = _past_booking_error(scheduled_date, scheduled_time)
            if past_error:
                flash(past_error, "danger")
                conn.close()
                return render_template("flight/schedule_form.html", form=request.form, **form_kwargs)
        conflicts = _scheduling_conflicts(conn, asset_id, cfi_id, student_id, scheduled_date, scheduled_time,
                                          duration_hours, exclude_id=scheduled_id)
        if conflicts:
            for c in conflicts:
                flash(c["message"], "danger")
            flash("Not saved - resolve the conflict above and try again.", "danger")
            conn.close()
            return render_template("flight/schedule_form.html", form=request.form, conflicts=conflicts,
                                   conflict_url=_conflict_highlight_url(conflicts), **form_kwargs)
        # Same flag-instead-of-block treatment as scheduling a new flight -
        # this also doubles as how a flagged flight gets "reviewed": adding
        # a CFI here (cfi_id becomes non-NULL) clears the flag automatically.
        needs_review, review_reason = _schedule_review_flag(conn, solo, cfi_id, student_id, scheduled_date)
        # A freshly (re)computed review flag always starts unacknowledged -
        # see the needs_review_acknowledged_at/by columns' migration comment
        # in db.py for why.
        conn.execute("""UPDATE scheduled_flights SET asset_id=?, cfi_id=?, student_id=?, scheduled_date=?,
                         scheduled_time=?, duration_hours=?, notes=?, private_notes=?, solo=?, part_solo=?, guest_name=?, guest_phone=?, guest_email=?, needs_review=?, review_reason=?,
                         needs_review_acknowledged_at=NULL, needs_review_acknowledged_by=NULL WHERE id=?""",
                     (asset_id, cfi_id, student_id, scheduled_date, scheduled_time, duration_hours,
                      request.form.get("notes", "").strip() or None,
                      request.form.get("private_notes", "").strip() or None,
                      solo, part_solo, guest_name, guest_phone, guest_email, needs_review, review_reason, scheduled_id))
        conn.commit()
        conn.close()
        if needs_review:
            flash(f"Scheduled flight updated, but still flagged for review: {review_reason}", "warning")
        else:
            flash("Scheduled flight updated.", "success")
        return redirect(url_for("flight.schedule_calendar", year=scheduled_date[:4], month=int(scheduled_date[5:7])))
    conn.close()
    return render_template("flight/schedule_form.html", form=None, **form_kwargs)


@flight_bp.route("/schedule/<int:scheduled_id>/reschedule", methods=["POST"])
@cfi_required
def schedule_reschedule(scheduled_id):
    """Quick reschedule: changes only the date/time on an existing booking -
    plane, student, instructor, solo, duration, and notes all stay exactly
    as they were. Separate from the full Edit form for the common case of
    'same lesson, different time slot'. Re-checks for conflicts at the new
    time/date the same way a full edit would."""
    conn = get_db()
    sched = conn.execute("SELECT * FROM scheduled_flights WHERE id = ?", (scheduled_id,)).fetchone()
    if not sched:
        conn.close()
        flash("Scheduled flight not found.", "danger")
        return redirect(url_for("flight.schedule_calendar"))
    if sched["status"] != "scheduled":
        conn.close()
        flash("Only a still-scheduled flight can be rescheduled (this one has already started, flown, or was cancelled).", "danger")
        return redirect(url_for("flight.schedule_calendar"))
    new_date = request.form.get("scheduled_date", "").strip()
    new_time = request.form.get("scheduled_time", "").strip() or None
    if not new_date:
        flash("Pick a new date.", "danger")
        conn.close()
        return redirect(url_for("flight.schedule_calendar", year=sched["scheduled_date"][:4], month=int(sched["scheduled_date"][5:7])))
    past_error = _past_booking_error(new_date, new_time)
    if past_error:
        flash("Not rescheduled - " + past_error, "danger")
        conn.close()
        return redirect(url_for("flight.schedule_calendar", year=sched["scheduled_date"][:4], month=int(sched["scheduled_date"][5:7])))
    conflicts = _scheduling_conflicts(conn, sched["asset_id"], sched["cfi_id"], sched["student_id"],
                                      new_date, new_time, sched["duration_hours"], exclude_id=scheduled_id)
    if conflicts:
        for c in conflicts:
            flash(c["message"], "danger")
        flash("Not rescheduled - resolve the conflict above and try again.", "danger")
        conn.close()
        return redirect(_conflict_highlight_url(conflicts))
    # Same flag-instead-of-block treatment as the full Edit form - moving a
    # booking to a new date can newly trip a currency/review check even
    # though plane/instructor/student/solo didn't change here, so this has
    # to recompute it too instead of silently carrying over the old flag.
    needs_review, review_reason = _schedule_review_flag(conn, sched["solo"], sched["cfi_id"], sched["student_id"], new_date)
    # Moving the booking is exactly what a change request asked for -
    # clears it the same as an explicit Dismiss would (see
    # schedule_change_request_dismiss), so it doesn't linger in the Change
    # Requests queue for a request that's already been acted on.
    conn.execute("""UPDATE scheduled_flights SET scheduled_date=?, scheduled_time=?, needs_review=?, review_reason=?,
                     needs_review_acknowledged_at=NULL, needs_review_acknowledged_by=NULL,
                     change_request_note=NULL, change_requested_at=NULL WHERE id=?""",
                 (new_date, new_time, needs_review, review_reason, scheduled_id))
    conn.commit()
    conn.close()
    if needs_review:
        flash(f"Flight rescheduled, but flagged for review: {review_reason}", "warning")
    else:
        flash("Flight rescheduled.", "success")
    return redirect(url_for("flight.schedule_calendar", year=new_date[:4], month=int(new_date[5:7])))


# ---------------------------------------------------------------------------
# Alerts tab - every booking flagged for review, like Squawks on the
# Maintenance side: open (not yet acknowledged), acknowledged but not yet
# resolved, and a resolved archive. Acknowledging only records who saw it
# and when; an alert only leaves the open list once the issue behind it is
# actually fixed (see _sync_flight_alerts()).
# ---------------------------------------------------------------------------

@flight_bp.after_request
def _sync_alerts_after_post(response):
    """After any Flight School form post (a booking edited, a CFI assigned,
    a sign-off entered, a flight logged or cancelled...), bring the Alerts
    tab up to date, recording the logged-in person as whoever resolved
    anything that just cleared. Never lets a sync problem break the page."""
    if request.method == "POST" and (session.get("cfi_id") or session.get("student_id")):
        try:
            conn = get_db()
            try:
                resolved_now = _sync_flight_alerts(conn, session.get("user_name"))
            finally:
                conn.close()
            for resolution in resolved_now:
                flash(f"✓ Alert resolved: {resolution}", "success")
        except Exception:
            current_app.logger.exception("Flight alerts sync failed")
    return response


HOBBS_GAP_MIN = 0.1  # hours - smaller differences are rounding on the meter


def _hobbs_gaps(conn):
    """Unaccounted Hobbs time, per plane: the next flight started with a
    higher Hobbs than the previous logged flight ended on, so the plane ran
    (or the meter was misread) with no flight logged for it. Pairs a master
    admin already marked reviewed (hobbs_gap_reviews) are left out.
    Newest first."""
    rows = conn.execute("""
        SELECT f.id, f.asset_id, f.flight_date, f.hobbs_start, f.hobbs_end, f.started_at, f.ended_at,
               a.tag as plane_tag, COALESCE(NULLIF(f.guest_name, '') || ' (guest)', s.name) as student_name,
               c.name as cfi_name
        FROM flights f
        JOIN assets a ON a.id = f.asset_id
        LEFT JOIN students s ON s.id = f.student_id
        LEFT JOIN cfis c ON c.id = f.cfi_id
        WHERE f.hobbs_start IS NOT NULL
        ORDER BY f.asset_id, f.hobbs_start, f.id""").fetchall()
    reviewed = {(r["from_flight_id"], r["to_flight_id"]) for r in
                conn.execute("SELECT from_flight_id, to_flight_id FROM hobbs_gap_reviews").fetchall()}
    gaps = []
    for prev, nxt in zip(rows, rows[1:]):
        if prev["asset_id"] != nxt["asset_id"] or prev["hobbs_end"] is None:
            continue
        gap = round(nxt["hobbs_start"] - prev["hobbs_end"], 1)
        if gap >= HOBBS_GAP_MIN and (prev["id"], nxt["id"]) not in reviewed:
            gaps.append({"asset_id": prev["asset_id"], "plane_tag": prev["plane_tag"], "gap": gap,
                         "prev": dict(prev), "next": dict(nxt)})
    gaps.sort(key=lambda g: (g["next"]["flight_date"] or "", g["next"]["id"]), reverse=True)
    return gaps


@flight_bp.route("/alerts/hobbs_gap/review", methods=["POST"])
@cfi_required
def hobbs_gap_review():
    """Master admin marks an unaccounted-Hobbs gap as looked into (with an
    optional note - ferry flight, maintenance run-up, meter misread...)."""
    if not session.get("is_master_admin"):
        flash("Only an admin can review Hobbs gaps.", "danger")
        return redirect(url_for("flight.alerts_list"))
    from_id = request.form.get("from_flight_id", type=int)
    to_id = request.form.get("to_flight_id", type=int)
    conn = get_db()
    pair = conn.execute("SELECT a.id a_id, a.asset_id, a.hobbs_end, b.hobbs_start FROM flights a, flights b "
                        "WHERE a.id = ? AND b.id = ?", (from_id, to_id)).fetchone()
    if pair:
        conn.execute("""INSERT OR IGNORE INTO hobbs_gap_reviews (asset_id, from_flight_id, to_flight_id, gap_hours,
                        note, reviewed_by, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (pair["asset_id"], from_id, to_id,
                      round((pair["hobbs_start"] or 0) - (pair["hobbs_end"] or 0), 1),
                      (request.form.get("note") or "").strip()[:300] or None, session.get("user_name"), now_iso()))
        conn.commit()
        flash("Hobbs gap marked reviewed.", "success")
    conn.close()
    return redirect(url_for("flight.alerts_list") + "#hobbs-gaps")


@flight_bp.context_processor
def _alert_nav_counts():
    """Open-alert counts for the Alerts nav badge (CFIs/admins only)."""
    if not session.get("cfi_id"):
        return {}
    try:
        conn = get_db()
        try:
            row = conn.execute("""SELECT COUNT(*) AS total,
                                         SUM(CASE WHEN needs_review_acknowledged_at IS NULL THEN 1 ELSE 0 END) AS unacked
                                  FROM scheduled_flights WHERE status = 'scheduled' AND needs_review = 1""").fetchone()
            # Unaccounted Hobbs time counts too - master admins only.
            gaps = len(_hobbs_gaps(conn)) if session.get("is_master_admin") else 0
        finally:
            conn.close()
        return {"flight_alert_counts": {"open": (row["total"] or 0) + gaps, "unacked": (row["unacked"] or 0) + gaps}}
    except Exception:
        return {}


@flight_bp.context_processor
def _cfi_active_flights_widget():
    """This CFI's own currently-running flight(s) - powers the small
    floating timer/pause/end widget that follows them to every Flight
    School page (see _active_flight_widget.html) so they don't have to go
    back to Active Flight just to pause or end whatever they're flying
    right now. Only flights assigned to them (cfi_id) that are still
    actually running (started, not stopped, not ended)."""
    if not session.get("cfi_id"):
        return {}
    try:
        conn = get_db()
        try:
            rows = conn.execute("""SELECT f.id, f.started_at, f.paused_at, f.paused_seconds,
                                           a.tag as plane_tag, s.name as student_name
                                    FROM flights f JOIN assets a ON a.id = f.asset_id JOIN students s ON s.id = f.student_id
                                    WHERE f.cfi_id = ? AND f.started_at IS NOT NULL AND f.ended_at IS NULL AND f.stopped_at IS NULL
                                    ORDER BY f.started_at""", (session["cfi_id"],)).fetchall()
        finally:
            conn.close()
        return {"cfi_active_flights": rows}
    except Exception:
        return {}


@flight_bp.context_processor
def _student_notification_count():
    """Unread count for the student's own Alerts nav badge - see
    _alert_nav_counts above for the CFI-side equivalent."""
    if not session.get("student_id"):
        return {}
    try:
        conn = get_db()
        try:
            row = conn.execute("SELECT COUNT(*) c FROM student_notifications WHERE student_id = ? AND read_at IS NULL",
                               (session["student_id"],)).fetchone()
        finally:
            conn.close()
        return {"student_alert_count": row["c"] or 0}
    except Exception:
        return {}


@flight_bp.route("/notifications")
@login_required
def my_alerts():
    """A student's own notification history - "your flight was approved",
    etc. (see _notify_student). Distinct from the CFI-facing /flight/alerts
    review queue; this is the student-facing Alerts tab. Viewing this page
    marks everything currently unread as read (clears the banner and nav
    badge) - was_unread is captured before that update so this visit still
    highlights what's new."""
    conn = get_db()
    student = current_student(conn)
    if not student:
        conn.close()
        flash("Alerts are for student accounts.", "danger")
        return redirect(url_for("flight.dashboard"))
    rows = conn.execute("""SELECT * FROM student_notifications WHERE student_id = ?
                           ORDER BY created_at DESC LIMIT 100""", (student["id"],)).fetchall()
    notifications = [dict(r, was_unread=not r["read_at"]) for r in rows]
    conn.execute("UPDATE student_notifications SET read_at = ? WHERE student_id = ? AND read_at IS NULL",
                (now_iso(), student["id"]))
    conn.commit()
    conn.close()
    return render_template("flight/notifications.html", notifications=notifications)


@flight_bp.route("/notifications/<int:notification_id>/dismiss", methods=["POST"])
@login_required
def notification_dismiss(notification_id):
    """Dismisses one notification from the dashboard banner (marks it read)
    without visiting the full Alerts tab - used by the X button there."""
    conn = get_db()
    student = current_student(conn)
    if student:
        conn.execute("UPDATE student_notifications SET read_at = ? WHERE id = ? AND student_id = ? AND read_at IS NULL",
                    (now_iso(), notification_id, student["id"]))
        conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("flight.dashboard"))


@flight_bp.route("/alerts")
@cfi_required
def alerts_list():
    conn = get_db()
    _sync_flight_alerts(conn, None)
    base_sql = """SELECT fa.*, sf.scheduled_date, sf.scheduled_time, sf.solo, sf.status as booking_status,
                         a.tag as plane_tag, COALESCE(NULLIF(sf.guest_name, '') || ' (guest)', s.name) as student_name, c.name as cfi_name
                  FROM flight_alerts fa
                  LEFT JOIN scheduled_flights sf ON sf.id = fa.scheduled_flight_id
                  LEFT JOIN assets a ON a.id = sf.asset_id
                  LEFT JOIN students s ON s.id = fa.student_id
                  LEFT JOIN cfis c ON c.id = sf.cfi_id"""
    open_rows = conn.execute(base_sql + """ WHERE fa.resolved_at IS NULL
                  ORDER BY sf.scheduled_date, sf.scheduled_time IS NULL, sf.scheduled_time""").fetchall()
    show_all = request.args.get("all") == "1"
    resolved_count = conn.execute("SELECT COUNT(*) c FROM flight_alerts WHERE resolved_at IS NOT NULL").fetchone()["c"]
    resolved = conn.execute(base_sql + " WHERE fa.resolved_at IS NOT NULL ORDER BY fa.resolved_at DESC"
                            + ("" if show_all else " LIMIT 50")).fetchall()
    # Unaccounted Hobbs time - master admins only (see _hobbs_gaps).
    hobbs_gaps = _hobbs_gaps(conn) if session.get("is_master_admin") else []
    hobbs_reviewed = conn.execute("""SELECT r.*, a.tag as plane_tag FROM hobbs_gap_reviews r
                                     LEFT JOIN assets a ON a.id = r.asset_id
                                     ORDER BY r.reviewed_at DESC LIMIT 10""").fetchall() \
        if session.get("is_master_admin") else []
    conn.close()

    def _decorate(r):
        r = dict(r)
        r["time_label"] = _format_time_12h(r["scheduled_time"]) if r.get("scheduled_time") else None
        r["fix_links"] = _alert_fix_links(r["reason"], r["scheduled_flight_id"], r["student_id"])
        return r
    open_rows = [_decorate(r) for r in open_rows]
    unacked = [r for r in open_rows if not r["acknowledged_at"]]
    acked = [r for r in open_rows if r["acknowledged_at"]]
    return render_template("flight/alerts.html", unacked=unacked, acked=acked,
                           resolved=[dict(r) for r in resolved], resolved_count=resolved_count, show_all=show_all,
                           hobbs_gaps=hobbs_gaps, hobbs_reviewed=hobbs_reviewed)


FLIGHT_REPORT_CATEGORIES = {
    "plane_issue": "Plane Issue",
    "missing_checklist": "Missing Checklist",
    "concerning_issue": "Concerning Issue",
    "suggestion": "Suggestion",
}


@flight_bp.route("/reports", methods=["GET", "POST"])
@login_required
def reports_list():
    """Reports tab: any logged-in student or CFI can flag a plane issue, a
    missing checklist, a concerning issue, or a suggestion - not just a CFI
    logging a flight, and unlike a flight squawk it's visible right here in
    Flight School instead of only on the Maintenance side. A Plane Issue
    against a specific plane also creates a plane_squawks row, so it still
    flows into the normal Maintenance squawk workflow too."""
    conn = get_db()
    if request.method == "POST":
        category = request.form.get("category", "")
        notes = (request.form.get("notes") or "").strip()
        asset_id = _parse_int(request.form.get("asset_id"))
        if category not in FLIGHT_REPORT_CATEGORIES:
            flash("Pick a report type.", "danger")
        elif not notes:
            flash("Add a note describing it.", "danger")
        else:
            reported_by = session.get("user_name")
            now = now_iso()
            conn.execute("""INSERT INTO flight_reports (category, asset_id, notes, reported_by, reported_at)
                            VALUES (?, ?, ?, ?, ?)""", (category, asset_id, notes, reported_by, now))
            if category == "plane_issue" and asset_id:
                conn.execute("""INSERT INTO plane_squawks (asset_id, notes, reported_by, reported_at)
                                VALUES (?, ?, ?, ?)""", (asset_id, notes, reported_by, now))
            conn.commit()
            flash("Report filed.", "success")
        conn.close()
        return redirect(url_for("flight.reports_list"))

    base_sql = """SELECT r.*, a.tag as plane_tag FROM flight_reports r
                  LEFT JOIN assets a ON a.id = r.asset_id"""
    open_rows = conn.execute(base_sql + " WHERE r.resolved_at IS NULL ORDER BY r.reported_at DESC").fetchall()
    show_all = request.args.get("all") == "1"
    resolved_count = conn.execute("SELECT COUNT(*) c FROM flight_reports WHERE resolved_at IS NOT NULL").fetchone()["c"]
    resolved = conn.execute(base_sql + " WHERE r.resolved_at IS NOT NULL ORDER BY r.resolved_at DESC"
                            + ("" if show_all else " LIMIT 50")).fetchall()
    planes = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()
    conn.close()
    open_by_category = {cat: [dict(r) for r in open_rows if r["category"] == cat] for cat in FLIGHT_REPORT_CATEGORIES}
    return render_template("flight/reports.html", open_by_category=open_by_category, categories=FLIGHT_REPORT_CATEGORIES,
                           resolved=[dict(r) for r in resolved], resolved_count=resolved_count, show_all=show_all,
                           planes=planes)


@flight_bp.route("/reports/<int:report_id>/resolve", methods=["POST"])
@cfi_required
def report_resolve(report_id):
    conn = get_db()
    row = conn.execute("SELECT id FROM flight_reports WHERE id = ? AND resolved_at IS NULL", (report_id,)).fetchone()
    if not row:
        conn.close()
        flash("That report is no longer open.", "warning")
        return redirect(url_for("flight.reports_list"))
    conn.execute("UPDATE flight_reports SET resolved_at = ?, resolved_by = ? WHERE id = ?",
                 (now_iso(), session.get("user_name"), report_id))
    conn.commit()
    conn.close()
    flash("Marked resolved.", "success")
    return redirect(url_for("flight.reports_list"))


@flight_bp.route("/schedule/<int:scheduled_id>/review/acknowledge", methods=["POST"])
@cfi_required
def schedule_review_acknowledge(scheduled_id):
    """Dismisses a needs-review flight from the dashboard's urgent alert -
    same idea as acknowledging a Flight School squawk on the Maintenance
    side: acknowledging doesn't fix the underlying issue (no instructor
    assigned yet, or a student over their solo currency), it just marks
    that a CFI/admin has seen it. The flight stays flagged needs_review=1
    until someone actually resolves it (assigning a CFI clears the flag on
    its own - see _schedule_review_flag()); if it's edited and still comes
    back flagged, the edit save resets these two columns to NULL so it
    surfaces for acknowledgment again instead of staying silently
    dismissed."""
    conn = get_db()
    sf = conn.execute("SELECT id FROM scheduled_flights WHERE id = ? AND needs_review = 1", (scheduled_id,)).fetchone()
    if not sf:
        conn.close()
        flash("That flight is no longer flagged for review.", "warning")
        return redirect(request.referrer or url_for("flight.dashboard"))
    conn.execute("UPDATE scheduled_flights SET needs_review_acknowledged_at = ?, needs_review_acknowledged_by = ? WHERE id = ?",
                 (now_iso(), session.get("user_name"), scheduled_id))
    conn.commit()
    conn.close()
    # Lands on the Alerts tab (not back on the dashboard) so it's clear
    # where the acknowledged alert went.
    flash("Acknowledged - it stays here in Alerts (and flagged on the Schedule) until it's resolved.", "success")
    return redirect(url_for("flight.alerts_list"))


@flight_bp.route("/schedule/<int:scheduled_id>/remind_confirm", methods=["POST"])
@cfi_required
def schedule_remind_confirm(scheduled_id):
    """A CFI/admin nudging a student who still hasn't confirmed a booking
    someone made for them (see the Unconfirmed list on the dashboard) -
    re-sends the same push _notify_booking_confirm already sends when the
    booking was first made, for a student who missed or dismissed it."""
    conn = get_db()
    sched = conn.execute("""SELECT sf.*, a.tag as plane_tag FROM scheduled_flights sf
                             JOIN assets a ON a.id = sf.asset_id WHERE sf.id = ?""", (scheduled_id,)).fetchone()
    if not sched or not sched["confirm_required"] or sched["confirmed_at"]:
        conn.close()
        return _pending_decision_response(False, "That flight isn't waiting on a confirmation.", "danger")
    student = conn.execute("SELECT user_id FROM students WHERE id = ?", (sched["student_id"],)).fetchone()
    if not student or not student["user_id"]:
        conn.close()
        return _pending_decision_response(False, "That student has no login to notify.", "danger")
    _notify_booking_confirm(conn, student["user_id"], scheduled_id, sched["plane_tag"], sched["scheduled_date"], sched["scheduled_time"])
    conn.close()
    return _pending_decision_response(True, "Reminder sent.", "success")


@flight_bp.route("/schedule/<int:scheduled_id>/confirm", methods=["POST"])
@login_required
def schedule_confirm_booking(scheduled_id):
    """The student's own OK on a flight a CFI/admin booked for them (see
    _booking_confirm_state in schedule_new) - only the student it's booked
    for can confirm it, so a CFI can't just confirm on their behalf and
    defeat the point of asking."""
    conn = get_db()
    sched = conn.execute("SELECT * FROM scheduled_flights WHERE id = ?", (scheduled_id,)).fetchone()
    if not sched or sched["student_id"] != session.get("student_id"):
        conn.close()
        flash("That flight isn't yours to confirm.", "danger")
        return redirect(url_for("flight.dashboard"))
    if not sched["confirmed_at"]:
        conn.execute("UPDATE scheduled_flights SET confirmed_at = ? WHERE id = ?", (now_iso(), scheduled_id))
        conn.commit()
        flash("Flight confirmed.", "success")
    conn.close()
    return redirect(request.referrer or url_for("flight.dashboard"))


@flight_bp.route("/schedule/<int:scheduled_id>/cancel", methods=["POST"])
@cfi_required
def schedule_cancel(scheduled_id):
    conn = get_db()
    conn.execute("UPDATE scheduled_flights SET status = 'cancelled' WHERE id = ?", (scheduled_id,))
    conn.commit()
    conn.close()
    flash("Scheduled flight cancelled.", "success")
    return redirect(request.referrer or url_for("flight.schedule_calendar"))


@flight_bp.route("/schedule/<int:scheduled_id>/request_change", methods=["POST"])
@login_required
def schedule_request_change(scheduled_id):
    """A student asking the school to change their own booking - unlike a
    CFI's Reschedule button, a student can't just move the booking
    themselves, so this only records their note and pings the CFI/admin to
    act on it (see _notify_schedule_request). Requires a comment so the
    school knows what to change."""
    conn = get_db()
    sched = conn.execute("SELECT * FROM scheduled_flights WHERE id = ?", (scheduled_id,)).fetchone()
    if not sched or sched["student_id"] != session.get("student_id"):
        conn.close()
        flash("That flight isn't yours to request a change on.", "danger")
        return redirect(url_for("flight.dashboard"))
    if sched["status"] != "scheduled":
        conn.close()
        flash("Only a still-scheduled flight can have a change requested.", "danger")
        return redirect(url_for("flight.dashboard"))
    note = (request.form.get("note") or "").strip()
    if not note:
        flash("Say what you'd like changed so the school knows what to do.", "danger")
        conn.close()
        return redirect(url_for("flight.dashboard"))
    conn.execute("UPDATE scheduled_flights SET change_request_note = ?, change_requested_at = ? WHERE id = ?",
                 (note, now_iso(), scheduled_id))
    conn.commit()
    _notify_schedule_request(conn, sched, "requested a change to", note)
    conn.close()
    flash("Your change request was sent to the school.", "success")
    return redirect(url_for("flight.dashboard"))


@flight_bp.route("/schedule/<int:scheduled_id>/change_request/dismiss", methods=["POST"])
@cfi_required
def schedule_change_request_dismiss(scheduled_id):
    """A CFI/admin clearing a student's change request off the Change
    Requests queue once it's been handled - by rescheduling the booking
    (schedule_reschedule clears it the same way) or by sorting it out some
    other way (a call, an in-person chat) that doesn't move the booking
    itself. Doesn't touch the booking otherwise."""
    conn = get_db()
    sched = conn.execute("SELECT change_requested_at FROM scheduled_flights WHERE id = ?", (scheduled_id,)).fetchone()
    if not sched or not sched["change_requested_at"]:
        conn.close()
        return _pending_decision_response(False, "That change request isn't there anymore.", "danger")
    conn.execute("UPDATE scheduled_flights SET change_request_note = NULL, change_requested_at = NULL WHERE id = ?",
                 (scheduled_id,))
    conn.commit()
    conn.close()
    return _pending_decision_response(True, "Change request dismissed.", "info")


@flight_bp.route("/schedule/<int:scheduled_id>/student_cancel", methods=["POST"])
@login_required
def schedule_student_cancel(scheduled_id):
    """A student cancelling their own booking - unlike the CFI/admin Cancel
    button (schedule_cancel), this requires a reason, and within 24 hours of
    the slot it also requires the cancellation-fee checkbox (enforced in the
    modal in _schedule_detail_modal.html, and re-checked here since a POST
    can always be sent by hand)."""
    conn = get_db()
    sched = conn.execute("SELECT * FROM scheduled_flights WHERE id = ?", (scheduled_id,)).fetchone()
    if not sched or sched["student_id"] != session.get("student_id"):
        conn.close()
        flash("That flight isn't yours to cancel.", "danger")
        return redirect(url_for("flight.dashboard"))
    if sched["status"] != "scheduled":
        conn.close()
        flash("That flight is already cancelled or has started.", "danger")
        return redirect(url_for("flight.dashboard"))
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Add a quick reason for the cancellation.", "danger")
        conn.close()
        return redirect(url_for("flight.dashboard"))
    if _within_24h_of_slot(sched["scheduled_date"], sched["scheduled_time"]) and request.form.get("ack_fee") != "on":
        flash("This flight is within 24 hours - check the box confirming you understand the cancellation charge before cancelling.", "danger")
        conn.close()
        return redirect(url_for("flight.dashboard"))
    conn.execute("UPDATE scheduled_flights SET status = 'cancelled', cancel_reason = ? WHERE id = ?",
                 (reason, scheduled_id))
    conn.commit()
    _notify_schedule_request(conn, sched, "cancelled", reason)
    conn.close()
    flash("Flight cancelled.", "success")
    return redirect(url_for("flight.dashboard"))


@flight_bp.route("/schedule/<int:scheduled_id>/start", methods=["POST"])
@login_required
def schedule_start(scheduled_id):
    """Starts the flight clock right from a scheduled booking: creates the
    flights row now (pre-filled from the booking, and from the plane's
    current Hobbs/Tach so there's less to type later), stamps started_at,
    and marks the booking in_progress so it drops off the upcoming list and
    stops blocking new bookings. The instructor finishes it later from the
    active-flight page, which is what stamps ended_at and computes the
    billable instructor time.

    Any CFI (not just the one assigned) or a master admin can start a
    booking, same as before - but a solo booking has no instructor at all,
    so the student flying it has to be able to press Start themselves too
    (see _can_end_flight, which grants them the same exception for ending
    it)."""
    conn = get_db()
    sched = conn.execute("SELECT * FROM scheduled_flights WHERE id = ?", (scheduled_id,)).fetchone()
    if not sched or sched["status"] != "scheduled":
        conn.close()
        flash("That booking is no longer available to start (already started, flown, or cancelled).", "danger")
        return redirect(url_for("flight.schedule_calendar"))
    if not (session.get("cfi_id") or session.get("is_master_admin")
            or (sched["solo"] and session.get("student_id") == sched["student_id"])):
        conn.close()
        flash("Only a CFI, an admin, or the student (on a solo flight) can start this flight.", "danger")
        return redirect(request.referrer or url_for("flight.schedule_calendar"))
    med_problem = _cfi_medical_problem(conn, sched["cfi_id"], date.today().isoformat())
    if med_problem:
        conn.close()
        flash("Not started - " + med_problem, "danger")
        return redirect(request.referrer or url_for("flight.schedule_calendar"))
    can_start, opens_at = _start_window(sched["scheduled_date"], sched["scheduled_time"])
    if not can_start:
        if request.form.get("move_to_now") != "1":
            # Normally the page's "move it to now?" prompt catches this first
            # (see _early_start_modal.html); this is the backstop for anything
            # that posts here without going through it.
            conn.close()
            flash(f"Not started - this flight is booked for {_slot_label(sched['scheduled_date'], sched['scheduled_time'])} "
                  "and can't be started before then. "
                  "If it's actually leaving now, press Start again and choose \"Move to now & start\".", "warning")
            return redirect(request.referrer or url_for("flight.schedule_calendar"))
        # The instructor confirmed it's really leaving now: move the booking
        # to the current time first (same conflict check as a reschedule) so
        # the calendar, conflicts and billing reflect when it actually flew.
        now = datetime.now()
        new_date = now.strftime("%Y-%m-%d")
        new_time = now.strftime("%H:%M")
        conflicts = _scheduling_conflicts(conn, sched["asset_id"], sched["cfi_id"], sched["student_id"],
                                          new_date, new_time, sched["duration_hours"], exclude_id=scheduled_id)
        if conflicts:
            for c in conflicts:
                flash(c["message"], "danger")
            flash("Not started - moving this flight to now would conflict with the booking above. "
                  "Sort that out (or reschedule it by hand), then start it.", "danger")
            conn.close()
            return redirect(_conflict_highlight_url(conflicts))
        conn.execute("UPDATE scheduled_flights SET scheduled_date = ?, scheduled_time = ? WHERE id = ?",
                     (new_date, new_time, scheduled_id))
        flash(f"Booking moved from {_slot_label(sched['scheduled_date'], sched['scheduled_time'])} "
              f"to now ({_format_time_12h(new_time)}).", "info")
    plane = conn.execute("SELECT * FROM assets WHERE id = ?", (sched["asset_id"],)).fetchone()
    # The clock starts right away - it no longer waits on Starting Hobbs/
    # Tach (every plain Start button posts here with neither). Whichever of
    # the two is still missing becomes a required field on the Log Flight
    # form once the flight ends (see log_active.html/log_end), so it's
    # never skipped, just no longer something you have to stop and type in
    # before the timer can begin.
    hobbs_start = _parse_float(request.form.get("hobbs_start"))
    tach_start = _parse_float(request.form.get("tach_start"))
    last_hobbs = plane["hobbs_hours"] if plane else None
    solo = 1 if not sched["cfi_id"] else 0
    cur = conn.execute("""INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, hobbs_start, tach_start,
                           solo, scheduled_flight_id, started_at, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (sched["cfi_id"], sched["student_id"], sched["asset_id"],
                        date.today().strftime("%Y-%m-%d"), hobbs_start,
                        tach_start, solo, scheduled_id, now_iso(), now_iso()))
    flight_id = cur.lastrowid
    if hobbs_start is not None and last_hobbs is not None and abs(hobbs_start - last_hobbs) >= 0.05:
        flash(f"Starting Hobbs {hobbs_start:.1f} doesn't match the last one on file for {plane['tag']} "
              f"({last_hobbs:.1f}) - {abs(hobbs_start - last_hobbs):.1f} hrs difference.", "warning")
    conn.execute("UPDATE flights SET guest_name = ? WHERE id = ?", (sched["guest_name"], flight_id))
    conn.execute("UPDATE scheduled_flights SET status = 'in_progress' WHERE id = ?", (scheduled_id,))
    conn.commit()
    conn.close()
    flash("Flight started - fill in the rest when you're done.", "success")
    return redirect(url_for("flight.log_active") + f"#active-flight-{flight_id}")


def _can_end_flight(flight_row):
    """Who's allowed to end a given active flight: a master admin, the CFI
    actually assigned to it, or - for a solo flight - the student flying
    it. Everyone logged in can now SEE the Active Flight board (it's
    school-wide), but ending someone else's flight for them is limited to
    the people who'd actually know how it went."""
    if session.get("is_master_admin"):
        return True
    my_cfi_id = session.get("cfi_id")
    if my_cfi_id and flight_row["cfi_id"] == my_cfi_id:
        return True
    my_student_id = session.get("student_id")
    if flight_row["solo"] and my_student_id and flight_row["student_id"] == my_student_id:
        return True
    return False


ETA_TURNAROUND_MIN = 15  # plane/instructor/student turnaround after a late flight lands


def _eta_impacts(conn, only_flight_id=None):
    """For every running flight that has an updated ETA (flights.eta_at, set
    from the Active Flight page - see log_set_eta), the later bookings that
    ETA runs into: still-booked flights that same day sharing the plane,
    the instructor or the student, starting before the new ETA plus
    ETA_TURNAROUND_MIN. Worked out live every time (nothing is stored on
    the affected bookings), so the flags go away on their own once the late
    flight ends, a new ETA clears them, or the booking starts/is cancelled.
    Returns {scheduled_flight_id: {"reason", "flight_id", "eta_label", "plane_tag", "shared"}}."""
    sql = """SELECT f.id, f.asset_id, f.cfi_id, f.student_id, f.eta_at, f.eta_set_by, f.started_at,
                    f.scheduled_flight_id, a.tag as plane_tag, c.name as cfi_name, COALESCE(NULLIF(f.guest_name, '') || ' (guest)', s.name) as student_name,
                    sf.scheduled_time as sched_time
             FROM flights f
             JOIN assets a ON a.id = f.asset_id
             JOIN students s ON s.id = f.student_id
             LEFT JOIN cfis c ON c.id = f.cfi_id
             LEFT JOIN scheduled_flights sf ON sf.id = f.scheduled_flight_id
             WHERE f.started_at IS NOT NULL AND f.ended_at IS NULL AND f.stopped_at IS NULL AND f.eta_at IS NOT NULL"""
    params = []
    if only_flight_id:
        sql += " AND f.id = ?"
        params.append(only_flight_id)
    impacts = {}
    for lf in conn.execute(sql, params).fetchall():
        try:
            eta = datetime.strptime(lf["eta_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        eta_date = eta.strftime("%Y-%m-%d")
        eta_min = eta.hour * 60 + eta.minute + ETA_TURNAROUND_MIN
        # Only bookings after this flight's own slot - an earlier one on the
        # same day isn't something this flight's delay can push back.
        own = _time_window(lf["sched_time"], 0)
        if own:
            from_min = own[0]
        else:
            st = datetime.strptime(lf["started_at"], "%Y-%m-%d %H:%M:%S")
            from_min = st.hour * 60 + st.minute if st.strftime("%Y-%m-%d") == eta_date else 0
        eta_label = _format_time_12h(eta.strftime("%H:%M"))
        rows = conn.execute("""SELECT id, asset_id, cfi_id, student_id, scheduled_time FROM scheduled_flights
                               WHERE status = 'scheduled' AND scheduled_date = ? AND id IS NOT ?
                                 AND (asset_id = ? OR student_id = ? OR (cfi_id IS NOT NULL AND cfi_id = ?))""",
                            (eta_date, lf["scheduled_flight_id"], lf["asset_id"], lf["student_id"], lf["cfi_id"])).fetchall()
        for r in rows:
            w = _time_window(r["scheduled_time"], 0)
            if not w or w[0] < from_min or w[0] >= eta_min:
                continue
            shared = []
            if r["asset_id"] == lf["asset_id"]:
                shared.append(f"plane {lf['plane_tag']}")
            if lf["cfi_id"] and r["cfi_id"] == lf["cfi_id"]:
                shared.append(f"instructor {lf['cfi_name']}")
            if r["student_id"] == lf["student_id"]:
                shared.append(f"student {lf['student_name']}")
            reason = (f"{lf['plane_tag']} is running late - new ETA {eta_label}"
                      f"{' (from ' + lf['eta_set_by'] + ')' if lf['eta_set_by'] else ''}. "
                      f"Same {', '.join(shared)} - this booking may start late.")
            impacts.setdefault(r["id"], {"reason": reason, "flight_id": lf["id"], "eta_label": eta_label,
                                         "plane_tag": lf["plane_tag"], "shared": shared})
    return impacts


def _can_ack_overdue(flight_row):
    """Who can tap Acknowledge on a flight's 'running late' banner: anyone
    who could end it (see _can_end_flight), plus ANY CFI - not just the one
    assigned - since any instructor on the field can check on a late plane
    and quiet the alert. Master admins are already covered by
    _can_end_flight."""
    if _can_end_flight(flight_row):
        return True
    return bool(session.get("cfi_id") or session.get("flight_role") == "cfi")


def _session_status(f):
    """For an ACTIVE flight row (started, not ended) that has a scheduled
    duration to compare against (scheduled_duration_hours, from the joined
    scheduled_flights - _LOG_ROW_SQL already carries this): returns elapsed/
    remaining seconds and whether it's 15+ min overdue right now. Returns
    None for a flight with no scheduled block (nothing to be "on time"
    against). Shared by the Active Flight board (red outline) and the
    periodic session-alert check (check_session_alerts, below) so both
    agree on what "overdue" means and how pause time factors in."""
    duration_hours = f["scheduled_duration_hours"]
    if not duration_hours or not f["started_at"]:
        return None
    started = datetime.strptime(f["started_at"], "%Y-%m-%d %H:%M:%S")
    now = datetime.strptime(now_iso(), "%Y-%m-%d %H:%M:%S")
    paused_seconds = f["paused_seconds"] or 0
    if f["paused_at"]:
        paused_started = datetime.strptime(f["paused_at"], "%Y-%m-%d %H:%M:%S")
        paused_seconds += max(0, (now - paused_started).total_seconds())
    elapsed = max(0.0, (now - started).total_seconds() - paused_seconds)
    remaining = duration_hours * 3600 - elapsed
    snoozed = False
    if f["overdue_acknowledged_at"]:
        ack = datetime.strptime(f["overdue_acknowledged_at"], "%Y-%m-%d %H:%M:%S")
        snoozed = (now - ack).total_seconds() < 900
    # An updated ETA (log_set_eta) keeps the running-late alert quiet until
    # that time passes - after that it's overdue again like normal.
    eta_at = f["eta_at"] if "eta_at" in f.keys() else None
    if eta_at:
        try:
            if now < datetime.strptime(eta_at, "%Y-%m-%d %H:%M:%S"):
                snoozed = True
        except ValueError:
            pass
    return {
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "overdue": remaining <= -900 and not snoozed,
    }


def check_session_alerts():
    """Periodic (roughly once a minute) check across every active flight
    tied to a scheduled block: pushes a "30 min left" alert as it
    approaches the end of that block, a "time's up" alert once it's
    reached, and a repeating (every 15 min, unless acknowledged) "running
    late" alert once it's 15+ min past - to the assigned CFI and the
    student, whichever of them opted in to that particular alert
    (notify_push_30min / notify_push_timeup / notify_push_late, each its
    own checkbox on My Account) and has a device subscribed. Started from a background thread in app.py;
    each call opens/closes its own connection since it runs off the
    request cycle."""
    conn = get_db()
    try:
        rows = conn.execute(_LOG_ROW_SQL + """
            WHERE f.started_at IS NOT NULL AND f.ended_at IS NULL AND f.stopped_at IS NULL AND sf.duration_hours IS NOT NULL
        """).fetchall()
        for f in rows:
            status = _session_status(f)
            if not status:
                continue
            recipient_user_ids = []
            cfi_row = conn.execute("SELECT user_id FROM cfis WHERE id = ?", (f["cfi_id"],)).fetchone() if f["cfi_id"] else None
            if cfi_row and cfi_row["user_id"]:
                recipient_user_ids.append(cfi_row["user_id"])
            student_row = conn.execute("SELECT user_id FROM students WHERE id = ?", (f["student_id"],)).fetchone()
            if student_row and student_row["user_id"]:
                recipient_user_ids.append(student_row["user_id"])
            # Per-alert opt-ins: user id -> row with notify_push_30min /
            # notify_push_timeup / notify_push_late.
            prefs = {}
            for uid in recipient_user_ids:
                u = conn.execute("SELECT id, notify_push_30min, notify_push_timeup, notify_push_late "
                                  "FROM users WHERE id = ? AND active = 1", (uid,)).fetchone()
                if u:
                    prefs[u["id"]] = u
            warn_ids = [uid for uid, u in prefs.items() if u["notify_push_30min"]]
            timeup_ids = [uid for uid, u in prefs.items() if u["notify_push_timeup"]]
            late_ids = [uid for uid, u in prefs.items() if u["notify_push_late"]]

            remaining = status["remaining_seconds"]
            plane_bit = f["plane_tag"] or "your plane"
            if 0 < remaining <= 1800 and not f["session_warning_sent_at"]:
                for uid in warn_ids:
                    push.queue_and_push(conn, uid, "30 minutes left",
                                         f"{plane_bit} - your scheduled flight time is almost up.",
                                         tag=f"flight-{f['id']}-warning", url=f"/flight/log/active#active-flight-{f['id']}")
                conn.execute("UPDATE flights SET session_warning_sent_at = ? WHERE id = ?", (now_iso(), f["id"]))
                conn.commit()
            elif remaining <= 0 and not f["session_expired_sent_at"]:
                for uid in timeup_ids:
                    push.queue_and_push(conn, uid, "Flight time is up",
                                         f"{plane_bit} - your scheduled block has ended.",
                                         tag=f"flight-{f['id']}-expired", url=f"/flight/log/active#active-flight-{f['id']}")
                conn.execute("UPDATE flights SET session_expired_sent_at = ? WHERE id = ?", (now_iso(), f["id"]))
                conn.commit()

            resend_due = True
            if f["overdue_alert_sent_at"]:
                last_sent = datetime.strptime(f["overdue_alert_sent_at"], "%Y-%m-%d %H:%M:%S")
                resend_due = (datetime.strptime(now_iso(), "%Y-%m-%d %H:%M:%S") - last_sent).total_seconds() >= 900
            if status["overdue"] and resend_due:
                # Only the instructor needs to act (close it or acknowledge
                # it's fine) - not the student. For a solo flight, fall back
                # to master admins so someone still gets nudged. Repeats
                # every 15 min (like the acknowledge snooze) rather than
                # once, since a running-late flight stays worth flagging.
                overdue_recipients = []
                if f["cfi_id"] and cfi_row and cfi_row["user_id"] and cfi_row["user_id"] in late_ids:
                    overdue_recipients.append(cfi_row["user_id"])
                elif f["solo"]:
                    admins = conn.execute(
                        "SELECT id FROM users WHERE active = 1 AND is_master_admin = 1 AND notify_push_late = 1"
                    ).fetchall()
                    overdue_recipients.extend(a["id"] for a in admins)
                for uid in overdue_recipients:
                    push.queue_and_push(conn, uid, "Flight is running late",
                                         f"{plane_bit} is more than 15 min past its scheduled block - close it or acknowledge.",
                                         tag=f"flight-{f['id']}-overdue", url=f"/flight/log/active#active-flight-{f['id']}")
                conn.execute("UPDATE flights SET overdue_alert_sent_at = ? WHERE id = ?", (now_iso(), f["id"]))
                conn.commit()
    finally:
        conn.close()


@flight_bp.route("/log/active")
@login_required
def log_active():
    """Shows every currently-running flight (started, not yet ended) school-
    wide - anyone logged in can see the whole board, including a student who
    has a flight of their own going (it used to filter down to just their
    own flight after starting/pausing/etc., which meant they couldn't see
    anyone else's was up). Only the assigned CFI, the student themselves (if
    solo), or a master admin can actually end one (see _can_end_flight /
    can_end on each row, used by the template to show or hide the End
    Flight form). A leftover ?flight_id= just scrolls to that card (see
    #active-flight-<id> handling in log_active.html) instead of hiding
    everything else."""
    conn = get_db()
    rows = conn.execute(_LOG_ROW_SQL + """ WHERE f.started_at IS NOT NULL AND f.ended_at IS NULL
                         ORDER BY f.started_at""").fetchall()
    impacts = _eta_impacts(conn)
    affected = {}
    if impacts:
        for b in conn.execute(_SCHEDULE_ROW_SQL + " AND sf.id IN (%s)" % ",".join("?" * len(impacts)),
                              list(impacts.keys())).fetchall():
            b = _decorate_schedule_row(b)
            affected.setdefault(impacts[b["id"]]["flight_id"], []).append(
                dict(b, shared=impacts[b["id"]]["shared"]))
        for lst in affected.values():
            lst.sort(key=lambda b: b["scheduled_time"] or "")
    conn.close()
    flights = []
    for r in rows:
        eta_label = None
        if r["eta_at"]:
            try:
                eta_label = _format_time_12h(r["eta_at"][11:16])
            except (TypeError, ValueError):
                eta_label = None
        # _row_with_cost() resolves this flight's effective plane_rate and
        # instructor_rate (and a total, though that's 0 until Hobbs End is
        # in) - the End Flight form needs the two rates as data-* so its own
        # JS can total the price live as Hobbs/ground/solo are typed,
        # instead of only finding out the total once Log Flight is pressed.
        flights.append(dict(_row_with_cost(r), can_end=_can_end_flight(r), can_ack=_can_ack_overdue(r),
                            session_status=_session_status(r), eta_label=eta_label,
                            eta_affected=affected.get(r["id"], [])))
    # Still-flying flights above ones whose clock has already been stopped
    # and are just waiting on End Flight's form to be filled in and
    # submitted - those two states used to interleave by start time, mixing
    # "still up" with "already down, needs paperwork". Within each group,
    # whoever's logged in sees their own flight (as the assigned CFI or the
    # solo student) first - easy to lose track of your own flight on a busy
    # board, especially for an admin who's also instructing.
    my_cfi_id = session.get("cfi_id")
    my_student_id = session.get("student_id")
    def _board_sort_key(f):
        is_stopped = 1 if f["stopped_at"] else 0
        is_mine = 0 if ((my_cfi_id and f["cfi_id"] == my_cfi_id) or
                        (my_student_id and f["student_id"] == my_student_id)) else 1
        return (is_stopped, is_mine, f["started_at"] or "")
    flights.sort(key=_board_sort_key)
    # Inline "it worked" note on the card just acted on - the page's flash
    # messages sit at the very top, out of sight on a phone once the
    # redirect scrolls down to the card (see log_ack_overdue / log_set_eta).
    return render_template("flight/log_active.html", flights=flights, eta_turnaround=ETA_TURNAROUND_MIN,
                           acked_id=request.args.get("acked", type=int),
                           eta_saved_id=request.args.get("eta_saved", type=int))


@flight_bp.route("/log/<int:flight_id>/ack_overdue", methods=["POST"])
@login_required
def log_ack_overdue(flight_id):
    """Instructor taps Acknowledge on the 'running late' banner - snoozes
    the red outline and further overdue pushes for 15 minutes (see
    _session_status). Anyone who can end the flight, plus any CFI or
    admin, can acknowledge it (see _can_ack_overdue)."""
    conn = get_db()
    f = conn.execute("SELECT * FROM flights WHERE id = ?", (flight_id,)).fetchone()
    if not f or not f["started_at"] or f["ended_at"]:
        conn.close()
        flash("That flight isn't currently running.", "danger")
        return redirect(url_for("flight.log_active"))
    if not _can_ack_overdue(f):
        conn.close()
        flash("Only a CFI, an admin, or the student (on a solo flight) can acknowledge this.", "danger")
        return redirect(url_for("flight.log_active"))
    conn.execute("UPDATE flights SET overdue_acknowledged_at = ? WHERE id = ?", (now_iso(), flight_id))
    conn.commit()
    conn.close()
    return redirect(url_for("flight.log_active", flight_id=flight_id, acked=flight_id) + f"#active-flight-{flight_id}")


@flight_bp.route("/log/<int:flight_id>/eta", methods=["POST"])
@login_required
def log_set_eta(flight_id):
    """A late flight's instructor (or any CFI/admin, or the solo student -
    same people who can acknowledge, see _can_ack_overdue) puts in an
    updated ETA: either a time (eta_time, HH:MM today) or one of the quick
    "+N min" buttons (eta_minutes). Also quiets the running-late alert
    until then. Later bookings that ETA runs into (same plane, instructor
    or student - see _eta_impacts) get flagged on the Schedule and the
    dashboard, and their CFI/student are told automatically (push, plus
    email/text if they get flight reminders) to still arrive as normal
    unless someone reaches out."""
    conn = get_db()
    f = conn.execute("SELECT * FROM flights WHERE id = ?", (flight_id,)).fetchone()
    back = url_for("flight.log_active", flight_id=flight_id) + f"#active-flight-{flight_id}"
    if not f or not f["started_at"] or f["ended_at"]:
        conn.close()
        flash("That flight isn't currently running.", "danger")
        return redirect(url_for("flight.log_active"))
    if not _can_ack_overdue(f):
        conn.close()
        flash("Only a CFI, an admin, or the student (on a solo flight) can update this flight's ETA.", "danger")
        return redirect(back)
    now = datetime.strptime(now_iso(), "%Y-%m-%d %H:%M:%S")
    eta = None
    minutes = _parse_int(request.form.get("eta_minutes"))
    eta_time = (request.form.get("eta_time") or "").strip()
    if minutes:
        eta = now + timedelta(minutes=minutes)
    elif eta_time:
        try:
            h, m = (int(x) for x in eta_time.split(":")[:2])
            eta = now.replace(hour=h, minute=m, second=0, microsecond=0)
        except ValueError:
            eta = None
    if request.form.get("clear"):
        conn.execute("UPDATE flights SET eta_at = NULL, eta_set_by = NULL WHERE id = ?", (flight_id,))
        conn.commit()
        conn.close()
        flash("Updated ETA cleared.", "success")
        return redirect(back)
    if not eta or eta <= now:
        conn.close()
        flash("Pick an ETA later than now.", "danger")
        return redirect(back)
    eta = eta.replace(second=0, microsecond=0)
    conn.execute("UPDATE flights SET eta_at = ?, eta_set_by = ?, overdue_acknowledged_at = ? WHERE id = ?",
                 (eta.strftime("%Y-%m-%d %H:%M:%S"), session.get("user_name"), now_iso(), flight_id))
    conn.commit()

    impacts = _eta_impacts(conn, only_flight_id=flight_id)
    eta_label = _format_time_12h(eta.strftime("%H:%M"))
    if impacts:
        rows = conn.execute(_SCHEDULE_ROW_SQL + " AND sf.id IN (%s)" % ",".join("?" * len(impacts)),
                            list(impacts.keys())).fetchall()
        # Automatic heads-up to the people on each affected booking (its CFI
        # and student) - no opt-in needed, since it's about their own
        # upcoming flight: a phone push always, plus email/text for anyone
        # with flight reminders turned on (same channels as the day-before
        # reminder). The wording tells them to still show up at the normal
        # time unless someone reaches out, so nobody skips a flight on a
        # guess. Sent once per booking per ETA (notification_log) - a new
        # ETA sends an update; re-saving the same one doesn't repeat it. The
        # person who entered the ETA isn't notified about their own update.
        me = session.get("user_id")
        emails = []
        for b in rows:
            eta_key = eta.strftime("%H:%M")
            if notify.already_notified_today(conn, "eta_delay", b["id"], eta_key):
                continue
            uids = []
            if b["cfi_id"]:
                cu = conn.execute("SELECT user_id FROM cfis WHERE id = ?", (b["cfi_id"],)).fetchone()
                if cu and cu["user_id"]:
                    uids.append(cu["user_id"])
            su = conn.execute("SELECT user_id FROM students WHERE id = ?", (b["student_id"],)).fetchone()
            if su and su["user_id"] and su["user_id"] not in uids:
                uids.append(su["user_id"])
            when = _format_time_12h(b["scheduled_time"])
            late_plane = impacts[b["id"]]["plane_tag"]
            title = "Heads up: your flight may start late"
            body = (f"{late_plane} is running late - new ETA {eta_label}. Your {when} {b['plane_tag']} flight "
                    f"today may start a little late. Please still arrive at your normal time unless "
                    f"someone reaches out to you.")
            for uid in uids:
                if uid == me:
                    continue
                u = conn.execute("SELECT * FROM users WHERE id = ? AND active = 1", (uid,)).fetchone()
                if not u:
                    continue
                try:
                    push.queue_and_push(conn, uid, title, body, tag=f"eta-{b['id']}",
                                        url=url_for("flight.schedule_calendar", view="day"))
                except Exception:
                    current_app.logger.exception("ETA push failed")
                if u["notify_flight_reminders"]:
                    emails.append((u, f"Your {when} flight may start late", body))
            notify.log_notification(conn, "eta_delay", b["id"], eta_key)
        if emails:
            settings = notify.get_settings(conn)

            def _send(batch=emails, settings=settings):
                for u, subject, body in batch:
                    try:
                        notify.notify_user(settings, u, subject, body, brand="Fly with Kate!")
                    except Exception:
                        pass
            # Email/SMS can take a few seconds each - don't hold up the page.
            threading.Thread(target=_send, daemon=True).start()
        conn.commit()
        flash(f"ETA updated to {eta_label}. {len(impacts)} later booking{'s' if len(impacts) != 1 else ''} "
              f"may be affected - flagged on the Schedule and dashboard.", "warning")
    else:
        flash(f"ETA updated to {eta_label}. No later bookings are affected.", "success")
    conn.close()
    return redirect(url_for("flight.log_active", flight_id=flight_id, eta_saved=flight_id) + f"#active-flight-{flight_id}")


# ---------------------------------------------------------------------------
# Phone push notifications - subscribe/unsubscribe a device, and the
# service worker's "what's new for me" fetch. See push.py + static/push-sw.js.
# ---------------------------------------------------------------------------

@flight_bp.route("/push/vapid_public_key")
@login_required
def push_vapid_public_key():
    conn = get_db()
    _priv, pub_b64 = push.get_or_create_vapid_keys(conn)
    conn.close()
    return jsonify({"key": pub_b64})


@flight_bp.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    keys = data.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not endpoint or not p256dh or not auth:
        return jsonify({"error": "Incomplete subscription."}), 400
    conn = get_db()
    conn.execute("""INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES (?, ?, ?, ?)
                     ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth""",
                 (session["user_id"], endpoint, p256dh, auth))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@flight_bp.route("/push/unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    conn = get_db()
    if endpoint:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?", (endpoint, session["user_id"]))
    else:
        conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (session["user_id"],))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@flight_bp.route("/push/pending")
@login_required
def push_pending():
    """Polled by the service worker when a (payload-less) push wakes it -
    returns and clears this user's queued alerts."""
    conn = get_db()
    # Only the last half hour - anything older is stale (a "30 minutes
    # left" from yesterday is just noise) and is dropped instead. Alerts
    # normally arrive inside the push itself now; this queue is the backup.
    conn.execute("DELETE FROM push_pending WHERE user_id = ? AND created_at < datetime('now', '-30 minutes')",
                 (session["user_id"],))
    conn.commit()
    rows = conn.execute("SELECT * FROM push_pending WHERE user_id = ? ORDER BY id", (session["user_id"],)).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        conn.execute(f"DELETE FROM push_pending WHERE id IN ({','.join('?' for _ in ids)})", ids)
        conn.commit()
    conn.close()
    return jsonify({"alerts": [dict(r) for r in rows]})


@flight_bp.route("/api/forecast")
@login_required
def api_forecast():
    """JSON forecast (Open-Meteo, ZIP 12458) for one scheduled lesson's
    date/time - fetched on demand when its calendar block is clicked, not
    prefetched for every block on the month. ?date=YYYY-MM-DD required,
    ?time=HH:MM optional (defaults to midday if the booking has no time)."""
    date_str = request.args.get("date", "").strip()
    time_str = request.args.get("time", "").strip() or None
    if not date_str:
        return jsonify({"error": "Missing date."}), 400
    conn = get_db()
    try:
        payload = weather.get_forecast_for(conn, date_str, time_str)
    except Exception:
        payload = {"date": date_str, "forecast": None, "error": "Couldn't reach the weather service."}
    conn.close()
    return jsonify(payload)


@flight_bp.route("/api/live_positions")
@cfi_required
def api_live_positions():
    """JSON for the Active Flight live map. ?mode=school (default): only
    currently-flying school planes with a hex on file. ?mode=all: every
    aircraft OpenSky reports within 25nm of N89, school planes flagged.
    Polled from log_active.html - see adsb.py."""
    mode = request.args.get("mode", "school").strip().lower()
    if mode not in ("school", "all"):
        mode = "school"
    conn = get_db()
    try:
        positions = adsb.get_live_positions(conn, mode=mode)
    except Exception:
        positions = []
    conn.close()
    resp = jsonify(positions)
    # Some browsers (notably Safari/iOS, which several stations use to view
    # this page) will silently serve a fetch() GET from their HTTP cache if
    # the response carries no explicit cache directive, which looks exactly
    # like "the map loaded but nothing ever moves" - block that outright.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@flight_bp.route("/api/live_positions/track")
@cfi_required
def api_live_track():
    """Breadcrumb trail for one plane (by ICAO24 hex), for the 'search a
    plane -> highlight it and show where it's been' feature on the Active
    Flight map. Only school planes get a recorded trail (see adsb.py)."""
    icao24 = request.args.get("icao24", "").strip().lower()
    conn = get_db()
    try:
        points = adsb.get_track(conn, icao24) if icao24 else []
    except Exception:
        points = []
    conn.close()
    resp = jsonify(points)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@flight_bp.route("/log/<int:flight_id>/pause", methods=["POST"])
@login_required
def log_pause(flight_id):
    """Pauses the running clock on an active flight (e.g. a weather hold or
    a break) - excluded from elapsed/instructor-clock time once resumed or
    ended. Same permission as ending the flight."""
    conn = get_db()
    f = conn.execute("SELECT * FROM flights WHERE id = ?", (flight_id,)).fetchone()
    if not f or not f["started_at"] or f["ended_at"]:
        conn.close()
        flash("That flight isn't currently running.", "danger")
        return redirect(url_for("flight.log_active"))
    if not _can_end_flight(f):
        conn.close()
        flash("Only the assigned instructor, the student (on a solo flight), or a master admin can pause this flight. (The Maintenance Admin role does not count - ask a master admin to check the 'Master Admin' box for that account.)", "danger")
        return redirect(url_for("flight.log_active"))
    if not f["paused_at"]:
        conn.execute("UPDATE flights SET paused_at = ? WHERE id = ?", (now_iso(), flight_id))
        conn.commit()
        flash("Flight paused.", "success")
    conn.close()
    return redirect(url_for("flight.log_active") + f"#active-flight-{flight_id}")


@flight_bp.route("/log/<int:flight_id>/resume", methods=["POST"])
@login_required
def log_resume(flight_id):
    """Resumes a paused flight, folding the paused span into paused_seconds
    so it's excluded from elapsed/instructor-clock time."""
    conn = get_db()
    f = conn.execute("SELECT * FROM flights WHERE id = ?", (flight_id,)).fetchone()
    if not f or not f["started_at"] or f["ended_at"]:
        conn.close()
        flash("That flight isn't currently running.", "danger")
        return redirect(url_for("flight.log_active"))
    if not _can_end_flight(f):
        conn.close()
        flash("Only the assigned instructor, the student (on a solo flight), or a master admin can resume this flight. (The Maintenance Admin role does not count - ask a master admin to check the 'Master Admin' box for that account.)", "danger")
        return redirect(url_for("flight.log_active"))
    if f["paused_at"]:
        paused_started = datetime.strptime(f["paused_at"], "%Y-%m-%d %H:%M:%S")
        added = max(0, (datetime.strptime(now_iso(), "%Y-%m-%d %H:%M:%S") - paused_started).total_seconds())
        conn.execute("UPDATE flights SET paused_at = NULL, paused_seconds = paused_seconds + ? WHERE id = ?",
                     (int(added), flight_id))
        conn.commit()
        flash("Flight resumed.", "success")
    conn.close()
    return redirect(url_for("flight.log_active") + f"#active-flight-{flight_id}")


@flight_bp.route("/log/<int:flight_id>/update_progress", methods=["POST"])
@login_required
def log_update_progress(flight_id):
    """Lets the CFI fill in starting Hobbs/Tach, oil added, and a note
    (flagged as a squawk the same as at End Flight) while the flight is
    still running - so it doesn't all have to happen in a rush once the
    clock stops. Only fills in a reading if it isn't already on file, same
    rule End Flight itself uses; oil and notes just overwrite with
    whatever's typed here."""
    conn = get_db()
    f = conn.execute("SELECT * FROM flights WHERE id = ?", (flight_id,)).fetchone()
    if not f or not f["started_at"] or f["ended_at"]:
        conn.close()
        flash("That flight isn't currently running.", "danger")
        return redirect(url_for("flight.log_active"))
    if not _can_end_flight(f):
        conn.close()
        flash("Only the assigned instructor, the student (on a solo flight), or a master admin can update this flight. (The Maintenance Admin role does not count - ask a master admin to check the 'Master Admin' box for that account.)", "danger")
        return redirect(url_for("flight.log_active"))
    hobbs_start = _parse_float(request.form.get("hobbs_start")) if f["hobbs_start"] is None else f["hobbs_start"]
    tach_start = _parse_float(request.form.get("tach_start")) if f["tach_start"] is None else f["tach_start"]
    oil_added_qt = _parse_float(request.form.get("oil_added_qt"))
    notes = request.form.get("notes", "").strip()
    conn.execute("UPDATE flights SET hobbs_start=?, tach_start=?, oil_added_qt=?, notes=? WHERE id=?",
                 (hobbs_start, tach_start, oil_added_qt, notes, flight_id))
    conn.commit()
    conn.close()
    flash("Flight updated.", "success")
    return redirect(url_for("flight.log_active", flight_id=flight_id) + f"#active-flight-{flight_id}")


@flight_bp.route("/log/<int:flight_id>/stop", methods=["POST"])
@login_required
def log_stop(flight_id):
    """End Flight, step 1: stops the clock now (so billed instructor time
    ends here), but leaves the flight on the Active Flight board until the
    rest is filled in - Hobbs end and paid/unpaid are required - by
    log_end, which is what logs it into Flight History."""
    conn = get_db()
    f = conn.execute("SELECT * FROM flights WHERE id = ?", (flight_id,)).fetchone()
    if not f or not f["started_at"] or f["ended_at"]:
        conn.close()
        flash("That flight isn't currently running.", "danger")
        return redirect(url_for("flight.log_active"))
    if not _can_end_flight(f):
        conn.close()
        flash("Only the assigned instructor, the student (on a solo flight), or a master admin can end this flight. (The Maintenance Admin role does not count - ask a master admin to check the 'Master Admin' box for that account.)", "danger")
        return redirect(url_for("flight.log_active"))
    if not f["stopped_at"]:
        stopped_at = now_iso()
        paused_seconds = f["paused_seconds"] or 0
        if f["paused_at"]:
            paused_seconds += max(0, int((datetime.strptime(stopped_at, "%Y-%m-%d %H:%M:%S")
                                          - datetime.strptime(f["paused_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()))
        conn.execute("UPDATE flights SET stopped_at = ?, paused_at = NULL, paused_seconds = ? WHERE id = ?",
                     (stopped_at, paused_seconds, flight_id))
        conn.commit()
    conn.close()
    flash("Clock stopped. Fill in the ending Hobbs and paid / unpaid below to log the flight.", "info")
    return redirect(url_for("flight.log_active", flight_id=flight_id) + f"#active-flight-{flight_id}")


@flight_bp.route("/log/<int:flight_id>/restart_clock", methods=["POST"])
@login_required
def log_restart_clock(flight_id):
    """Undo an End Flight pressed by mistake: the clock picks up again, and
    the stopped time counts as paused (not billed)."""
    conn = get_db()
    f = conn.execute("SELECT * FROM flights WHERE id = ?", (flight_id,)).fetchone()
    if f and f["stopped_at"] and not f["ended_at"] and _can_end_flight(f):
        extra = max(0, int((datetime.now() - datetime.strptime(f["stopped_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()))
        conn.execute("UPDATE flights SET stopped_at = NULL, paused_seconds = COALESCE(paused_seconds, 0) + ? WHERE id = ?",
                     (extra, flight_id))
        conn.commit()
        flash("Clock running again.", "success")
    conn.close()
    return redirect(url_for("flight.log_active", flight_id=flight_id) + f"#active-flight-{flight_id}")


@flight_bp.route("/log/<int:flight_id>/end", methods=["POST"])
@login_required
def log_end(flight_id):
    """Stops the clock on a flight started from the schedule: fills in the
    rest (Hobbs/Tach end, oil, ground time, notes), stamps ended_at, and
    computes instructor_clock_hours from started_at to now - that elapsed
    time is what the instructor gets billed for, covering pre/post-flight
    time with the student rather than just time in the air.

    Now that the Active Flight board is visible school-wide, this is
    server-side gated too (not just a hidden button) - see _can_end_flight:
    the assigned CFI, the student themselves on a solo flight, or a master
    admin."""
    conn = get_db()
    f = conn.execute("SELECT * FROM flights WHERE id = ?", (flight_id,)).fetchone()
    if not f or not f["started_at"] or f["ended_at"]:
        conn.close()
        flash("That flight isn't currently running.", "danger")
        return redirect(url_for("flight.log_active"))
    if not _can_end_flight(f):
        conn.close()
        flash("Only the assigned instructor, the student (on a solo flight), or a master admin can end this flight. (The Maintenance Admin role does not count - ask a master admin to check the 'Master Admin' box for that account.)", "danger")
        return redirect(url_for("flight.log_active"))

    hobbs_end = _parse_float(request.form.get("hobbs_end"))
    tach_end = _parse_float(request.form.get("tach_end"))
    back = url_for("flight.log_active", flight_id=flight_id) + f"#active-flight-{flight_id}"
    if hobbs_end is None or tach_end is None:
        conn.close()
        flash("Enter the ending Hobbs and Tach to log the flight.", "danger")
        return redirect(back)
    # Starting Hobbs/Tach are only still missing on a flight whose clock was
    # allowed to start before they were entered (see flight.schedule_start)
    # - the Log Flight form makes them required inputs in that case instead
    # of the usual disabled "already on file" display, so they're required
    # here too rather than left to end up NULL.
    hobbs_start = f["hobbs_start"] if f["hobbs_start"] is not None else _parse_float(request.form.get("hobbs_start"))
    tach_start = f["tach_start"] if f["tach_start"] is not None else _parse_float(request.form.get("tach_start"))
    if hobbs_start is None or tach_start is None:
        conn.close()
        flash("Enter the starting Hobbs and Tach to log the flight.", "danger")
        return redirect(back)
    if hobbs_end < hobbs_start:
        conn.close()
        flash("Ending Hobbs can't be less than starting Hobbs.", "danger")
        return redirect(back)
    if tach_end < tach_start:
        conn.close()
        flash("Ending Tach can't be less than starting Tach.", "danger")
        return redirect(back)
    paid_choice = request.form.get("paid")
    if paid_choice not in ("0", "1"):
        conn.close()
        flash("Pick Paid or Unpaid to log the flight.", "danger")
        return redirect(back)
    payment_method = (request.form.get("payment_method") or "").strip()[:40] or None
    payment_amount = max(0.0, _parse_float(request.form.get("payment_amount")) or 0.0)
    credit_requested = max(0.0, _parse_float(request.form.get("credit_applied")) or 0.0)

    oil_added_qt = _parse_float(request.form.get("oil_added_qt"))
    ground_time_hours = _parse_float(request.form.get("ground_time_hours"))
    notes = request.form.get("notes", "").strip()
    squawk = 1 if notes else 0
    day_landings_fs = _parse_int(request.form.get("day_landings_fs"))
    day_landings_tg = _parse_int(request.form.get("day_landings_tg"))
    night_landings_fs = _parse_int(request.form.get("night_landings_fs"))
    night_landings_tg = _parse_int(request.form.get("night_landings_tg"))

    # The clock stopped when End Flight was pressed (log_stop), not when
    # the details got filled in - bill up to then.
    ended_at = f["stopped_at"] or now_iso()
    started = datetime.strptime(f["started_at"], "%Y-%m-%d %H:%M:%S")
    ended = datetime.strptime(ended_at, "%Y-%m-%d %H:%M:%S")
    paused_seconds = f["paused_seconds"] or 0
    if f["paused_at"]:
        # Still paused when Ended was pressed - count time up to now as paused too.
        paused_started = datetime.strptime(f["paused_at"], "%Y-%m-%d %H:%M:%S")
        paused_seconds += max(0, (ended - paused_started).total_seconds())
    elapsed_seconds = max(0.0, (ended - started).total_seconds() - paused_seconds)
    instructor_clock_hours = elapsed_seconds / 3600.0

    conn.execute("""UPDATE flights SET hobbs_start=?, hobbs_end=?, tach_start=?, tach_end=?, oil_added_qt=?, ground_time_hours=?, notes=?,
                     squawk=?, ended_at=?, instructor_clock_hours=?, paused_at=NULL, paused_seconds=?,
                     day_landings_fs=?, day_landings_tg=?, night_landings_fs=?, night_landings_tg=?, paid=? WHERE id=?""",
                 (hobbs_start, hobbs_end, tach_start, tach_end, oil_added_qt, ground_time_hours, notes, squawk,
                  ended_at, instructor_clock_hours, paused_seconds,
                  day_landings_fs, day_landings_tg, night_landings_fs, night_landings_tg, int(paid_choice), flight_id))
    if hobbs_end is not None:
        conn.execute("UPDATE assets SET hobbs_hours = ?, hobbs_updated_at = ? WHERE id = ?",
                     (hobbs_end, now_iso(), f["asset_id"]))
    if tach_end is not None:
        conn.execute("UPDATE assets SET tach_hours = ?, tach_updated_at = ? WHERE id = ?",
                     (tach_end, now_iso(), f["asset_id"]))
    if f["scheduled_flight_id"]:
        conn.execute("UPDATE scheduled_flights SET status = 'completed' WHERE id = ?", (f["scheduled_flight_id"],))
    # Auto-deduct now that the flight is actually complete (Hobbs/Tach end,
    # elapsed instructor time, etc. are all in) - see log_new for the other
    # completion path (logged after the fact in one step).
    conn.execute("UPDATE flights SET solo_hours = ? WHERE id = ?",
                 (_solo_hours_from_form(request.form, f["solo"]), flight_id))
    ended_row = conn.execute(_LOG_ROW_SQL + " WHERE f.id = ?", (flight_id,)).fetchone()
    cost = _row_with_cost(ended_row) if ended_row else None
    if paid_choice == "1" and cost is not None:
        # Paid has to actually be accounted for: the flight's cost covered
        # either by credit already on the student's account (their existing
        # positive balance - see students.balance) or by what was collected
        # just now, or some of each. Clamped server-side regardless of what
        # the form sent, since a negative/inflated credit_applied would
        # otherwise let someone claim more credit than the student has.
        available_credit = max(0.0, ended_row["student_balance"] or 0.0)
        credit_applied = min(credit_requested, available_credit, cost["total"])
        covered = payment_amount + credit_applied
        if payment_amount > 0.005 and not payment_method:
            conn.rollback()
            conn.close()
            flash("Pick how the payment was made (cash, card, etc.) to log this flight as Paid.", "danger")
            return redirect(back)
        if cost["total"] > 0.005 and covered + 0.005 < cost["total"]:
            conn.rollback()
            conn.close()
            short = cost["total"] - covered
            flash(f"This flight comes to ${cost['total']:.2f}. ${covered:.2f} is accounted for so far - "
                  f"enter the remaining ${short:.2f} as a payment (or apply more credit) to log it as Paid.", "danger")
            return redirect(back)
        conn.execute("UPDATE flights SET payment_method=?, payment_amount=?, credit_applied=? WHERE id=?",
                     (payment_method, payment_amount, credit_applied, flight_id))
    if cost is not None:
        _deduct_flight_cost(conn, cost, created_by=session.get("user_name"))
        if paid_choice == "1" and payment_amount > 0.005:
            # The auto-deduction above already took the flight's full cost
            # off the balance (crediting nothing back for the credit_applied
            # portion, since that was already sitting in the balance as
            # existing credit) - this ledger entry is the actual new money
            # collected right now, bringing the balance back up by that much.
            note = f"Paid at flight ({payment_method})" if payment_method else "Paid at flight"
            _ledger_entry(conn, ended_row["student_id"], "payment", payment_amount, note=note,
                          flight_id=flight_id, created_by=session.get("user_name"))
    # The student's pilot logbook gets a pending, pre-filled entry.
    pilotlog.ensure_entry(conn, flight_id)
    conn.commit()
    conn.close()
    if squawk:
        flash("Flight ended and logged. The squawk you flagged will show up on the Maintenance side until it's acknowledged.", "success")
    else:
        flash("Flight ended and logged.", "success")
    return redirect(url_for("flight.log_history"))


def _solo_hours_from_form(form, solo):
    """The part of a dual flight the student flew alone (e.g. the
    instructor got out for pattern solos) - not billed for the instructor,
    and logged as solo/PIC in the student's logbook. None when there isn't
    one, or the whole flight was solo."""
    if solo:
        return None
    v = _parse_float(form.get("solo_hours"))
    return round(v, 1) if v and v > 0 else None


SIGNATURE_PREFIX = "data:image/png;base64,"
SIGNATURE_MAX_LEN = 300_000  # ~220 KB of PNG - a drawn signature is far smaller


def _signature_from_form(form):
    """(changed, value) from the signature pad's hidden field: "" = no
    change, "clear" = remove it, else a PNG data URL (validated)."""
    raw = (form.get("signature_data") or "").strip()
    if not raw:
        return False, None
    if raw == "clear":
        return True, None
    if raw.startswith(SIGNATURE_PREFIX) and len(raw) <= SIGNATURE_MAX_LEN:
        import base64
        import binascii
        try:
            png = base64.b64decode(raw[len(SIGNATURE_PREFIX):], validate=True)
        except (binascii.Error, ValueError):
            return False, None
        if png[:8] == b"\x89PNG\r\n\x1a\n":
            return True, raw
    return False, None


def _save_cfi_signature(conn, cfi_id, signature):
    """Store (or clear) a CFI's signature and put it on their logbook
    entries that are still pending (approved ones keep what they had)."""
    conn.execute("UPDATE cfis SET signature = ?, signature_updated_at = ? WHERE id = ?",
                 (signature, now_iso() if signature else None, cfi_id))
    conn.execute("""UPDATE pilot_logbook SET instructor_signature = ?
                    WHERE cfi_id = ? AND status = 'pending' AND dual > 0""", (signature, cfi_id))


def _cfi_cert_from_form(form):
    """(cfi_cert_number, cfi_cert_expires) - the instructor certificate
    printed with their name on students' logbook entries."""
    number = (form.get("cfi_cert_number") or "").strip()[:40] or None
    expires = (form.get("cfi_cert_expires") or "").strip()[:10] or None
    try:
        if expires:
            datetime.strptime(expires, "%Y-%m-%d")
    except ValueError:
        expires = None
    return number, expires


def _first_solo_from_form(form):
    """students.first_solo_date from the student form's "Has completed
    first solo" box (+ optional date; today if left blank). None = not yet,
    which keeps landing currency off their profile and the Students list."""
    if not form.get("first_solo_done"):
        return None
    d = (form.get("first_solo_date") or "").strip()[:10]
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        d = date.today().isoformat()
    return d


def _pay_preference_from_form(form):
    """students.pay_preference from the student form's How They Usually Pay
    select - one of PAY_PREFERENCES, or None if left blank/unrecognized."""
    v = (form.get("pay_preference") or "").strip()
    return v if v in PAY_PREFERENCES else None


def _tsa_verified_from_form(form):
    """students.tsa_verified_date from the student form's "TSA Verified"
    box (+ optional date; today if left blank). None = not verified yet."""
    if not form.get("tsa_verified_done"):
        return None
    d = (form.get("tsa_verified_date") or "").strip()[:10]
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        d = date.today().isoformat()
    return d


def _save_logged_flight(conn, form, date_field="flight_date", notes_field="notes"):
    """Saves one already-flown flight from a submitted form: the flights
    row, the plane's new Hobbs/Tach, marking its booking completed (when
    there is one), the squawk, and the auto-deduct from the student's
    balance. Shared by log_new (logging a scheduled booking after the fact)
    and schedule_new's "Flight Already Complete?" mode (a flight that was
    never put on the schedule) - date_field/notes_field say which form
    fields hold the date and the squawk notes, since Schedule Flight's own
    date/notes fields have different names. Commits and flashes the success
    message; returns an error message instead (nothing saved) when the form
    doesn't validate."""
    asset_id = form.get("asset_id") or None
    student_id = form.get("student_id") or None
    solo = 1 if form.get("solo") else 0
    cfi_id = None if solo else (form.get("cfi_id") or session["cfi_id"])
    flight_date = form.get(date_field, "").strip() or date.today().strftime("%Y-%m-%d")
    hobbs_start = _parse_float(form.get("hobbs_start"))
    hobbs_end = _parse_float(form.get("hobbs_end"))
    tach_start = _parse_float(form.get("tach_start"))
    tach_end = _parse_float(form.get("tach_end"))
    oil_added_qt = _parse_float(form.get("oil_added_qt"))
    ground_time_hours = _parse_float(form.get("ground_time_hours"))
    notes = form.get(notes_field, "").strip()
    scheduled_flight_id = form.get("scheduled_flight_id") or None
    day_landings_fs = _parse_int(form.get("day_landings_fs"))
    day_landings_tg = _parse_int(form.get("day_landings_tg"))
    night_landings_fs = _parse_int(form.get("night_landings_fs"))
    night_landings_tg = _parse_int(form.get("night_landings_tg"))
    # Any note is treated as a squawk needing shop attention - no
    # separate checkbox to remember to tick.
    squawk = 1 if notes else 0
    paid = 1 if form.get("paid") else 0
    payment_method = (form.get("payment_method") or "").strip()[:40] or None
    payment_amount = max(0.0, _parse_float(form.get("payment_amount")) or 0.0)
    credit_requested = max(0.0, _parse_float(form.get("credit_applied")) or 0.0)

    if not asset_id or not student_id:
        return "Select a plane and a student."
    if hobbs_end is not None and hobbs_start is not None and hobbs_end < hobbs_start:
        return "Ending Hobbs can't be less than starting Hobbs."

    cur = conn.execute("""INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, hobbs_start, hobbs_end,
                     tach_start, tach_end, oil_added_qt, notes, solo, squawk, ground_time_hours, paid,
                     scheduled_flight_id, day_landings_fs, day_landings_tg, night_landings_fs, night_landings_tg,
                     created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (cfi_id, student_id, asset_id, flight_date, hobbs_start, hobbs_end,
                  tach_start, tach_end, oil_added_qt, notes, solo, squawk, ground_time_hours, paid,
                  scheduled_flight_id, day_landings_fs, day_landings_tg, night_landings_fs, night_landings_tg,
                  now_iso()))
    new_flight_id = cur.lastrowid

    # Advance the plane's Hobbs/Tach readings so the maintenance tile's
    # due-soon/overdue tracking reflects the hours actually flown.
    if hobbs_end is not None:
        conn.execute("UPDATE assets SET hobbs_hours = ?, hobbs_updated_at = ? WHERE id = ?",
                     (hobbs_end, now_iso(), asset_id))
    if tach_end is not None:
        conn.execute("UPDATE assets SET tach_hours = ?, tach_updated_at = ? WHERE id = ?",
                     (tach_end, now_iso(), asset_id))
    if scheduled_flight_id:
        conn.execute("UPDATE scheduled_flights SET status = 'completed' WHERE id = ? AND status = 'scheduled'",
                     (scheduled_flight_id,))
    # Auto-deduct this flight's cost from the student's balance - logging
    # a flight is what "completes" it for billing purposes, whether it
    # was entered after the fact (here) or ended via the Active Flight
    # clock (see log_end).
    conn.execute("UPDATE flights SET solo_hours = ? WHERE id = ?", (_solo_hours_from_form(form, solo), new_flight_id))
    # Guest (no profile): the name typed on the form, else the booking's.
    guest_name, _guest_phone, _guest_email = _guest_fields(conn, form, student_id)
    if not guest_name and scheduled_flight_id:
        b = conn.execute("SELECT guest_name FROM scheduled_flights WHERE id = ?", (scheduled_flight_id,)).fetchone()
        guest_name = b["guest_name"] if b else None
    if guest_name and str(student_id) == str(_guest_student_id(conn)):
        conn.execute("UPDATE flights SET guest_name = ? WHERE id = ?", (guest_name, new_flight_id))
    new_row = conn.execute(_LOG_ROW_SQL + " WHERE f.id = ?", (new_flight_id,)).fetchone()
    cost = _row_with_cost(new_row) if new_row else None
    if paid and cost is not None:
        # Same "Paid has to actually be accounted for" check as ending a
        # flight from the Active Flight clock (log_end) - this form used to
        # let Paid through with no payment method and no amount at all,
        # silently logging the flight as paid without collecting either.
        available_credit = max(0.0, new_row["student_balance"] or 0.0)
        credit_applied = min(credit_requested, available_credit, cost["total"])
        covered = payment_amount + credit_applied
        if payment_amount > 0.005 and not payment_method:
            conn.rollback()
            return "Pick how the payment was made (cash, card, etc.) to log this flight as Paid."
        if cost["total"] > 0.005 and covered + 0.005 < cost["total"]:
            short = cost["total"] - covered
            conn.rollback()
            return (f"This flight comes to ${cost['total']:.2f}. ${covered:.2f} is accounted for so far - "
                    f"enter the remaining ${short:.2f} as a payment (or apply more credit) to log it as Paid.")
        conn.execute("UPDATE flights SET payment_method=?, payment_amount=?, credit_applied=? WHERE id=?",
                     (payment_method, payment_amount, credit_applied, new_flight_id))
    if cost is not None:
        _deduct_flight_cost(conn, cost, created_by=session.get("user_name"))
        if paid and payment_amount > 0.005:
            note = f"Paid at flight ({payment_method})" if payment_method else "Paid at flight"
            _ledger_entry(conn, new_row["student_id"], "payment", payment_amount, note=note,
                          flight_id=new_flight_id, created_by=session.get("user_name"))
    # The student's pilot logbook gets a pending, pre-filled entry.
    pilotlog.ensure_entry(conn, new_flight_id)
    conn.commit()
    if squawk:
        flash("Flight logged. The squawk you flagged will show up on the Maintenance side until it's acknowledged.", "success")
    else:
        flash("Flight logged.", "success")
    return None


@flight_bp.route("/log/new", methods=["GET", "POST"])
@cfi_required
def log_new():
    """Log a scheduled booking after the fact, pre-filled by clicking
    through from it (?asset_id=&student_id=&
    cfi_id=&solo=&flight_date=&scheduled_flight_id=) - the scheduled_flight_id
    travels through as a hidden field so, on save, that booking gets marked
    completed instead of lingering as an upcoming/conflicting booking."""
    conn = get_db()
    planes = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()
    students = conn.execute("SELECT * FROM students WHERE active = 1 ORDER BY name").fetchall()
    cfis = conn.execute("SELECT * FROM cfis WHERE active = 1 AND is_station = 0 ORDER BY name").fetchall()
    form_kwargs = dict(planes=planes, students=students, cfis=cfis,
                        today=date.today().strftime("%Y-%m-%d"), current_cfi_id=session["cfi_id"])
    if request.method == "POST":
        error = _save_logged_flight(conn, request.form)
        if error:
            flash(error, "danger")
            conn.close()
            return render_template("flight/log_new.html", form=request.form, **form_kwargs)
        conn.close()
        return redirect(url_for("flight.log_history"))

    # Pre-fill from a scheduled booking, if this was reached by clicking
    # "Log Flight" on one. The old stand-alone "Log a Flight" page (no
    # booking to pre-fill from) is gone - that's now Schedule Flight with
    # "Flight Already Complete?" checked - so an old bookmark or link with
    # no booking goes there instead.
    if not request.args.get("scheduled_flight_id"):
        conn.close()
        return redirect(url_for("flight.schedule_new", complete=1))
    prefill = {
        "asset_id": request.args.get("asset_id", ""),
        "student_id": request.args.get("student_id", ""),
        "cfi_id": request.args.get("cfi_id", ""),
        "solo": request.args.get("solo", ""),
        "flight_date": request.args.get("flight_date", ""),
        "scheduled_flight_id": request.args.get("scheduled_flight_id", ""),
    }
    conn.close()
    return render_template("flight/log_new.html", form=None, prefill=prefill, **form_kwargs)


def _can_edit_logged_flight():
    """Editing an already-logged flight (correcting Hobbs/Tach, notes,
    landings, etc. after the fact) is admin-or-any-CFI only - not students,
    even on their own solo flight."""
    return bool(session.get("is_master_admin") or session.get("cfi_id"))


@flight_bp.route("/log/<int:flight_id>/edit", methods=["GET", "POST"])
@login_required
def log_edit(flight_id):
    """Edit an already-logged (completed) flight - admin or any CFI only.
    Re-runs the billing deduction against the edited numbers (see
    _rededuct_flight_cost) since Hobbs/Tach/plane/instructor/solo can all
    change what the flight cost."""
    if not _can_edit_logged_flight():
        flash("Only an instructor or admin can edit a logged flight.", "danger")
        return redirect(url_for("flight.log_detail", flight_id=flight_id))
    conn = get_db()
    f = conn.execute("SELECT * FROM flights WHERE id = ?", (flight_id,)).fetchone()
    if not f:
        conn.close()
        flash("Flight not found.", "danger")
        return redirect(url_for("flight.log_history"))
    planes = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()
    students = conn.execute("SELECT * FROM students WHERE active = 1 OR id = ? ORDER BY name", (f["student_id"],)).fetchall()
    cfis = conn.execute("SELECT * FROM cfis WHERE active = 1 AND is_station = 0 OR id = ? ORDER BY name", (f["cfi_id"],)).fetchall()
    form_kwargs = dict(planes=planes, students=students, cfis=cfis,
                        today=date.today().strftime("%Y-%m-%d"), current_cfi_id=session.get("cfi_id"))

    if request.method == "POST":
        asset_id = request.form.get("asset_id") or None
        student_id = request.form.get("student_id") or None
        solo = 1 if request.form.get("solo") else 0
        cfi_id = None if solo else (request.form.get("cfi_id") or None)
        flight_date = request.form.get("flight_date", "").strip() or f["flight_date"]
        hobbs_start = _parse_float(request.form.get("hobbs_start"))
        hobbs_end = _parse_float(request.form.get("hobbs_end"))
        tach_start = _parse_float(request.form.get("tach_start"))
        tach_end = _parse_float(request.form.get("tach_end"))
        oil_added_qt = _parse_float(request.form.get("oil_added_qt"))
        ground_time_hours = _parse_float(request.form.get("ground_time_hours"))
        notes = request.form.get("notes", "").strip()
        day_landings_fs = _parse_int(request.form.get("day_landings_fs"))
        day_landings_tg = _parse_int(request.form.get("day_landings_tg"))
        night_landings_fs = _parse_int(request.form.get("night_landings_fs"))
        night_landings_tg = _parse_int(request.form.get("night_landings_tg"))
        squawk = 1 if notes else 0
        paid = 1 if request.form.get("paid") else 0

        if not asset_id or not student_id:
            flash("Select a plane and a student.", "danger")
            conn.close()
            return render_template("flight/log_new.html", form=request.form, edit_flight_id=flight_id, **form_kwargs)
        if hobbs_end is not None and hobbs_start is not None and hobbs_end < hobbs_start:
            flash("Ending Hobbs can't be less than starting Hobbs.", "danger")
            conn.close()
            return render_template("flight/log_new.html", form=request.form, edit_flight_id=flight_id, **form_kwargs)

        conn.execute("""UPDATE flights SET cfi_id=?, student_id=?, asset_id=?, flight_date=?, hobbs_start=?, hobbs_end=?,
                         tach_start=?, tach_end=?, oil_added_qt=?, notes=?, solo=?, squawk=?, ground_time_hours=?, paid=?,
                         day_landings_fs=?, day_landings_tg=?, night_landings_fs=?, night_landings_tg=? WHERE id=?""",
                     (cfi_id, student_id, asset_id, flight_date, hobbs_start, hobbs_end,
                      tach_start, tach_end, oil_added_qt, notes, solo, squawk, ground_time_hours, paid,
                      day_landings_fs, day_landings_tg, night_landings_fs, night_landings_tg, flight_id))
        if hobbs_end is not None:
            conn.execute("UPDATE assets SET hobbs_hours = ?, hobbs_updated_at = ? WHERE id = ? AND (hobbs_hours IS NULL OR hobbs_hours <= ?)",
                         (hobbs_end, now_iso(), asset_id, hobbs_end))
        if tach_end is not None:
            conn.execute("UPDATE assets SET tach_hours = ?, tach_updated_at = ? WHERE id = ? AND (tach_hours IS NULL OR tach_hours <= ?)",
                         (tach_end, now_iso(), asset_id, tach_end))
        conn.execute("UPDATE flights SET solo_hours = ? WHERE id = ?",
                     (_solo_hours_from_form(request.form, solo), flight_id))
        conn.execute("UPDATE flights SET guest_name = ? WHERE id = ?",
                     (_guest_fields(conn, request.form, student_id)[0], flight_id))
        edited_row = conn.execute(_LOG_ROW_SQL + " WHERE f.id = ?", (flight_id,)).fetchone()
        if edited_row:
            _rededuct_flight_cost(conn, _row_with_cost(edited_row), created_by=session.get("user_name"))
        # Refresh the student's logbook entry (only while it's still pending).
        pilotlog.ensure_entry(conn, flight_id)
        conn.commit()
        conn.close()
        flash("Flight updated.", "success")
        return redirect(url_for("flight.log_detail", flight_id=flight_id))

    form = {
        "asset_id": f["asset_id"], "student_id": f["student_id"], "cfi_id": f["cfi_id"], "solo": f["solo"],
        "flight_date": f["flight_date"], "hobbs_start": f["hobbs_start"], "hobbs_end": f["hobbs_end"],
        "tach_start": f["tach_start"], "tach_end": f["tach_end"], "oil_added_qt": f["oil_added_qt"],
        "ground_time_hours": f["ground_time_hours"], "notes": f["notes"], "paid": f["paid"],
        "day_landings_fs": f["day_landings_fs"], "day_landings_tg": f["day_landings_tg"],
        "night_landings_fs": f["night_landings_fs"], "night_landings_tg": f["night_landings_tg"],
        "solo_hours": f["solo_hours"], "guest_name": f["guest_name"],
    }
    conn.close()
    return render_template("flight/log_new.html", form=form, edit_flight_id=flight_id, **form_kwargs)


_LOG_ROW_SQL = """
    SELECT f.*, COALESCE(NULLIF(f.guest_name, '') || ' (guest)', s.name) as student_name, a.tag as plane_tag, a.name as plane_name, c.name as cfi_name,
           s.rate_override as student_rate_override, s.plane_rate_override as student_plane_rate_override,
           s.sim_rate_override as student_sim_rate_override, a.is_simulator as asset_is_simulator, a.sim_rate as asset_sim_rate,
           s.balance as student_balance, s.pay_preference as student_pay_preference,
           c.rate_per_hour as cfi_rate_per_hour,
           sf.duration_hours as scheduled_duration_hours, sf.part_solo as scheduled_part_solo
    FROM flights f
    JOIN students s ON s.id = f.student_id
    JOIN assets a ON a.id = f.asset_id
    LEFT JOIN cfis c ON c.id = f.cfi_id
    LEFT JOIN scheduled_flights sf ON sf.id = f.scheduled_flight_id
"""


def _row_with_cost(row):
    d = dict(row)
    # Plane rate comes only from the student's own Plane Rate (Students >
    # Edit). Planes no longer carry a rental rate of their own - the old
    # assets.rental_rate column is left in the database but ignored.
    # A simulator is different: it has its own base rate (assets.sim_rate,
    # set on its profile) since it isn't a real aircraft with a per-student
    # design like the plane rate - a student's Sim Rate override, if set,
    # still wins over that base rate the same way plane_rate_override would.
    if row["asset_is_simulator"]:
        d["plane_rate"] = row["student_sim_rate_override"] if row["student_sim_rate_override"] is not None else row["asset_sim_rate"]
    else:
        d["plane_rate"] = row["student_plane_rate_override"]
    d["instructor_rate"] = row["student_rate_override"] if row["student_rate_override"] is not None else row["cfi_rate_per_hour"]
    d.update(_flight_cost(d))
    return d


@flight_bp.route("/log")
@login_required
def log_history():
    conn = get_db()
    cfi = current_cfi(conn)
    if cfi:
        rows = conn.execute(_LOG_ROW_SQL + " ORDER BY f.flight_date DESC, f.id DESC LIMIT 100").fetchall()
    else:
        student = current_student(conn)
        rows = conn.execute(_LOG_ROW_SQL + " WHERE f.student_id = ? ORDER BY f.flight_date DESC, f.id DESC LIMIT 100",
                             (student["id"],)).fetchall()
    conn.close()
    flights = [_row_with_cost(r) for r in rows]
    return render_template("flight/log_history.html", flights=flights, cfi=cfi, can_bill=can_manage_billing())


@flight_bp.route("/log/<int:flight_id>")
@login_required
def log_detail(flight_id):
    """Full detail view for one logged flight - the "more details" drill-down
    from a row on Flight History or Billing: every reading, the clock
    times if it was logged that way, and the full cost breakdown, not just
    the summary columns those tables have room for. A student can only
    open their own flights; a CFI/admin can open any of them."""
    conn = get_db()
    row = conn.execute(_LOG_ROW_SQL + " WHERE f.id = ?", (flight_id,)).fetchone()
    if not row:
        conn.close()
        flash("Flight not found.", "danger")
        return redirect(url_for("flight.log_history"))
    student = current_student(conn)
    if student and row["student_id"] != student["id"]:
        conn.close()
        flash("That's not your flight.", "danger")
        return redirect(url_for("flight.log_history"))
    f = _row_with_cost(row)
    conn.close()
    return render_template("flight/log_detail.html", f=f, can_bill=can_manage_billing())


@flight_bp.route("/log/<int:flight_id>/delete", methods=["POST"])
@admin_required
def log_delete(flight_id):
    """Master admin only: permanently removes one flight from Flight
    History (a duplicate, a test entry, one logged against the wrong
    student). Also undoes what logging it did to the student's account:
    its ledger deduction(s) are removed and the balance put back. A flight
    still in progress sends its booking back to "scheduled" so it can be
    started again; a finished booking stays as it was on the calendar. The
    plane's Hobbs/Tach readings aren't rolled back (they may have moved on
    since) - fix those on the plane if needed. Recorded in the field change
    log with the flight's details."""
    conn = get_db()
    row = conn.execute(_LOG_ROW_SQL + " WHERE f.id = ?", (flight_id,)).fetchone()
    if not row:
        conn.close()
        flash("That flight was already deleted.", "warning")
        return redirect(url_for("flight.log_history"))
    f = _row_with_cost(row)
    who = session.get("user_name")
    for led in conn.execute("SELECT id, student_id, amount FROM student_ledger WHERE flight_id = ?", (flight_id,)).fetchall():
        conn.execute("UPDATE students SET balance = balance - ? WHERE id = ?", (led["amount"], led["student_id"]))
        conn.execute("DELETE FROM student_ledger WHERE id = ?", (led["id"],))
    if f["scheduled_flight_id"] and f["started_at"] and not f["ended_at"]:
        conn.execute("UPDATE scheduled_flights SET status = 'scheduled' WHERE id = ? AND status = 'in_progress'",
                     (f["scheduled_flight_id"],))
    summary = (f"{_us_date(f['flight_date'])} {f['plane_tag']} - {f['student_name']}"
               f"{' with ' + f['cfi_name'] if f['cfi_name'] else ' (solo)'}, {f['hours']:.1f} hr, ${f['total']:.2f}")
    _log_field_change(conn, "flight", flight_id, "deleted", summary, "", who)
    pilotlog.remove_for_flight(conn, flight_id)
    conn.execute("DELETE FROM flights WHERE id = ?", (flight_id,))
    conn.commit()
    conn.close()
    flash(f"Flight deleted: {summary}. Its charge was taken off {f['student_name']}'s account.", "success")
    return redirect(url_for("flight.log_history"))


@flight_bp.route("/log/<int:flight_id>/toggle_paid", methods=["POST"])
@billing_required
def log_toggle_paid(flight_id):
    conn = get_db()
    f = conn.execute("SELECT paid FROM flights WHERE id = ?", (flight_id,)).fetchone()
    if not f:
        conn.close()
        flash("Flight not found.", "danger")
        return redirect(url_for("flight.log_history"))
    conn.execute("UPDATE flights SET paid = ? WHERE id = ?", (0 if f["paid"] else 1, flight_id))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("flight.log_history"))


@flight_bp.route("/billing")
@billing_required
def billing():
    """Flights grouped by student, each with a plane/instructor cost
    breakdown and a running total - so it's obvious at a glance who owes
    what. Filterable by status (?status=unpaid|paid|all) - defaults to
    unpaid, since that's the common case (who still owes money)."""
    status = request.args.get("status", "unpaid").strip().lower()
    if status not in ("unpaid", "paid", "all"):
        status = "unpaid"
    conn = get_db()
    sql = _LOG_ROW_SQL
    if status == "unpaid":
        sql += " WHERE f.paid = 0"
    elif status == "paid":
        sql += " WHERE f.paid = 1"
    sql += " ORDER BY f.student_id, f.flight_date, f.id"
    rows = conn.execute(sql).fetchall()
    conn.close()
    flights = [_row_with_cost(r) for r in rows]
    groups = {}
    order = []
    for f in flights:
        sid = f["student_id"]
        if sid not in groups:
            groups[sid] = {"student_id": sid, "student_name": f["student_name"], "flights": [], "total": 0.0}
            order.append(sid)
        groups[sid]["flights"].append(f)
        groups[sid]["total"] += f["total"]
    students_billing = [groups[sid] for sid in order]
    students_billing.sort(key=lambda g: g["student_name"])
    grand_total = sum(g["total"] for g in students_billing)
    return render_template("flight/billing.html", students_billing=students_billing,
                           grand_total=grand_total, status=status)


@flight_bp.route("/billing/export")
@billing_required
def billing_export():
    """CSV export of the billing view - either the specific flights checked
    on the page (?ids=1,2,3) or, with no selection, every flight currently
    shown for the chosen status filter (same as what's on screen)."""
    status = request.args.get("status", "unpaid").strip().lower()
    if status not in ("unpaid", "paid", "all"):
        status = "unpaid"
    ids_param = request.args.get("ids", "").strip()
    selected_ids = None
    if ids_param:
        try:
            selected_ids = {int(x) for x in ids_param.split(",") if x.strip()}
        except ValueError:
            selected_ids = None

    conn = get_db()
    sql = _LOG_ROW_SQL
    if not selected_ids:
        if status == "unpaid":
            sql += " WHERE f.paid = 0"
        elif status == "paid":
            sql += " WHERE f.paid = 1"
    sql += " ORDER BY f.student_id, f.flight_date, f.id"
    rows = conn.execute(sql).fetchall()
    conn.close()
    flights = [_row_with_cost(r) for r in rows]
    if selected_ids:
        flights = [f for f in flights if f["id"] in selected_ids]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Student", "Plane", "Instructor", "Flight Hrs", "Ground Hrs",
                      "Plane $", "Instructor $", "Ground $", "Total", "Paid"])
    grand_total = 0.0
    for f in flights:
        writer.writerow([
            f["flight_date"], f["student_name"], f["plane_tag"],
            "Solo" if f["solo"] else (f["cfi_name"] or "-"),
            f["hours"], f["ground_hours"] or 0,
            round(f["plane_cost"], 2), round(f["instructor_cost"], 2), round(f["ground_cost"], 2),
            round(f["total"], 2), "Yes" if f["paid"] else "No",
        ])
        grand_total += f["total"]
    writer.writerow([])
    writer.writerow(["", "", "", "", "", "", "", "", "Grand Total", round(grand_total, 2), ""])

    scope = "selected" if selected_ids else status
    fname = f"flight_billing_{scope}_{date.today().strftime('%Y%m%d')}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": f"attachment; filename={fname}"})


@flight_bp.route("/billing/student/<int:student_id>/mark_paid", methods=["POST"])
@billing_required
def billing_mark_paid(student_id):
    conn = get_db()
    conn.execute("UPDATE flights SET paid = 1 WHERE student_id = ? AND paid = 0", (student_id,))
    conn.commit()
    conn.close()
    flash("Marked paid.", "success")
    return redirect(url_for("flight.billing"))


def _stats_range(range_key, custom_start, custom_end):
    """Resolves a preset ('week'/'month'/'year'/'custom') plus optional
    custom start/end strings into a concrete (range_key, start_date,
    end_date). Falls back to 'month' on anything unrecognized or
    unparseable, so the stats page always has something sane to show."""
    today = date.today()
    if range_key == "week":
        start, end = today - timedelta(days=today.weekday()), today
    elif range_key == "year":
        start, end = today.replace(month=1, day=1), today
    elif range_key == "custom":
        try:
            start = datetime.strptime(custom_start, "%Y-%m-%d").date() if custom_start else today.replace(day=1)
        except ValueError:
            start = today.replace(day=1)
        try:
            end = datetime.strptime(custom_end, "%Y-%m-%d").date() if custom_end else today
        except ValueError:
            end = today
        if end < start:
            start, end = end, start
    else:
        range_key = "month"
        start, end = today.replace(day=1), today
    return range_key, start, end


@flight_bp.route("/stats")
@billing_required
def stats():
    """School-wide statistics: hours flown, hours billed, and money made,
    over a chosen week/month/year/custom range - billing-permission-gated
    since it's the same $ visibility as the Billing page. Optionally
    filtered to one plane; either way, a bar chart compares every plane's
    hours side by side so it's obvious how they stack up against each other."""
    range_key, start, end = _stats_range(
        request.args.get("range", "month"), request.args.get("start", ""), request.args.get("end", ""))
    start_str, end_str = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    # Multi-select: an empty selection means "all" (unfiltered), same as
    # before - picking one or more planes/instructors narrows the range
    # down to just those, so the side-by-side charts/tables compare exactly
    # the set someone's interested in rather than everything at once.
    plane_ids = [x for x in request.args.getlist("plane_id") if x.strip()]
    cfi_ids = [x for x in request.args.getlist("cfi_id") if x.strip()]

    conn = get_db()
    planes = conn.execute(
        "SELECT * FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()
    cfis = conn.execute("SELECT * FROM cfis WHERE active = 1 AND is_station = 0 ORDER BY name").fetchall()
    sql = _LOG_ROW_SQL + " WHERE f.flight_date BETWEEN ? AND ?"
    params = [start_str, end_str]
    if plane_ids:
        sql += f" AND f.asset_id IN ({','.join('?' * len(plane_ids))})"
        params += plane_ids
    if cfi_ids:
        sql += f" AND f.cfi_id IN ({','.join('?' * len(cfi_ids))})"
        params += cfi_ids
    sql += " ORDER BY f.flight_date, f.id"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    flights = [_row_with_cost(r) for r in rows]

    total_hours = sum(f["hours"] for f in flights)
    billed_hours = sum(f["hours"] for f in flights if f["paid"])
    revenue_collected = sum(f["total"] for f in flights if f["paid"])
    outstanding = sum(f["total"] for f in flights if not f["paid"])

    by_plane = {}
    for f in flights:
        b = by_plane.setdefault(f["plane_tag"], {"plane": f["plane_tag"], "hours": 0.0, "revenue": 0.0})
        b["hours"] += f["hours"]
        b["revenue"] += f["total"]
    plane_rows = sorted(by_plane.values(), key=lambda b: -b["hours"])
    plane_chart_labels = [b["plane"] for b in plane_rows]
    plane_chart_hours = [round(b["hours"], 2) for b in plane_rows]

    by_instructor = {}
    for f in flights:
        key = f["cfi_name"] or "Solo"
        b = by_instructor.setdefault(key, {"instructor": key, "hours": 0.0, "revenue": 0.0})
        b["hours"] += f["hours"]
        b["revenue"] += f["total"]
    instructor_rows = sorted(by_instructor.values(), key=lambda b: -b["hours"])
    instructor_chart_labels = [b["instructor"] for b in instructor_rows]
    instructor_chart_hours = [round(b["hours"], 2) for b in instructor_rows]

    daily = {}
    for f in flights:
        daily[f["flight_date"]] = daily.get(f["flight_date"], 0.0) + f["hours"]
    chart_labels = sorted(daily.keys())
    chart_hours = [round(daily[d], 2) for d in chart_labels]

    return render_template("flight/stats.html", range_key=range_key, start=start_str, end=end_str,
                           planes=planes, plane_ids=plane_ids, cfis=cfis, cfi_ids=cfi_ids,
                           total_hours=total_hours, billed_hours=billed_hours,
                           revenue_collected=revenue_collected, outstanding=outstanding,
                           plane_rows=plane_rows, instructor_rows=instructor_rows,
                           plane_chart_labels=plane_chart_labels, plane_chart_hours=plane_chart_hours,
                           instructor_chart_labels=instructor_chart_labels, instructor_chart_hours=instructor_chart_hours,
                           flight_count=len(flights), chart_labels=chart_labels, chart_hours=chart_hours)


# ---------------------------------------------------------------------------
# Account management (creating accounts, resetting passwords, granting
# admin) now lives in one place shared with Shop Inventory - see
# app.py's /admin/users. These old URLs just forward there.
# ---------------------------------------------------------------------------

@flight_bp.route("/admin/cfis")
@admin_required
def admin_cfis_list():
    return redirect(url_for("admin_users_list"))


@flight_bp.route("/admin/cfis/<int:cfi_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_cfi_edit(cfi_id):
    conn = get_db()
    cfi_row = conn.execute("SELECT user_id FROM cfis WHERE id = ?", (cfi_id,)).fetchone()
    conn.close()
    if cfi_row and cfi_row["user_id"]:
        return redirect(url_for("admin_user_edit", user_id=cfi_row["user_id"]))
    return redirect(url_for("admin_users_list"))
