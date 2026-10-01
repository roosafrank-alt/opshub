import os
import re
import csv
import json
import io
import calendar as calendar_mod
import secrets
import sqlite3
import subprocess
import threading
import time
import logging
import math
from datetime import date, datetime, timedelta, timezone
from flask import (Flask, render_template, request, redirect, url_for, jsonify, flash, abort,
                    Response, session, got_request_exception)

from db import (get_db, init_db, user_shop_roles, user_flight_roles, clean_roles, SHOP_ROLE_ORDER, FLIGHT_ROLE_ORDER,
                 close_request_conns, gen_internal_barcode, gen_project_code, gen_labor_code, now_iso,
                 allowed_image, save_upload, UPLOAD_DIR, asset_meter, maintenance_status,
                 MAINT_CATEGORY_COLORS, MAINT_CATEGORY_LABELS, found_item_messages)
from flight import flight_bp, _flight_hours, check_session_alerts, run_balance_hold_release_check, hundred_hr_status
from logbook import logbook_bp
from pilotlog import pilotlog_bp
from customer import customer_bp, _owned_asset_ids, _project_bill
from manuals import manuals_bp, manuals_for_asset
from groundschool import groundschool_bp
from payroll import payroll_bp
import tracking
from ads import ads_bp, ads_for_asset, add_ads_to_annual_project, AD_KINDS, AD_METHODS
import academy
from auth import (authenticate, log_in_user, log_out_user, current_user, login_required,
                   master_admin_required, shop_role_required, can_see_shop_costs,
                   owner_user_id, owner_locked, authenticate_customer, log_in_combined,
                   current_customer, start_view_as, exit_view_as, viewing_as_label,
                   view_as_active_program, SHOP_VIEW_AS_LEVELS, FLIGHT_VIEW_AS_LEVELS,
                   real_is_master_admin, view_as_chips, home_view_as_level,
                   account_program_count, single_program_endpoint,
                   login_allowed, login_failed, login_succeeded, unlock_account, LOGIN_BLOCKED_MSG,
                   person_view_active, person_view_name, can_view_as_person,
                   start_view_as_person, stop_view_as_person, real_user_id)
import notify
import wave_billing
from urllib.parse import urlparse
import push

def _load_or_create_secret_key():
    """The key that signs session cookies - anyone who has it can forge a
    valid login for any account, including a master admin, so it can't be a
    fixed value checked into the code. Generated once (32 random bytes) and
    kept in instance/secret_key - the same place as the database, which
    means it's excluded from every deploy rsync (--exclude=instance/) and
    never gets pushed out or overwritten. Reusing the same key across
    restarts is what keeps existing logins valid instead of signing
    everyone out every time the service restarts; a fresh Pi (or anyone
    restoring from a backup without instance/) just gets a new key and a
    fresh round of logins."""
    instance_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance")
    os.makedirs(instance_dir, exist_ok=True)
    key_path = os.path.join(instance_dir, "secret_key")
    if os.path.exists(key_path):
        with open(key_path) as f:
            key = f.read().strip()
        if key:
            return key
    key = secrets.token_hex(32)
    with open(key_path, "w") as f:
        f.write(key)
    try:
        os.chmod(key_path, 0o600)  # readable/writable only by the account running the app
    except OSError:
        pass
    return key


app = Flask(__name__)
app.secret_key = _load_or_create_secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=180)  # keeps a login valid for months - mainly for the kiosk display, which should stay logged in across reboots rather than showing the login screen every time
app.register_blueprint(flight_bp)
app.register_blueprint(logbook_bp)
app.register_blueprint(pilotlog_bp)
app.register_blueprint(customer_bp)
app.register_blueprint(manuals_bp)
app.register_blueprint(groundschool_bp)
app.register_blueprint(payroll_bp)
app.register_blueprint(ads_bp)
app.teardown_request(close_request_conns)


def _parse_qty(raw, allow_zero=False):
    """A quantity typed or scanned at the parts counter -> float, or None
    if it isn't a usable number. Rejects NaN/Infinity (float("nan") passes
    every `<= 0` check and would write NaN into qty_on_hand, wrecking that
    part's count for good) and negatives; zero only when allow_zero (a
    physical recount can legitimately be 0, a scan/assignment can't)."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val < 0 or (val == 0 and not allow_zero):
        return None
    return val


# ---------------------------------------------------------------------------
# Error log - instead of one big growing file, every unhandled exception is
# saved as its OWN file in instance/error_logs/, named by the timestamp it
# happened at (survives deploys since instance/ is excluded from the rsync
# push, same as the database). Viewable from Admin -> System (admin_system_log
# below) so an error can be diagnosed and sent to Claude without needing SSH
# access to the Pi at all. `got_request_exception` fires regardless of debug
# mode - unlike Flask's own log_exception, which is skipped when DEBUG is on
# (PROPAGATE_EXCEPTIONS bypasses it) - so this still logs even though the
# interactive debugger is also enabled below.
# ---------------------------------------------------------------------------
INSTANCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)
ERROR_LOG_DIR = os.path.join(INSTANCE_DIR, "error_logs")
os.makedirs(ERROR_LOG_DIR, exist_ok=True)
_MAX_ERROR_LOG_FILES = 200  # oldest files beyond this are pruned, so a long Pi uptime doesn't fill the SD card


class _PerFileErrorHandler(logging.Handler):
    """Writes each log record to its own timestamped file in ERROR_LOG_DIR
    instead of appending to one ever-growing file, so a single error can be
    opened, copied, or deleted on its own rather than hunted for inside a
    big block of scrollback. Prunes the oldest files past
    _MAX_ERROR_LOG_FILES on every write."""

    def emit(self, record):
        try:
            os.makedirs(ERROR_LOG_DIR, exist_ok=True)
            stamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]
            path = os.path.join(ERROR_LOG_DIR, stamp + ".log")
            suffix = 1
            while os.path.exists(path):  # two errors in the same millisecond - make the name unique
                path = os.path.join(ERROR_LOG_DIR, "%s-%d.log" % (stamp, suffix))
                suffix += 1
            with open(path, "w") as f:
                f.write(self.format(record) + "\n")
            self._prune()
        except OSError:
            pass

    def _prune(self):
        try:
            names = sorted(n for n in os.listdir(ERROR_LOG_DIR) if n.endswith(".log"))
        except OSError:
            return
        for name in names[:max(0, len(names) - _MAX_ERROR_LOG_FILES)]:
            try:
                os.remove(os.path.join(ERROR_LOG_DIR, name))
            except OSError:
                pass


_log_handler = _PerFileErrorHandler()
_log_handler.setLevel(logging.WARNING)
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
app.logger.addHandler(_log_handler)
app.logger.setLevel(logging.WARNING)


def _log_request_exception(sender, exception, **extra):
    path = request.path if request else "(no request)"
    sender.logger.error("Unhandled exception on %s %s: %s", request.method if request else "?",
                        path, exception, exc_info=exception)


got_request_exception.connect(_log_request_exception, app)


def _skip_flasks_own_exception_log(exc_info):
    """Drops Flask's own copy of an unhandled exception.

    Flask's handle_exception() sends got_request_exception (logged just
    above) and then, when the exception isn't being propagated - i.e. debug
    off, which is how the Pi runs - logs the very same traceback itself
    through app.logger. Every crash therefore landed in the error log
    TWICE: once as Flask's "Exception on /api/scan [POST]" and once as the
    hook's "Unhandled exception on POST /api/scan: ...". Two files, one
    problem, and the count on Admin -> Home doubled with it.

    The hook's line is the one worth keeping: it names the method and path
    in the order _ERROR_LOG_AFFECTED_RE reads, so the log's Affected column
    says "POST /api/scan" instead of "-". This replaces Flask's
    log_exception (its only caller is handle_exception, always after the
    signal above, so nothing goes unlogged) to keep it to one file each.
    test_error_log.py fails if a Flask upgrade ever brings the second copy
    back."""


app.log_exception = _skip_flasks_own_exception_log

_ERROR_LOG_HEAD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) \[(\w+)\] (.*)$")
_ERROR_LOG_AFFECTED_RE = re.compile(r"on (\S+ \S+):")


def _parse_error_log_entry(name, text):
    """Pulls a one-line summary (date, time, the request that hit it) out of
    a saved error file for the Admin error log's compact list view - the
    full traceback (the file's whole content) only gets shown once that row
    is clicked open. Falls back gracefully if a file was hand-edited or the
    log format ever changes, rather than erroring the whole page."""
    first_line = text.split("\n", 1)[0]
    m = _ERROR_LOG_HEAD_RE.match(first_line)
    if m:
        log_date, log_time, _level, message = m.groups()
    else:
        log_date, log_time, message = "", "", first_line
    am = _ERROR_LOG_AFFECTED_RE.search(message)
    affected = am.group(1) if am else "-"
    return {"name": name, "date": log_date, "time": log_time, "affected": affected,
            "message": message, "text": text}


def usdate(value, show_time=False):
    """Formats an ISO date/datetime string ('YYYY-MM-DD' or
    'YYYY-MM-DD HH:MM:SS'/'YYYY-MM-DDTHH:MM:SS') as DD-MM-YYYY, the display
    format used everywhere in this app (storage/sorting stays ISO under the
    hood). Anything that doesn't look like an ISO date passes through
    unchanged. With show_time, appends HH:MM after the date."""
    if not value:
        return value
    s = str(value).strip()
    if len(s) < 10:
        return value
    date_part = s[:10]
    parts = date_part.split("-")
    if len(parts) != 3 or len(parts[0]) != 4:
        return value
    y, m, d = parts
    out = f"{d}-{m}-{y}"
    if show_time:
        rest = s[10:].replace("T", " ").strip()
        if rest:
            out += " " + rest[:5]
    return out


app.jinja_env.filters["usdate"] = usdate


def shortwhen(value):
    """'Today 7:56 PM' for today's ISO datetimes, otherwise the usdate date
    (DD-MM-YYYY). Anything unparsable passes through via usdate()."""
    if not value:
        return value
    s = str(value).strip().replace("T", " ")
    try:
        dt = datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return usdate(value)
    if dt.date() == datetime.now().date():
        return "Today " + dt.strftime("%I:%M %p").lstrip("0")
    return usdate(s[:10])


app.jinja_env.filters["shortwhen"] = shortwhen


def shortdate(value):
    """'YYYY-MM-DD' -> 'MM-DD' - the compact month-day date for the phone
    dashboard's Today/Upcoming headers (QA ux-flight-dash-section-headers).
    Deliberately month-day, not this app's usual day-month (usdate above) -
    Frank asked for that order specifically for this one spot."""
    if not value:
        return value
    s = str(value).strip()
    if len(s) < 10:
        return value
    date_part = s[:10]
    parts = date_part.split("-")
    if len(parts) != 3 or len(parts[0]) != 4:
        return value
    _, m, d = parts
    return f"{m}-{d}"


app.jinja_env.filters["shortdate"] = shortdate


def first_name(value):
    """'Kate Frank' -> 'Kate', for friendly greetings ("Welcome, Kate")."""
    parts = str(value or "").split()
    return parts[0] if parts else ""


app.jinja_env.filters["first_name"] = first_name
app.jinja_env.globals["tracking_carrier_choices"] = tracking.CARRIER_CHOICES
app.jinja_env.globals["user_shop_roles"] = user_shop_roles
app.jinja_env.globals["user_flight_roles"] = user_flight_roles
app.jinja_env.globals["tracking_info"] = tracking.to_json
app.jinja_env.globals["academy_point_rules"] = academy.POINT_RULES
app.jinja_env.globals["academy_entry_kinds"] = academy.ENTRY_KINDS
app.jinja_env.globals["academy_achievements"] = academy.ACHIEVEMENTS
app.jinja_env.globals["academy_cert_points"] = academy.CERT_POINTS
app.jinja_env.globals["academy_rating_points"] = academy.RATING_POINTS


_WIND_COMPASS_POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def wind_compass_label(deg):
    """Degrees -> a 16-point compass label (e.g. 270 -> 'W'), for the
    Flight School dashboard weather tile's wind direction display."""
    if deg is None:
        return "-"
    try:
        deg = float(deg) % 360
    except (TypeError, ValueError):
        return "-"
    idx = int((deg / 22.5) + 0.5) % 16
    return _WIND_COMPASS_POINTS[idx]


app.jinja_env.globals["_wind_compass"] = wind_compass_label


def _view_as_chip_rows():
    """{'shop': [...], 'flight': [...]} for _view_as_chips.html - see
    auth.view_as_chips."""
    if not session.get("user_id"):
        return {}
    conn = get_db()
    try:
        return {p: view_as_chips(conn, p) for p in ("shop", "flight")}
    finally:
        conn.close()


@app.context_processor
def inject_auth_context():
    open_squawk_count = 0
    if session.get("is_master_admin") or session.get("shop_role") in ("admin", "tech"):
        conn = get_db()
        open_squawk_count = conn.execute(
            "SELECT COUNT(*) c FROM flights WHERE squawk = 1 AND squawk_acknowledged_at IS NULL"
        ).fetchone()["c"]
        conn.close()
    return {
        "can_see_shop_costs": can_see_shop_costs(),
        "shop_role": session.get("shop_role"),
        "is_master_admin": session.get("is_master_admin"),
        "logged_in_user_name": session.get("user_name"),
        "open_squawk_count": open_squawk_count,
        "tour_seen_shop": session.get("tour_seen_shop"),
        "tour_seen_flight": session.get("tour_seen_flight"),
        "maint_category_colors": CATEGORY_COLORS,
        "maint_category_labels": CATEGORY_LABELS,
        "viewing_as": viewing_as_label(),
        "is_real_master_admin": real_is_master_admin(),
        "view_as_chip_rows": _view_as_chip_rows(),
        "view_as_active_program": view_as_active_program(),
        "single_program_account": account_program_count() == 1,
        "person_view_name": person_view_name(),
        "can_view_as_person": can_view_as_person(),
    }


# Viewing as a person (auth.start_view_as_person) is look-only: refuse
# anything that would change data while it's on. Going back to your own
# view, picking someone else, the role chips and logging out still work.
_PERSON_VIEW_ALLOWED = {"view_as_person_start", "view_as_person_exit", "view_as_start", "view_as_exit"}


@app.before_request
def block_writes_in_person_view():
    if not person_view_active() or request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.endpoint in _PERSON_VIEW_ALLOWED:
        return None
    msg = (f"You're only looking at {person_view_name()}'s account, so nothing can be changed. "
           "Use Back to my view to make changes.")
    if request.is_json or request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
        return jsonify(ok=False, error=msg), 403
    flash(msg, "warning")
    return redirect(request.referrer or url_for("home_launcher"))


@app.route("/", methods=["GET", "POST"])
def home_launcher():
    """Master login + top-level launcher. Not signed in: shows the login
    form. Signed in: shows only the program tiles (Shop Inventory - really
    the shop + maintenance tile - and/or Flight School) this account has a
    role in, plus a "My Aircraft" tile for anyone with a customer account
    (own aircraft for a plain customer, the admin customer-management page
    for a shop admin). The form checks the entered username/password
    against both the staff `users` table and the `customers` table, so a
    flight student who's also an aircraft-owning customer (same
    email/password in both) gets every tile at once - see
    auth.log_in_combined. An account with only one tile (a lone CFI/student,
    a tech, or an aircraft owner) skips this picker entirely and lands
    straight on that program's home - see auth.single_program_endpoint."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # Checked by default (see home_launcher.html) - unchecking it is for
        # a shared/public device, so that login clears out when the browser
        # closes instead of staying signed in for the next person. A
        # checkbox only appears in form data at all when it's checked.
        remember = "remember" in request.form
        if not login_allowed(username):
            flash(LOGIN_BLOCKED_MSG, "danger")
            return render_template("home_launcher.html", user=None, customer=None, username=username)
        user_row = authenticate(username, password)
        # Customers log in by email - the hub's "Username" field doubles as
        # that when it doesn't match a staff username.
        customer_row = authenticate_customer(username, password)
        if not user_row and not customer_row:
            login_failed(username)
            flash(LOGIN_BLOCKED_MSG, "danger")
            return render_template("home_launcher.html", user=None, customer=None, username=username)
        login_succeeded(username)
        log_in_combined(user_row, customer_row, remember=remember)
        flash(f"Welcome, {(user_row or customer_row)['name']}!", "success")
        if user_row and user_row["shop_role"] == "admin" and not user_row["is_master_admin"]:
            # Idea ux-shop-admin-skip-launcher: a shop admin's login goes
            # straight to the shop home instead of "Choose a program" -
            # the grid button (same url_for('home_launcher') link) still
            # opens the real picker any time they want to switch programs.
            return redirect(url_for("dashboard"))
        return redirect(url_for("home_launcher"))

    only_program = single_program_endpoint()
    if only_program:
        return redirect(url_for(only_program))

    conn = get_db()
    user = current_user(conn)
    customer = current_customer(conn)
    conn.close()
    return render_template("home_launcher.html", user=user, customer=customer)


@app.route("/tour/seen", methods=["POST"])
@login_required
def tour_seen():
    """Fired once by the guided-tour JS when someone finishes or skips it,
    so it doesn't pop up again on their next login. ?side=shop|flight."""
    side = request.form.get("side", "").strip()
    if side not in ("shop", "flight"):
        return ("", 400)
    conn = get_db()
    col = "tour_seen_shop" if side == "shop" else "tour_seen_flight"
    conn.execute(f"UPDATE users SET {col} = 1 WHERE id = ?", (session["user_id"],))
    conn.commit()
    conn.close()
    session[col] = True
    return ("", 204)


@app.route("/logout")
def logout():
    log_out_user()
    flash("Logged out.", "success")
    return redirect(url_for("home_launcher"))


@app.route("/academy")
@login_required
def academy_page():
    """Flight Academy - phase 3 placeholder. Tracks student progress,
    experience toward ratings, and (eventually) lesson plans/ground school.
    Only visible/reachable for accounts an admin has specifically granted
    academy_access to (master admins always have it) - the tile on the
    program picker is hidden for everyone else, and this route double-checks
    it server-side too."""
    if not (session.get("is_master_admin") or session.get("academy_access")):
        flash("You don't have access to Flight Academy yet. Ask an admin.", "danger")
        return redirect(url_for("home_launcher"))
    # Leaderboard (see academy.py): points, achievements and rankings for
    # every student, for All Time / This Year / This Month.
    period = request.args.get("period", "all")
    if period not in ("all", "year", "month"):
        period = "all"
    conn = get_db()
    stats = academy.student_stats(conn, academy.period_start(period))
    boards = academy.leaderboards(stats)
    my_id = session.get("student_id")
    me = stats.get(my_id) if my_id else None
    staff = bool(session.get("is_master_admin") or session.get("cfi_id"))
    entry_sql = """SELECT e.*, s.name as student_name FROM academy_entries e
                     JOIN students s ON s.id = e.student_id"""
    if staff:
        entries = conn.execute(entry_sql + " ORDER BY e.entry_date DESC, e.id DESC LIMIT 25").fetchall()
    elif my_id:
        entries = conn.execute(entry_sql + " WHERE e.student_id = ? ORDER BY e.entry_date DESC, e.id DESC LIMIT 25",
                               (my_id,)).fetchall()
    else:
        entries = []
    conn.close()
    return render_template("academy.html", stats=stats, boards=boards, me=me,
                           my_rank=academy.rank_of(stats, my_id) if my_id else None,
                           entries=entries, staff=staff, period=period,
                           entry_kinds=academy.ENTRY_KINDS, entry_kind_map=academy.ENTRY_KIND_MAP,
                           leaderboard_defs=academy.LEADERBOARDS, today=date.today().isoformat())


@app.route("/academy/entry", methods=["POST"])
@login_required
def academy_entry_add():
    """A student logs flying for the leaderboard (distance, landings,
    cross-countries, night/instrument time). Students log for themselves;
    a CFI/admin can log for any student."""
    if not (session.get("is_master_admin") or session.get("academy_access")):
        return redirect(url_for("home_launcher"))
    staff = bool(session.get("is_master_admin") or session.get("cfi_id"))
    student_id = request.form.get("student_id", type=int) if staff else session.get("student_id")
    kind = request.form.get("kind")
    try:
        value = float(request.form.get("value") or 0)
    except ValueError:
        value = 0
    entry_date = (request.form.get("entry_date") or date.today().isoformat()).strip()[:10]
    if not student_id or kind not in academy.ENTRY_KIND_MAP or value <= 0 or value > 100000:
        flash("Pick what you're logging and enter an amount above zero.", "danger")
        return redirect(url_for("academy_page") + "#log")
    conn = get_db()
    conn.execute("""INSERT INTO academy_entries (student_id, entry_date, kind, value, note, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                 (student_id, entry_date, kind, value, (request.form.get("note") or "").strip()[:200] or None,
                  session.get("user_name"), now_iso()))
    conn.commit()
    conn.close()
    code, label, unit, per = academy.ENTRY_KIND_MAP[kind]
    flash(f"Logged {value:g} {unit} - that's {value * per:g} points.", "success")
    return redirect(url_for("academy_page") + "#log")


@app.route("/academy/entry/<int:entry_id>/delete", methods=["POST"])
@login_required
def academy_entry_delete(entry_id):
    """Remove a leaderboard entry - your own, or any if you're a CFI/admin."""
    staff = bool(session.get("is_master_admin") or session.get("cfi_id"))
    conn = get_db()
    e = conn.execute("SELECT * FROM academy_entries WHERE id = ?", (entry_id,)).fetchone()
    if e and (staff or e["student_id"] == session.get("student_id")):
        conn.execute("DELETE FROM academy_entries WHERE id = ?", (entry_id,))
        conn.commit()
        flash("Entry removed.", "success")
    conn.close()
    return redirect(url_for("academy_page") + "#log")

CATEGORIES_DEFAULT = ["Fasteners", "Electrical", "Plumbing", "Bearings/Belts", "Lubricants",
                       "Safety/PPE", "Tools", "HVAC", "Welding", "Hydraulics", "General"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def part_to_dict(row, include_cost=True):
    if not row:
        return None
    d = dict(row)
    if not include_cost:
        d.pop("unit_cost", None)
        d.pop("sell_price", None)
    return d


def get_part_by_barcode(conn, barcode):
    # A retired part (see part_retire) keeps its barcode on file for history,
    # but can't be scanned in/out anymore.
    barcode = barcode.strip()
    part = conn.execute("SELECT * FROM parts WHERE barcode = ? AND retired_at IS NULL", (barcode,)).fetchone()
    if part:
        return part
    # A USB scanner types like a keyboard, so Caps Lock on the shop PC flips
    # every letter (SHOP-AB12 arrives as shop-ab12). Fall back to ignoring
    # case, but only when that still points at exactly one part.
    rows = conn.execute("SELECT * FROM parts WHERE barcode = ? COLLATE NOCASE AND retired_at IS NULL LIMIT 2",
                        (barcode,)).fetchall()
    return rows[0] if len(rows) == 1 else None


def get_low_stock(conn):
    return conn.execute(
        "SELECT * FROM parts WHERE qty_on_hand <= reorder_point ORDER BY name"
    ).fetchall()



# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _fleet_maintenance_reminders(conn):
    """Every active maintenance item across the fleet that's overdue or due
    soon, worst-first - shown on the main dashboard and on My Tasks (see
    tech_spot()) so any tech can see what needs doing, not just an admin."""
    maint_rows = conn.execute("""
        SELECT mi.*, a.tag as asset_tag, a.name as asset_name,
               a.hobbs_hours as asset_hobbs_hours, a.tach_hours as asset_tach_hours
        FROM maintenance_items mi
        JOIN assets a ON a.id = mi.asset_id
        WHERE mi.active = 1 AND a.deleted_at IS NULL
    """).fetchall()
    reminders = []
    for m in maint_rows:
        current = m["asset_tach_hours"] if m["hour_type"] != "hobbs" else m["asset_hobbs_hours"]
        status = maintenance_status(m, current)
        if status["urgency"] in ("overdue", "due_soon"):
            reminders.append({"item": m, "status": status})
    reminders.sort(key=lambda r: (0 if r["status"]["urgency"] == "overdue" else 1,
                                   r["status"]["remaining"] if r["status"]["remaining"] is not None else 0))
    return reminders


@app.route("/shop")
@shop_role_required('admin', 'tech', 'apprentice', 'inspector')
def dashboard():
    conn = get_db()
    project_count = conn.execute(
        "SELECT COUNT(*) c FROM projects WHERE status='active' AND deleted_at IS NULL"
    ).fetchone()["c"]
    pending_orders_count = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE status='pending'"
    ).fetchone()["c"]
    low_stock = get_low_stock(conn)
    recent_tx = conn.execute("""
        SELECT t.*, p.name as part_name, p.barcode as part_barcode, pr.name as project_name
        FROM transactions t
        JOIN parts p ON p.id = t.part_id
        LEFT JOIN projects pr ON pr.id = t.project_id
        ORDER BY t.created_at DESC, t.id DESC
        LIMIT 9
    """).fetchall()

    # Maintenance reminders, regardless of what project is currently open.
    reminders = _fleet_maintenance_reminders(conn)
    tool_reminders = _tools_due_reminders(conn)

    # Upcoming scheduled projects, regardless of what project is currently open.
    today_str = date.today().strftime("%Y-%m-%d")
    upcoming_projects = conn.execute("""
        SELECT projects.*, a.tag as asset_display_tag
        FROM projects LEFT JOIN assets a ON a.id = projects.asset_id
        WHERE projects.deleted_at IS NULL AND projects.scheduled_date IS NOT NULL
              AND projects.scheduled_date >= ? AND projects.status NOT IN ('completed', 'archived')
        ORDER BY projects.scheduled_date
        LIMIT 6
    """, (today_str,)).fetchall()
    upcoming_count = conn.execute("""
        SELECT COUNT(*) c FROM projects
        WHERE deleted_at IS NULL AND scheduled_date IS NOT NULL
              AND scheduled_date >= ? AND status NOT IN ('completed', 'archived')
    """, (today_str,)).fetchone()["c"]
    # QA finding ux-shop-dash-by-role: an Apprentice or Inspector's shop home
    # is reshaped around the one thing they actually do - open a project -
    # instead of the admin's full page, most of which just bounces them
    # back (they can't open Parts, Orders, Squawks or Activity). See
    # is_limited_role in dashboard.html.
    active_projects = []
    if not session.get("is_master_admin") and session.get("shop_role") in ("apprentice", "inspector"):
        active_projects = conn.execute("""
            SELECT projects.*, a.tag as asset_display_tag
            FROM projects LEFT JOIN assets a ON a.id = projects.asset_id
            WHERE projects.status = 'active' AND projects.deleted_at IS NULL
            ORDER BY projects.created_at DESC
        """).fetchall()
    open_squawks = get_open_squawks(conn)
    assignable_workers = get_assignable_workers(conn)
    # Assigned-squawk alerts: this account's own work list (every squawk
    # assigned to them not yet signed off, same as My Tasks - see
    # get_my_squawks), plus - for admin/master admin - everyone's
    # unacknowledged assignments, so whoever did the assigning can see who
    # hasn't picked it up.
    my_squawks = get_my_squawks(conn, session["user_id"]) if session.get("user_id") else []
    unacknowledged_assignments = []
    system_alerts = []
    if session.get("is_master_admin") or session.get("shop_role") == "admin":
        unacknowledged_assignments = get_unacknowledged_assignments(conn)
        system_alerts = conn.execute(
            "SELECT * FROM system_alerts WHERE resolved_at IS NULL ORDER BY created_at DESC"
        ).fetchall()
    # An Inspector's actual to-do: repairs waiting on their sign-off. Shown
    # to admins too, since they can confirm a repair as well.
    squawks_awaiting_confirm = []
    if session.get("is_master_admin") or session.get("shop_role") in ("admin", "inspector"):
        squawks_awaiting_confirm = get_squawks_awaiting_confirm(conn)

    # QA finding ux-shop-stat-boxes-by-role: a Tech's Pending Orders stat box
    # opened a page they can't see (Orders is admin-only), so it swaps for
    # Open Squawks instead - something a Tech actually acts on.
    open_squawk_count = 0
    if not session.get("is_master_admin") and session.get("shop_role") == "tech":
        open_squawk_count = get_open_squawk_count(conn)

    # Customer portal: appointments the customer asked to reschedule -
    # stays here until an admin dismisses it (see project_reschedule_dismiss).
    reschedule_requests = conn.execute("""
        SELECT projects.*, a.tag as asset_display_tag
        FROM projects LEFT JOIN assets a ON a.id = projects.asset_id
        WHERE projects.deleted_at IS NULL AND projects.customer_reschedule_requested_at IS NOT NULL
        ORDER BY projects.customer_reschedule_requested_at DESC
    """).fetchall()

    open_sessions = conn.execute("""
        SELECT ls.*, l.name as laborer_name, p.code as project_code, p.name as project_name
        FROM labor_sessions ls
        JOIN laborers l ON l.id = ls.laborer_id
        JOIN projects p ON p.id = ls.project_id
        WHERE ls.ended_at IS NULL
        ORDER BY ls.started_at
    """).fetchall()

    # Sub areas marked ready but not yet confirmed - an Inspector's queue,
    # also shown to admins since they can confirm too. See
    # project_section_complete/project_section_confirm.
    needs_confirm_sections = []
    if session.get("is_master_admin") or session.get("shop_role") in ("admin", "inspector"):
        needs_confirm_sections = conn.execute("""
            SELECT ps.*, p.id as project_id, p.code as project_code, p.name as project_name
            FROM project_sections ps
            JOIN projects p ON p.id = ps.project_id
            WHERE ps.confirm_requested_at IS NOT NULL AND ps.completed_at IS NULL AND p.deleted_at IS NULL
            ORDER BY ps.confirm_requested_at
        """).fetchall()

    # Today's completed labor entries, folded into Recent Activity alongside
    # part transactions - resets naturally each day since it's filtered to today.
    recent_labor = conn.execute("""
        SELECT ls.*, l.name as laborer_name, p.code as project_code, p.name as project_name
        FROM labor_sessions ls
        JOIN laborers l ON l.id = ls.laborer_id
        JOIN projects p ON p.id = ls.project_id
        WHERE ls.ended_at IS NOT NULL AND date(ls.ended_at) = ?
        ORDER BY ls.ended_at DESC
        LIMIT 9
    """, (today_str,)).fetchall()
    conn.close()

    # Merge part transactions and labor clock in/out entries into one
    # chronological "Recent Activity" feed for the dashboard.
    recent_activity = []
    for t in recent_tx:
        recent_activity.append({
            "when": t["created_at"], "kind": "part",
            "tx_type": t["type"], "part_id": t["part_id"], "part_name": t["part_name"],
            "qty": t["qty"], "project_id": t["project_id"], "project_name": t["project_name"],
        })
    for s in recent_labor:
        recent_activity.append({
            "when": s["ended_at"], "kind": "labor",
            "laborer_name": s["laborer_name"], "project_id": s["project_id"],
            "project_code": s["project_code"], "section": s["section"], "hours": s["hours"],
        })
    recent_activity.sort(key=lambda a: a["when"] or "", reverse=True)
    recent_activity = recent_activity[:9]

    return render_template("dashboard.html", project_count=project_count,
                           pending_orders_count=pending_orders_count, upcoming_count=upcoming_count,
                           low_stock=low_stock, recent_activity=recent_activity,
                           reminders=reminders, upcoming_projects=upcoming_projects,
                           reschedule_requests=reschedule_requests,
                           open_squawks=open_squawks, assignable_workers=assignable_workers, open_sessions=open_sessions,
                           needs_confirm_sections=needs_confirm_sections,
                           my_squawks=my_squawks,
                           unacknowledged_assignments=unacknowledged_assignments,
                           squawks_awaiting_confirm=squawks_awaiting_confirm,
                           system_alerts=system_alerts, tool_reminders=tool_reminders,
                           active_projects=active_projects, open_squawk_count=open_squawk_count)


# ---------------------------------------------------------------------------
# Flight School squawks, surfaced here on the Maintenance side - a CFI flags
# one when logging a flight, and it stays front-and-center until someone on
# the shop side acknowledges it.
# ---------------------------------------------------------------------------

# Squawks come from two places: flagged during Flight School's "log a
# flight" flow (flights.squawk), or reported directly against a plane
# without logging a flight (plane_squawks - see asset_squawk_new()). Every
# place squawks are listed merges the two with a UNION ALL so they show up
# together, tagged with a 'kind' column ('flight' or 'quick') that the
# acknowledge/repair routes use to know which table to update.
_FLIGHT_SQUAWK_COLS = """'flight' as kind, f.id as squawk_id, a.id as asset_id, a.tag as asset_tag,
               a.name as asset_name, f.flight_date as event_date, s.name as student_name,
               c.name as cfi_name, NULL as reported_by, f.notes as notes,
               f.squawk_acknowledged_at as acknowledged_at, f.squawk_acknowledged_by as acknowledged_by,
               f.squawk_repaired_at as repaired_at, f.squawk_repaired_by as repaired_by,
               f.squawk_assigned_to as assigned_to, au.name as assigned_to_name,
               f.squawk_worker_acknowledged_at as worker_acknowledged_at,
               f.squawk_worker_acknowledged_by as worker_acknowledged_by,
               f.squawk_repair_confirm_requested_at as repair_confirm_requested_at,
               f.squawk_repair_confirm_requested_by as repair_confirm_requested_by,
               f.squawk_sent_back_note as sent_back_note"""
_QUICK_SQUAWK_COLS = """'quick' as kind, q.id as squawk_id, a.id as asset_id, a.tag as asset_tag,
               a.name as asset_name, q.reported_at as event_date, NULL as student_name,
               NULL as cfi_name, q.reported_by as reported_by, q.notes as notes,
               q.acknowledged_at as acknowledged_at, q.acknowledged_by as acknowledged_by,
               q.repaired_at as repaired_at, q.repaired_by as repaired_by,
               q.assigned_to as assigned_to, au.name as assigned_to_name,
               q.worker_acknowledged_at as worker_acknowledged_at,
               q.worker_acknowledged_by as worker_acknowledged_by,
               q.repair_confirm_requested_at as repair_confirm_requested_at,
               q.repair_confirm_requested_by as repair_confirm_requested_by,
               q.sent_back_note as sent_back_note"""

# Reported -> Assigned -> Working -> Inspection -> Done - the same five
# steps and order the step pills show everywhere a squawk appears (see
# squawk_step_pills in templates/_squawk_macros.html, which this mirrors).
SQUAWK_STEPS = ["new", "assigned", "working", "inspection", "done"]


def squawk_step_index(sq):
    if sq["repaired_at"]:
        return 4
    if sq["repair_confirm_requested_at"]:
        return 3
    if not sq["acknowledged_at"]:
        return 0
    if sq["worker_acknowledged_at"] or not sq["assigned_to"]:
        return 2
    return 1


def get_open_squawks(conn):
    return conn.execute(f"""
        SELECT {_FLIGHT_SQUAWK_COLS}
        FROM flights f
        JOIN assets a ON a.id = f.asset_id
        JOIN students s ON s.id = f.student_id
        LEFT JOIN cfis c ON c.id = f.cfi_id
        LEFT JOIN users au ON au.id = f.squawk_assigned_to
        WHERE f.squawk = 1 AND f.squawk_acknowledged_at IS NULL
        UNION ALL
        SELECT {_QUICK_SQUAWK_COLS}
        FROM plane_squawks q
        JOIN assets a ON a.id = q.asset_id
        LEFT JOIN users au ON au.id = q.assigned_to
        WHERE q.acknowledged_at IS NULL
        ORDER BY event_date DESC, squawk_id DESC
    """).fetchall()


def get_open_squawk_count(conn):
    """Every squawk not yet marked repaired, across both tables - the
    Tech's dashboard stat box (QA finding ux-shop-stat-boxes-by-role), which
    stands in for Pending Orders since Techs can't see Orders."""
    return conn.execute("""
        SELECT (SELECT COUNT(*) FROM flights WHERE squawk = 1 AND squawk_repaired_at IS NULL) +
               (SELECT COUNT(*) FROM plane_squawks WHERE repaired_at IS NULL) AS c
    """).fetchone()["c"]


def get_plane_open_squawks(conn, asset_id):
    """Every one of this plane's squawks that isn't signed off yet (any
    step through Inspection), not just brand-new ones - the plane page's
    To-Do list and the project page's squawks & to-dos box (QA finding
    ux-squawk-on-project) both use this, unlike get_open_squawks() above,
    which is just the unacknowledged ones."""
    return conn.execute(f"""
        SELECT {_FLIGHT_SQUAWK_COLS}
        FROM flights f
        JOIN assets a ON a.id = f.asset_id
        JOIN students s ON s.id = f.student_id
        LEFT JOIN cfis c ON c.id = f.cfi_id
        LEFT JOIN users au ON au.id = f.squawk_assigned_to
        WHERE f.squawk = 1 AND f.squawk_repaired_at IS NULL AND a.id = ?
        UNION ALL
        SELECT {_QUICK_SQUAWK_COLS}
        FROM plane_squawks q
        JOIN assets a ON a.id = q.asset_id
        LEFT JOIN users au ON au.id = q.assigned_to
        WHERE q.repaired_at IS NULL AND a.id = ?
        ORDER BY event_date DESC, squawk_id DESC
    """, (asset_id, asset_id)).fetchall()


def get_plane_done_squawks(conn, asset_id):
    """This plane's completed (repaired) squawks, newest first - the plane
    page's Squawks history, with the same columns as the Squawks page's Done
    list."""
    return conn.execute(f"""
        SELECT {_FLIGHT_SQUAWK_COLS}
        FROM flights f
        JOIN assets a ON a.id = f.asset_id
        JOIN students s ON s.id = f.student_id
        LEFT JOIN cfis c ON c.id = f.cfi_id
        LEFT JOIN users au ON au.id = f.squawk_assigned_to
        WHERE f.squawk = 1 AND f.squawk_repaired_at IS NOT NULL AND a.id = ?
        UNION ALL
        SELECT {_QUICK_SQUAWK_COLS}
        FROM plane_squawks q
        JOIN assets a ON a.id = q.asset_id
        LEFT JOIN users au ON au.id = q.assigned_to
        WHERE q.repaired_at IS NOT NULL AND a.id = ?
        ORDER BY repaired_at DESC
    """, (asset_id, asset_id)).fetchall()


def _squawk_by_kind_id(conn, kind, squawk_id):
    """One specific squawk, whatever step it's on (unlike
    get_plane_open_squawks, this doesn't drop it once it's repaired) - used
    to show its Reported/Assigned/Working/Inspection/Done pills and assigned
    tech on a Discrepancy List item it's linked to (QA finding
    ux-squawk-on-project, idea "Reported Assigned Working Inspection Done")."""
    if kind == "flight":
        row = conn.execute(f"""
            SELECT {_FLIGHT_SQUAWK_COLS}
            FROM flights f
            JOIN assets a ON a.id = f.asset_id
            JOIN students s ON s.id = f.student_id
            LEFT JOIN cfis c ON c.id = f.cfi_id
            LEFT JOIN users au ON au.id = f.squawk_assigned_to
            WHERE f.id = ?""", (squawk_id,)).fetchone()
    else:
        row = conn.execute(f"""
            SELECT {_QUICK_SQUAWK_COLS}
            FROM plane_squawks q
            JOIN assets a ON a.id = q.asset_id
            LEFT JOIN users au ON au.id = q.assigned_to
            WHERE q.id = ?""", (squawk_id,)).fetchone()
    return dict(row) if row else None


def _todo_by_id(conn, todo_id):
    """One specific plane to-do, whatever step it's on - the to-do
    equivalent of _squawk_by_kind_id above."""
    row = conn.execute("""SELECT pt.*, u.name as assigned_to_name FROM plane_todos pt
                          LEFT JOIN users u ON u.id = pt.assigned_to WHERE pt.id = ?""",
                       (todo_id,)).fetchone()
    return dict(row) if row else None


def get_assignable_workers(conn):
    """Techs/admins a squawk can be handed off to - see squawk_assign()."""
    return conn.execute(
        "SELECT id, name FROM users WHERE active = 1 AND shop_role IN ('admin', 'tech') ORDER BY name"
    ).fetchall()


def get_unacknowledged_assignments(conn):
    """Squawks assigned to someone who hasn't acknowledged the assignment yet
    (and isn't already repaired) - the admin dashboard alert for this."""
    return conn.execute(f"""
        SELECT {_FLIGHT_SQUAWK_COLS}
        FROM flights f
        JOIN assets a ON a.id = f.asset_id
        JOIN students s ON s.id = f.student_id
        LEFT JOIN cfis c ON c.id = f.cfi_id
        LEFT JOIN users au ON au.id = f.squawk_assigned_to
        WHERE f.squawk = 1 AND f.squawk_repaired_at IS NULL
              AND f.squawk_assigned_to IS NOT NULL AND f.squawk_worker_acknowledged_at IS NULL
        UNION ALL
        SELECT {_QUICK_SQUAWK_COLS}
        FROM plane_squawks q
        JOIN assets a ON a.id = q.asset_id
        LEFT JOIN users au ON au.id = q.assigned_to
        WHERE q.repaired_at IS NULL AND q.assigned_to IS NOT NULL AND q.worker_acknowledged_at IS NULL
        ORDER BY event_date DESC, squawk_id DESC
    """).fetchall()


def get_squawks_awaiting_confirm(conn):
    """Squawks a tech has marked ready for repair, still waiting on an
    Inspector or admin to sign off (squawk_repair_confirm) - the dashboard
    alert that gives an Inspector something to actually do, since the
    pages the Confirm button used to live on (Squawks, a plane's page) were
    off-limits to them (QA finding ux-squawk-inspector-signoff)."""
    return conn.execute(f"""
        SELECT {_FLIGHT_SQUAWK_COLS}
        FROM flights f
        JOIN assets a ON a.id = f.asset_id
        JOIN students s ON s.id = f.student_id
        LEFT JOIN cfis c ON c.id = f.cfi_id
        LEFT JOIN users au ON au.id = f.squawk_assigned_to
        WHERE f.squawk = 1 AND f.squawk_repair_confirm_requested_at IS NOT NULL
        UNION ALL
        SELECT {_QUICK_SQUAWK_COLS}
        FROM plane_squawks q
        JOIN assets a ON a.id = q.asset_id
        LEFT JOIN users au ON au.id = q.assigned_to
        WHERE q.repair_confirm_requested_at IS NOT NULL
        ORDER BY event_date DESC, squawk_id DESC
    """).fetchall()


def get_my_squawks(conn, user_id):
    """Every squawk assigned to this user that isn't repaired yet, accepted
    or not - the full working list for their My Tasks page and the
    dashboard's My Squawks box (both show the same rows and buttons - see
    QA finding ux-squawk-my-list-dashboard)."""
    return conn.execute(f"""
        SELECT {_FLIGHT_SQUAWK_COLS}
        FROM flights f
        JOIN assets a ON a.id = f.asset_id
        JOIN students s ON s.id = f.student_id
        LEFT JOIN cfis c ON c.id = f.cfi_id
        LEFT JOIN users au ON au.id = f.squawk_assigned_to
        WHERE f.squawk = 1 AND f.squawk_repaired_at IS NULL AND f.squawk_assigned_to = ?
        UNION ALL
        SELECT {_QUICK_SQUAWK_COLS}
        FROM plane_squawks q
        JOIN assets a ON a.id = q.asset_id
        LEFT JOIN users au ON au.id = q.assigned_to
        WHERE q.repaired_at IS NULL AND q.assigned_to = ?
        ORDER BY event_date DESC, squawk_id DESC
    """, (user_id, user_id)).fetchall()


def get_my_done_squawks(conn, user_id, limit=25):
    """Squawks assigned to this user that are repaired, newest first - the
    completed squawks shown beside completed to-dos on My Tasks."""
    return conn.execute(f"""
        SELECT {_FLIGHT_SQUAWK_COLS}
        FROM flights f
        JOIN assets a ON a.id = f.asset_id
        JOIN students s ON s.id = f.student_id
        LEFT JOIN cfis c ON c.id = f.cfi_id
        LEFT JOIN users au ON au.id = f.squawk_assigned_to
        WHERE f.squawk = 1 AND f.squawk_repaired_at IS NOT NULL AND f.squawk_assigned_to = ?
        UNION ALL
        SELECT {_QUICK_SQUAWK_COLS}
        FROM plane_squawks q
        JOIN assets a ON a.id = q.asset_id
        LEFT JOIN users au ON au.id = q.assigned_to
        WHERE q.repaired_at IS NOT NULL AND q.assigned_to = ?
        ORDER BY repaired_at DESC LIMIT ?
    """, (user_id, user_id, limit)).fetchall()


@app.route("/squawks")
@shop_role_required('admin', 'tech', 'inspector')
def squawks_list():
    conn = get_db()
    open_squawks = get_open_squawks(conn)
    acknowledged = conn.execute(f"""
        SELECT {_FLIGHT_SQUAWK_COLS}
        FROM flights f
        JOIN assets a ON a.id = f.asset_id
        JOIN students s ON s.id = f.student_id
        LEFT JOIN cfis c ON c.id = f.cfi_id
        LEFT JOIN users au ON au.id = f.squawk_assigned_to
        WHERE f.squawk = 1 AND f.squawk_acknowledged_at IS NOT NULL AND f.squawk_repaired_at IS NULL
        UNION ALL
        SELECT {_QUICK_SQUAWK_COLS}
        FROM plane_squawks q
        JOIN assets a ON a.id = q.asset_id
        LEFT JOIN users au ON au.id = q.assigned_to
        WHERE q.acknowledged_at IS NOT NULL AND q.repaired_at IS NULL
        ORDER BY acknowledged_at DESC LIMIT 50
    """).fetchall()
    repaired = conn.execute(f"""
        SELECT {_FLIGHT_SQUAWK_COLS}
        FROM flights f
        JOIN assets a ON a.id = f.asset_id
        JOIN students s ON s.id = f.student_id
        LEFT JOIN cfis c ON c.id = f.cfi_id
        LEFT JOIN users au ON au.id = f.squawk_assigned_to
        WHERE f.squawk = 1 AND f.squawk_repaired_at IS NOT NULL
        UNION ALL
        SELECT {_QUICK_SQUAWK_COLS}
        FROM plane_squawks q
        JOIN assets a ON a.id = q.asset_id
        LEFT JOIN users au ON au.id = q.assigned_to
        WHERE q.repaired_at IS NOT NULL
        ORDER BY repaired_at DESC LIMIT 50
    """).fetchall()
    assets = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_simulator = 0 AND is_owner_placeholder = 0 ORDER BY tag").fetchall()
    assignable_workers = get_assignable_workers(conn)
    conn.close()

    # One list at a time, filtered by step, instead of 3 stacked lists that
    # each mixed several steps together (QA finding ux-squawk-page-steps).
    # open_squawks is always step "new" and repaired always "done"; the old
    # "acknowledged" list actually mixed assigned/working/inspection, so it
    # gets bucketed the same way.
    buckets = {step: [] for step in SQUAWK_STEPS}
    for sq in list(open_squawks) + list(acknowledged) + list(repaired):
        buckets[SQUAWK_STEPS[squawk_step_index(sq)]].append(sq)
    step_counts = {step: len(rows) for step, rows in buckets.items()}
    current_step = request.args.get("step")
    if current_step not in SQUAWK_STEPS:
        current_step = next((s for s in SQUAWK_STEPS if step_counts[s]), "new")

    return render_template("squawks.html", step_counts=step_counts, current_step=current_step,
                           squawks_for_step=buckets[current_step],
                           assets=assets, assignable_workers=assignable_workers,
                           show_report_form=request.args.get("report") == "1")


@app.route("/squawks/new", methods=["POST"])
@shop_role_required('admin', 'tech')
def squawk_quick_new():
    """Report an issue against a plane right from the Squawks page itself,
    without going through the plane's own page first - same plane_squawks
    row as asset_squawk_new(), just filed from here instead."""
    asset_id = request.form.get("asset_id", type=int)
    notes = request.form.get("notes", "").strip()
    if not asset_id:
        flash("Pick a plane before reporting.", "danger")
        return redirect(url_for("squawks_list"))
    if not notes:
        flash("Enter what's wrong before reporting.", "danger")
        return redirect(url_for("squawks_list"))
    conn = get_db()
    asset = conn.execute("SELECT id, deleted_at FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    if asset["deleted_at"]:
        conn.close()
        flash("That plane is in the trash.", "danger")
        return redirect(url_for("squawks_list"))
    conn.execute("INSERT INTO plane_squawks (asset_id, notes, reported_by, reported_at) VALUES (?, ?, ?, ?)",
                 (asset_id, notes, session.get("user_name"), now_iso()))
    conn.commit()
    conn.close()
    flash("Issue reported.", "success")
    return redirect(url_for("squawks_list"))


@app.route("/squawks/<kind>/<int:squawk_id>/acknowledge", methods=["POST"])
@shop_role_required('admin', 'tech')
def squawk_acknowledge(kind, squawk_id):
    conn = get_db()
    if kind == "flight":
        row = conn.execute("SELECT id FROM flights WHERE id = ? AND squawk = 1", (squawk_id,)).fetchone()
        set_sql = "UPDATE flights SET squawk_acknowledged_at = ?, squawk_acknowledged_by = ? WHERE id = ?"
    elif kind == "quick":
        row = conn.execute("SELECT id FROM plane_squawks WHERE id = ?", (squawk_id,)).fetchone()
        set_sql = "UPDATE plane_squawks SET acknowledged_at = ?, acknowledged_by = ? WHERE id = ?"
    else:
        conn.close()
        abort(404)
    if not row:
        conn.close()
        flash("Squawk not found.", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    conn.execute(set_sql, (now_iso(), session.get("user_name"), squawk_id))
    # A new squawk's only action is "Assign to..." or "I'll take it" (see
    # squawk_new_actions in _squawk_macros.html) - picking a name or taking
    # it acknowledges and assigns in the same request, so it always leaves
    # here with an owner. squawk_assign() below still handles a later
    # reassignment on its own, separate from acknowledging.
    assigned_to_raw = request.form.get("assigned_to")
    assigned_ok = _apply_squawk_assignment(conn, kind, squawk_id, assigned_to_raw)
    conn.commit()
    conn.close()
    if not assigned_ok:
        flash("Squawk acknowledged, but pick a shop worker to assign it.", "danger")
        return redirect(request.referrer or url_for("squawks_list"))
    flash("Assigned and acknowledged." if (assigned_to_raw or "").strip() else "Squawk acknowledged.", "success")
    return redirect(request.referrer or url_for("squawks_list"))


def _apply_squawk_assignment(conn, kind, squawk_id, assigned_to_raw):
    """Shared by squawk_acknowledge (optional assign-while-acknowledging) and
    squawk_assign (assign/reassign/unassign on its own). Reassigning always
    clears any previous worker acknowledgement - the new person hasn't seen
    it yet, whatever the last one did."""
    assigned_to_raw = (assigned_to_raw or "").strip()
    if not assigned_to_raw:
        return True
    assigned_to = int(assigned_to_raw) if assigned_to_raw.isdigit() else None
    if assigned_to not in {w["id"] for w in get_assignable_workers(conn)}:
        return False
    if kind == "flight":
        conn.execute("UPDATE flights SET squawk_assigned_to = ?, squawk_worker_acknowledged_at = NULL, "
                     "squawk_worker_acknowledged_by = NULL WHERE id = ?", (assigned_to, squawk_id))
    elif kind == "quick":
        conn.execute("UPDATE plane_squawks SET assigned_to = ?, worker_acknowledged_at = NULL, "
                     "worker_acknowledged_by = NULL WHERE id = ?", (assigned_to, squawk_id))
    return True


@app.route("/squawks/<kind>/<int:squawk_id>/assign", methods=["POST"])
@shop_role_required('admin', 'tech')
def squawk_assign(kind, squawk_id):
    """Hands a squawk off to a specific tech (or clears the assignment with
    an empty pick) - separate from Acknowledge, so it can also be done (or
    changed) later while it's sitting in Acknowledged, Not Yet Repaired."""
    conn = get_db()
    if kind == "flight":
        row = conn.execute("SELECT id FROM flights WHERE id = ? AND squawk = 1", (squawk_id,)).fetchone()
    elif kind == "quick":
        row = conn.execute("SELECT id FROM plane_squawks WHERE id = ?", (squawk_id,)).fetchone()
    else:
        conn.close()
        abort(404)
    if not row:
        conn.close()
        flash("Squawk not found.", "danger")
        return redirect(request.referrer or url_for("squawks_list"))
    assigned_to_raw = request.form.get("assigned_to", "").strip()
    assigned_to = int(assigned_to_raw) if assigned_to_raw.isdigit() else None
    if assigned_to_raw and assigned_to not in {w["id"] for w in get_assignable_workers(conn)}:
        conn.close()
        flash("Pick a shop worker.", "danger")
        return redirect(request.referrer or url_for("squawks_list"))
    if kind == "flight":
        conn.execute("UPDATE flights SET squawk_assigned_to = ?, squawk_worker_acknowledged_at = NULL, "
                     "squawk_worker_acknowledged_by = NULL WHERE id = ?", (assigned_to, squawk_id))
    else:
        conn.execute("UPDATE plane_squawks SET assigned_to = ?, worker_acknowledged_at = NULL, "
                     "worker_acknowledged_by = NULL WHERE id = ?", (assigned_to, squawk_id))
    conn.commit()
    conn.close()
    flash("Assigned." if assigned_to else "Assignment cleared.", "success")
    return redirect(request.referrer or url_for("squawks_list"))


@app.route("/squawks/<kind>/<int:squawk_id>/worker_ack", methods=["POST"])
@shop_role_required('admin', 'tech')
def squawk_worker_ack(kind, squawk_id):
    """The assigned tech's own "I've got it" - separate from an admin's
    Acknowledge above, which just means someone's seen the squawk exists.
    Only the person it's assigned to (or a master admin) can do this."""
    conn = get_db()
    if kind == "flight":
        row = conn.execute("SELECT id, squawk_assigned_to as assigned_to FROM flights WHERE id = ? AND squawk = 1",
                            (squawk_id,)).fetchone()
        set_sql = "UPDATE flights SET squawk_worker_acknowledged_at = ?, squawk_worker_acknowledged_by = ? WHERE id = ?"
    elif kind == "quick":
        row = conn.execute("SELECT id, assigned_to FROM plane_squawks WHERE id = ?", (squawk_id,)).fetchone()
        set_sql = "UPDATE plane_squawks SET worker_acknowledged_at = ?, worker_acknowledged_by = ? WHERE id = ?"
    else:
        conn.close()
        abort(404)
    if not row:
        conn.close()
        flash("Squawk not found.", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    if row["assigned_to"] != session.get("user_id") and not session.get("is_master_admin"):
        conn.close()
        flash("This squawk isn't assigned to you.", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    conn.execute(set_sql, (now_iso(), session.get("user_name"), squawk_id))
    conn.commit()
    conn.close()
    flash("Got it - marked as accepted.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/squawks/<kind>/<int:squawk_id>/repair", methods=["POST"])
@shop_role_required('admin', 'tech')
def squawk_repair(kind, squawk_id):
    """Marking a squawk repaired only requests confirmation now - same
    request/confirm two-step a project sub area requires (see
    project_section_complete/project_section_confirm): it stays in
    Acknowledged, Not Yet Repaired, flagged as awaiting confirmation, until
    an Inspector or admin signs off via squawk_repair_confirm. Also
    acknowledges it if that hadn't happened yet, so a tech can jump straight
    to "repaired" without an extra click.

    Hitting this same button again while already awaiting confirmation
    undoes the request instead (My Tasks greys the button out and toggles
    it back), so a tech can take back an accidental Mark Repaired without
    needing an Inspector to Send Back."""
    conn = get_db()
    if kind == "flight":
        row = conn.execute("SELECT id, squawk_acknowledged_at as acked, "
                            "squawk_repair_confirm_requested_at as confirm_requested FROM flights "
                            "WHERE id = ? AND squawk = 1", (squawk_id,)).fetchone()
        ack_sql = "UPDATE flights SET squawk_acknowledged_at = ?, squawk_acknowledged_by = ? WHERE id = ?"
        confirm_req_sql = ("UPDATE flights SET squawk_repair_confirm_requested_at = ?, "
                            "squawk_repair_confirm_requested_by = ?, squawk_sent_back_note = NULL WHERE id = ?")
        undo_sql = "UPDATE flights SET squawk_repair_confirm_requested_at = NULL, squawk_repair_confirm_requested_by = NULL WHERE id = ?"
    elif kind == "quick":
        row = conn.execute("SELECT id, acknowledged_at as acked, "
                            "repair_confirm_requested_at as confirm_requested FROM plane_squawks "
                            "WHERE id = ?", (squawk_id,)).fetchone()
        ack_sql = "UPDATE plane_squawks SET acknowledged_at = ?, acknowledged_by = ? WHERE id = ?"
        confirm_req_sql = ("UPDATE plane_squawks SET repair_confirm_requested_at = ?, "
                            "repair_confirm_requested_by = ?, sent_back_note = NULL WHERE id = ?")
        undo_sql = "UPDATE plane_squawks SET repair_confirm_requested_at = NULL, repair_confirm_requested_by = NULL WHERE id = ?"
    else:
        conn.close()
        abort(404)
    if not row:
        conn.close()
        flash("Squawk not found.", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    if row["confirm_requested"]:
        conn.execute(undo_sql, (squawk_id,))
        conn.commit()
        conn.close()
        flash("Un-marked - back to not yet repaired.", "warning")
        return redirect(request.referrer or url_for("dashboard"))
    if not row["acked"]:
        conn.execute(ack_sql, (now_iso(), session.get("user_name"), squawk_id))
    conn.execute(confirm_req_sql, (now_iso(), session.get("user_name"), squawk_id))
    conn.commit()
    conn.close()
    flash("Marked ready for repair confirmation - an Inspector or admin needs to confirm it.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/squawks/<kind>/<int:squawk_id>/repair_confirm", methods=["POST"])
@shop_role_required('admin', 'inspector')
def squawk_repair_confirm(kind, squawk_id):
    """An Inspector (or admin) signs off on a squawk someone else marked
    ready to repair - this is what actually marks it repaired. 'Send back'
    clears the request instead, so whoever fixed it knows it wasn't
    approved (same pattern as project_section_confirm)."""
    conn = get_db()
    if kind == "flight":
        row = conn.execute("SELECT id FROM flights WHERE id = ? AND squawk = 1", (squawk_id,)).fetchone()
        send_back_sql = ("UPDATE flights SET squawk_repair_confirm_requested_at = NULL, "
                          "squawk_repair_confirm_requested_by = NULL, squawk_sent_back_note = ? WHERE id = ?")
        confirm_sql = "UPDATE flights SET squawk_repaired_at = ?, squawk_repaired_by = ?, squawk_repair_confirm_requested_at = NULL, squawk_repair_confirm_requested_by = NULL WHERE id = ?"
    elif kind == "quick":
        row = conn.execute("SELECT id FROM plane_squawks WHERE id = ?", (squawk_id,)).fetchone()
        send_back_sql = ("UPDATE plane_squawks SET repair_confirm_requested_at = NULL, "
                          "repair_confirm_requested_by = NULL, sent_back_note = ? WHERE id = ?")
        confirm_sql = "UPDATE plane_squawks SET repaired_at = ?, repaired_by = ?, repair_confirm_requested_at = NULL, repair_confirm_requested_by = NULL WHERE id = ?"
    else:
        conn.close()
        abort(404)
    if not row:
        conn.close()
        flash("Squawk not found.", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    if request.form.get("action") == "send_back":
        note = (request.form.get("note") or "").strip() or None
        conn.execute(send_back_sql, (note, squawk_id))
        flash("Sent back - not marked repaired.", "warning")
    else:
        conn.execute(confirm_sql, (now_iso(), session.get("user_name"), squawk_id))
        flash("Squawk marked repaired.", "success")
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("squawks_list"))


# ---------------------------------------------------------------------------
# Maintenance calendar - scheduling projects and browsing past months
# ---------------------------------------------------------------------------

DEFAULT_EVENT_COLORS = {
    "project": "#0d6efd",
    "overdue": "#dc3545",
    "due_soon": "#fd7e14",
}

# Calendar color coding by maintenance category, plus the plain "scheduled
# project" blue above - a maintenance item's own color always wins over the
# old overdue/due_soon coloring (urgency is now shown as an icon instead, so
# category and urgency can both be seen on the same chip).
# Now defined in db.py so flight.py can share them too - aliased here under
# their original names since the rest of this file already uses them.
CATEGORY_COLORS = MAINT_CATEGORY_COLORS
CATEGORY_LABELS = MAINT_CATEGORY_LABELS


def _build_month_data(conn, year, month):
    """Builds one month's calendar grid: weeks (lists of day-numbers or None)
    and by_day (day -> {"projects": [...], "maint": [...]}), including
    multi-day scheduled project blocks (scheduled_date..scheduled_end_date)
    that merely overlap this month, and calendar-type maintenance items next
    due in this month. Each project row gets a `.display_color` attribute
    resolved from scheduled_color or a status-based default."""
    first_weekday, days_in_month = calendar_mod.monthrange(year, month)  # Monday=0
    month_start = f"{year:04d}-{month:02d}-01"
    month_end = f"{year:04d}-{month:02d}-{days_in_month:02d}"

    by_day = {d: {"projects": [], "maint": []} for d in range(1, days_in_month + 1)}

    projects = conn.execute("""
        SELECT projects.*, a.tag as asset_display_tag, a.is_flight_asset as asset_is_flight_asset
        FROM projects LEFT JOIN assets a ON a.id = projects.asset_id
        WHERE projects.deleted_at IS NULL AND projects.scheduled_date IS NOT NULL
          AND projects.scheduled_date <= ?
          AND COALESCE(projects.scheduled_end_date, projects.scheduled_date) >= ?
        ORDER BY projects.scheduled_date
    """, (month_end, month_start)).fetchall()
    for p in projects:
        p = dict(p)
        p["display_color"] = p.get("scheduled_color") or DEFAULT_EVENT_COLORS["project"]
        start = p["scheduled_date"]
        end = p["scheduled_end_date"] or p["scheduled_date"]
        for d in range(1, days_in_month + 1):
            day_str = f"{year:04d}-{month:02d}-{d:02d}"
            if start <= day_str <= end:
                by_day[d]["projects"].append(p)

    maint_rows = conn.execute("""
        SELECT mi.*, a.tag as asset_tag, a.name as asset_name
        FROM maintenance_items mi JOIN assets a ON a.id = mi.asset_id
        WHERE mi.active = 1 AND mi.type = 'calendar' AND a.deleted_at IS NULL
    """).fetchall()
    for m in maint_rows:
        status = maintenance_status(m, None)
        next_due = status["next_due"]
        if next_due and next_due[:7] == f"{year:04d}-{month:02d}":
            try:
                day = int(next_due[8:10])
                category = m["category"] if m["category"] in CATEGORY_COLORS else "scheduled_maint"
                color = CATEGORY_COLORS[category]
                by_day[day]["maint"].append({"item": m, "status": status, "display_color": color, "category": category})
            except (ValueError, KeyError):
                pass

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
            "weeks": weeks, "by_day": by_day}


def _build_year_list(conn, year):
    """All scheduled projects in a given year, grouped by the month their
    start date falls in, each with a human date-range label. Used by the
    calendar's List view."""
    year_start = f"{year:04d}-01-01"
    year_end = f"{year:04d}-12-31"
    projects = conn.execute("""
        SELECT projects.*, a.tag as asset_display_tag, a.is_flight_asset as asset_is_flight_asset
        FROM projects LEFT JOIN assets a ON a.id = projects.asset_id
        WHERE projects.deleted_at IS NULL AND projects.scheduled_date IS NOT NULL
          AND projects.scheduled_date BETWEEN ? AND ?
        ORDER BY projects.scheduled_date
    """, (year_start, year_end)).fetchall()
    by_month = {m: [] for m in range(1, 13)}
    for p in projects:
        p = dict(p)
        p["display_color"] = p.get("scheduled_color") or DEFAULT_EVENT_COLORS["project"]
        start = p["scheduled_date"]
        end = p["scheduled_end_date"]
        if end and end != start:
            p["date_label"] = f"{usdate(start)} - {usdate(end)}"
        else:
            p["date_label"] = usdate(start)
        month = int(start[5:7])
        by_month.setdefault(month, []).append(p)
    return [{"month": m, "month_name": calendar_mod.month_name[m], "projects": by_month.get(m, [])}
            for m in range(1, 13)]


@app.route("/calendar")
@shop_role_required('admin', 'tech', 'apprentice', 'inspector')
def calendar_page():
    conn = get_db()
    today = date.today()
    view = request.args.get("view", "month")
    if view not in ("month", "year", "list"):
        # Also catches an old view=quarter link/bookmark (Quarter view was
        # retired - see the Idea Queue "QA fix: Schedule on a phone" idea):
        # it just opens Month instead of erroring.
        view = "month"
    try:
        year = int(request.args.get("year", today.year))
    except (TypeError, ValueError):
        year = today.year
    try:
        month = int(request.args.get("month", today.month))
    except (TypeError, ValueError):
        month = today.month
    if month < 1:
        month, year = 12, year - 1
    elif month > 12:
        month, year = 1, year + 1

    year_list = None
    if view == "month":
        months_data = [_build_month_data(conn, year, month)]
        prev_month, prev_year = (12, year - 1) if month == 1 else (month - 1, year)
        next_month, next_year = (1, year + 1) if month == 12 else (month + 1, year)
    elif view == "list":
        year_list = _build_year_list(conn, year)
        months_data = []
        prev_month, prev_year = month, year - 1
        next_month, next_year = month, year + 1
    else:  # year
        months_data = [_build_month_data(conn, year, m) for m in range(1, 13)]
        prev_month, prev_year = month, year - 1
        next_month, next_year = month, year + 1
    conn.close()

    # Backward-compat single-month convenience vars, used by the month view template.
    weeks = months_data[0]["weeks"] if months_data else []
    by_day = months_data[0]["by_day"] if months_data else {}
    month_name = months_data[0]["month_name"] if months_data else calendar_mod.month_name[month]

    return render_template("calendar.html", year=year, month=month, view=view,
                           month_name=month_name, weeks=weeks, by_day=by_day,
                           months_data=months_data, year_list=year_list,
                           today=today, prev_year=prev_year, prev_month=prev_month,
                           next_year=next_year, next_month=next_month,
                           category_colors=CATEGORY_COLORS, category_labels=CATEGORY_LABELS,
                           project_color=DEFAULT_EVENT_COLORS["project"])


# ---------------------------------------------------------------------------
# Activity log (full, sortable)
# ---------------------------------------------------------------------------

ACTIVITY_SORT_COLUMNS = {
    "date": "t.created_at",
    "part": "p.name",
    "type": "t.type",
    "qty": "t.qty",
    "cost": "cost",
    "project": "pr.name",
    "section": "t.section",
    "by": "t.performed_by",
}


@app.route("/activity")
@shop_role_required('admin', 'tech')
def activity_log():
    conn = get_db()
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "date")
    direction = request.args.get("dir", "desc")
    sort_col = ACTIVITY_SORT_COLUMNS.get(sort, "t.created_at")
    direction_sql = "ASC" if direction == "asc" else "DESC"

    query = """
        SELECT t.*, p.name as part_name, p.barcode as part_barcode, p.unit_cost,
               pr.name as project_name, (t.qty * p.unit_cost) as cost
        FROM transactions t
        JOIN parts p ON p.id = t.part_id
        LEFT JOIN projects pr ON pr.id = t.project_id
        WHERE 1=1
    """
    params = []
    if q:
        query += " AND (p.name LIKE ? OR pr.name LIKE ? OR t.performed_by LIKE ? OR t.note LIKE ? OR t.section LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like, like, like]
    query += f" ORDER BY {sort_col} {direction_sql}, t.id {direction_sql}"
    tx = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("activity.html", tx=tx, sort=sort, direction=direction, q=q)


# ---------------------------------------------------------------------------
# Scan in/out
# ---------------------------------------------------------------------------

@app.route("/scan")
@shop_role_required('admin', 'tech', 'apprentice', 'inspector')
def scan_page():
    conn = get_db()
    projects = conn.execute(
        "SELECT * FROM projects WHERE status='active' AND deleted_at IS NULL ORDER BY name"
    ).fetchall()
    # Who's currently clocked in, and recent scan/labor activity, both now
    # live on the Dashboard instead of here.
    conn.close()
    # "Scanning as" defaults to whoever's actually logged in, so a single-user
    # session never has to pick their own name - it's only an actual choice
    # on a shared/kiosk station where several people scan under one login.
    return render_template("scan.html", projects=projects, logged_in_name=session.get("user_name") or "",
                           embedded=request.args.get("embedded") == "1")


@app.route("/labor")
@login_required
def labor_page():
    # The Labor and Scan Parts pages were merged into one intuitive scanner
    # that figures out from the code itself (part / project / laborer)
    # what you're doing - old links/bookmarks land on the same page.
    return redirect(url_for("scan_page"))


GENERAL_SHOP_CODE = "GENERAL-SHOP"


@app.route("/labor/general-code")
@shop_role_required('admin', 'tech')
def general_shop_code_page():
    """One shared, printable code for non-project shop time (cleanup,
    meetings, general overhead) - scan it plus a laborer badge, in either
    order, on the Scan page to clock in/out against no particular project."""
    return render_template("general_shop_code.html", code=GENERAL_SHOP_CODE)


@app.route("/labor/general-code/print-label", methods=["POST"])
@shop_role_required('admin', 'tech')
def general_shop_code_print_label():
    try:
        from label_printer import print_task_label
        print_task_label("Shop / General Time", "Clock in/out - any non-project work", GENERAL_SHOP_CODE)
        flash("Label sent to printer.", "success")
    except Exception as e:
        flash(f"Couldn't print label: {e}", "danger")
    return redirect(url_for("general_shop_code_page"))


@app.route("/api/lookup/<path:barcode>")
@login_required
def api_lookup(barcode):
    conn = get_db()
    part = get_part_by_barcode(conn, barcode)
    conn.close()
    if not part:
        return jsonify({"found": False, "barcode": barcode})
    d = part_to_dict(part, include_cost=can_see_shop_costs())
    d["found"] = True
    return jsonify(d)


@app.route("/api/project_lookup/<code>")
@login_required
def api_project_lookup(code):
    """Look up a project by its scannable code (e.g. 26-001), used by the Scan
    page so a project's own printed code can be scanned to select it."""
    conn = get_db()
    project = conn.execute(
        "SELECT * FROM projects WHERE code = ? COLLATE NOCASE AND deleted_at IS NULL", (code.strip(),)
    ).fetchone()
    conn.close()
    if not project:
        return jsonify({"found": False, "code": code})
    d = dict(project)
    d["found"] = True
    return jsonify(d)


@app.route("/api/operators")
@login_required
def api_operators():
    """Names/devices that have been used before in the 'Scanning as' field,
    most-recently-used first, so the UI can offer a pick list instead of
    requiring free typing every time."""
    conn = get_db()
    rows = conn.execute("""
        SELECT performed_by, MAX(created_at) as last_used
        FROM transactions
        WHERE performed_by IS NOT NULL AND TRIM(performed_by) != ''
        GROUP BY performed_by
        ORDER BY last_used DESC
    """).fetchall()
    conn.close()
    return jsonify([r["performed_by"] for r in rows])


@app.route("/api/sections/<int:project_id>")
@login_required
def api_sections(project_id):
    """Sub-areas (e.g. 'Brakes', 'Engine') known for this specific project -
    scoped per project since a 'Brakes' on one plane's job isn't necessarily
    relevant to another. Includes both sections created ahead of time (via
    Add Sub Area) and sections that have simply been used on a transaction
    or labor session before, most-recently-used first."""
    conn = get_db()
    rows = conn.execute("""
        SELECT section as name, MAX(last_used) as last_used FROM (
            SELECT name as section, created_at as last_used FROM project_sections WHERE project_id = ?
            UNION ALL
            SELECT section, created_at as last_used FROM transactions
                WHERE project_id = ? AND section IS NOT NULL AND TRIM(section) != ''
            UNION ALL
            SELECT section, started_at as last_used FROM labor_sessions
                WHERE project_id = ? AND section IS NOT NULL AND TRIM(section) != ''
        )
        GROUP BY section
        ORDER BY last_used DESC
    """, (project_id, project_id, project_id)).fetchall()
    conn.close()
    return jsonify([r["name"] for r in rows])


def _find_or_create_linked_section(conn, project_id, base_name, linked_squawk_kind=None,
                                    linked_squawk_id=None, linked_todo_id=None):
    """The Sub Area standing in for a plane's squawk/to-do on this job - see
    project_squawk_fix_on_job/project_todo_do_on_job. Reuses one already
    linked to it (double-submit safety) rather than making a second; a
    fresh name gets a "(2)", "(3)" ... suffix if it collides with an
    unrelated Sub Area already on this project (project_sections.name is
    unique per project)."""
    if linked_squawk_kind:
        existing = conn.execute(
            "SELECT id FROM project_sections WHERE project_id = ? AND linked_squawk_kind = ? AND linked_squawk_id = ?",
            (project_id, linked_squawk_kind, linked_squawk_id)).fetchone()
    else:
        existing = conn.execute(
            "SELECT id FROM project_sections WHERE project_id = ? AND linked_todo_id = ?",
            (project_id, linked_todo_id)).fetchone()
    if existing:
        return existing["id"]
    name = (base_name or "Untitled").strip()[:60] or "Untitled"
    candidate, n = name, 2
    while conn.execute("SELECT id FROM project_sections WHERE project_id = ? AND name = ?",
                        (project_id, candidate)).fetchone():
        candidate = f"{name} ({n})"
        n += 1
    cur = conn.execute("""INSERT INTO project_sections
                          (project_id, name, created_at, linked_squawk_kind, linked_squawk_id, linked_todo_id)
                          VALUES (?, ?, ?, ?, ?, ?)""",
                       (project_id, candidate, now_iso(), linked_squawk_kind, linked_squawk_id, linked_todo_id))
    return cur.lastrowid


# A Sub Area linked to a squawk or to-do (see _find_or_create_linked_section)
# mirrors its own request/confirm/send-back steps onto that squawk/to-do, so
# whichever page someone's looking at - the project, the plane, My Tasks -
# shows the same status for the same underlying work (QA finding
# ux-squawk-on-project). `section` needs linked_squawk_kind, linked_squawk_id
# and linked_todo_id selected.
def _propagate_section_check(conn, section, by):
    if section["linked_squawk_kind"]:
        kind, sid = section["linked_squawk_kind"], section["linked_squawk_id"]
        table = "flights" if kind == "flight" else "plane_squawks"
        acked_col = "squawk_acknowledged_at" if kind == "flight" else "acknowledged_at"
        row = conn.execute(f"SELECT {acked_col} as acked FROM {table} WHERE id = ?", (sid,)).fetchone()
        if not row:
            return
        if kind == "flight":
            if not row["acked"]:
                conn.execute("UPDATE flights SET squawk_acknowledged_at = ?, squawk_acknowledged_by = ? WHERE id = ?",
                             (now_iso(), by, sid))
            conn.execute("""UPDATE flights SET squawk_repair_confirm_requested_at = ?,
                            squawk_repair_confirm_requested_by = ?, squawk_sent_back_note = NULL WHERE id = ?""",
                         (now_iso(), by, sid))
        else:
            if not row["acked"]:
                conn.execute("UPDATE plane_squawks SET acknowledged_at = ?, acknowledged_by = ? WHERE id = ?",
                             (now_iso(), by, sid))
            conn.execute("""UPDATE plane_squawks SET repair_confirm_requested_at = ?,
                            repair_confirm_requested_by = ?, sent_back_note = NULL WHERE id = ?""",
                         (now_iso(), by, sid))
    elif section["linked_todo_id"]:
        conn.execute("UPDATE plane_todos SET confirm_requested_at = ?, confirm_requested_by = ? WHERE id = ?",
                     (now_iso(), by, section["linked_todo_id"]))


def _propagate_section_uncheck(conn, section):
    if section["linked_squawk_kind"]:
        kind, sid = section["linked_squawk_kind"], section["linked_squawk_id"]
        if kind == "flight":
            conn.execute("UPDATE flights SET squawk_repair_confirm_requested_at = NULL, "
                         "squawk_repair_confirm_requested_by = NULL WHERE id = ?", (sid,))
        else:
            conn.execute("UPDATE plane_squawks SET repair_confirm_requested_at = NULL, "
                         "repair_confirm_requested_by = NULL WHERE id = ?", (sid,))
    elif section["linked_todo_id"]:
        conn.execute("UPDATE plane_todos SET confirm_requested_at = NULL, confirm_requested_by = NULL WHERE id = ?",
                     (section["linked_todo_id"],))


def _propagate_section_confirm(conn, section, by):
    if section["linked_squawk_kind"]:
        kind, sid = section["linked_squawk_kind"], section["linked_squawk_id"]
        if kind == "flight":
            conn.execute("""UPDATE flights SET squawk_repaired_at = ?, squawk_repaired_by = ?,
                            squawk_repair_confirm_requested_at = NULL, squawk_repair_confirm_requested_by = NULL
                            WHERE id = ?""", (now_iso(), by, sid))
        else:
            conn.execute("""UPDATE plane_squawks SET repaired_at = ?, repaired_by = ?,
                            repair_confirm_requested_at = NULL, repair_confirm_requested_by = NULL
                            WHERE id = ?""", (now_iso(), by, sid))
    elif section["linked_todo_id"]:
        conn.execute("UPDATE plane_todos SET done = 1, completed_at = ?, confirmed_by = ? WHERE id = ?",
                     (now_iso(), by, section["linked_todo_id"]))


def _propagate_section_send_back(conn, section, by):
    if section["linked_squawk_kind"]:
        kind, sid = section["linked_squawk_kind"], section["linked_squawk_id"]
        if kind == "flight":
            conn.execute("UPDATE flights SET squawk_repair_confirm_requested_at = NULL, "
                         "squawk_repair_confirm_requested_by = NULL WHERE id = ?", (sid,))
        else:
            conn.execute("UPDATE plane_squawks SET repair_confirm_requested_at = NULL, "
                         "repair_confirm_requested_by = NULL WHERE id = ?", (sid,))
    elif section["linked_todo_id"]:
        conn.execute("""UPDATE plane_todos SET confirm_requested_at = NULL, confirm_requested_by = NULL,
                        sent_back_at = ?, sent_back_by = ? WHERE id = ?""",
                     (now_iso(), by, section["linked_todo_id"]))


@app.route("/projects/<int:project_id>/squawks/<kind>/<int:squawk_id>/fix_on_job", methods=["POST"])
@shop_role_required('admin', 'tech')
def project_squawk_fix_on_job(project_id, kind, squawk_id):
    """Claims a plane's squawk for this job in one step (QA finding
    ux-squawk-on-project): assigns it to me, moves it straight to Working,
    and gives it a matching Sub Area on this project so parts and labor
    charged here are tied to it. Checking that Sub Area off later moves the
    squawk to Inspection automatically (see _propagate_section_check)."""
    conn = get_db()
    project = conn.execute("SELECT id, asset_id FROM projects WHERE id = ? AND deleted_at IS NULL",
                           (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    if kind == "flight":
        row = conn.execute("SELECT id, notes FROM flights WHERE id = ? AND squawk = 1 AND asset_id = ?",
                           (squawk_id, project["asset_id"])).fetchone()
    elif kind == "quick":
        row = conn.execute("SELECT id, notes FROM plane_squawks WHERE id = ? AND asset_id = ?",
                           (squawk_id, project["asset_id"])).fetchone()
    else:
        conn.close()
        abort(404)
    if not row:
        conn.close()
        flash("Squawk not found.", "danger")
        return redirect(url_for("project_detail", project_id=project_id))
    by, uid = session.get("user_name"), session.get("user_id")
    _find_or_create_linked_section(conn, project_id, row["notes"], linked_squawk_kind=kind, linked_squawk_id=squawk_id)
    if kind == "flight":
        conn.execute("""UPDATE flights SET squawk_acknowledged_at = COALESCE(squawk_acknowledged_at, ?),
                        squawk_acknowledged_by = COALESCE(squawk_acknowledged_by, ?), squawk_assigned_to = ?,
                        squawk_worker_acknowledged_at = ?, squawk_worker_acknowledged_by = ? WHERE id = ?""",
                     (now_iso(), by, uid, now_iso(), by, squawk_id))
    else:
        conn.execute("""UPDATE plane_squawks SET acknowledged_at = COALESCE(acknowledged_at, ?),
                        acknowledged_by = COALESCE(acknowledged_by, ?), assigned_to = ?,
                        worker_acknowledged_at = ?, worker_acknowledged_by = ? WHERE id = ?""",
                     (now_iso(), by, uid, now_iso(), by, squawk_id))
    conn.commit()
    conn.close()
    flash("Added as a Discrepancy on this job, assigned to you.", "success")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/todos/<int:todo_id>/do_on_job", methods=["POST"])
@shop_role_required('admin', 'tech')
def project_todo_do_on_job(project_id, todo_id):
    """Same idea as project_squawk_fix_on_job, for a plane to-do: claims it,
    gives it a matching Sub Area on this job, and moves it to Working."""
    conn = get_db()
    project = conn.execute("SELECT id, asset_id FROM projects WHERE id = ? AND deleted_at IS NULL",
                           (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    todo = conn.execute("SELECT id, description FROM plane_todos WHERE id = ? AND asset_id = ?",
                        (todo_id, project["asset_id"])).fetchone()
    if not todo:
        conn.close()
        flash("To-do not found.", "danger")
        return redirect(url_for("project_detail", project_id=project_id))
    _find_or_create_linked_section(conn, project_id, todo["description"], linked_todo_id=todo_id)
    conn.execute("UPDATE plane_todos SET assigned_to = ? WHERE id = ?", (session.get("user_id"), todo_id))
    conn.commit()
    conn.close()
    flash("Added as a Discrepancy on this job, assigned to you.", "success")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/add_section", methods=["POST"])
@shop_role_required('admin', 'tech')
def project_add_section(project_id):
    """Create a project sub-area (section) ahead of time, without first
    having to scan a part into it - e.g. so labor codes can be printed for
    it before any parts work has happened."""
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Enter a name for the discrepancy.", "danger")
        conn.close()
        return redirect(url_for("project_detail", project_id=project_id))
    conn.execute("INSERT OR IGNORE INTO project_sections (project_id, name, created_at) VALUES (?, ?, ?)",
                 (project_id, name, now_iso()))
    conn.commit()
    conn.close()
    flash(f"Discrepancy '{name}' added.", "success")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/sections/<int:section_id>/complete", methods=["POST"])
@shop_role_required('admin', 'tech')
def project_section_complete(project_id, section_id):
    """Checking a sub area's box no longer completes it outright - it
    requests confirmation instead (confirm_requested_at/by), and stays open
    on the calendar/board until an Inspector (or admin) confirms it via
    project_section_confirm. Unchecking it clears both the request and any
    completion, back to plain open."""
    conn = get_db()
    section = conn.execute("""SELECT id, linked_squawk_kind, linked_squawk_id, linked_todo_id
                              FROM project_sections WHERE id = ? AND project_id = ?""",
                           (section_id, project_id)).fetchone()
    if not section:
        conn.close()
        abort(404)
    completed = request.form.get("completed") == "1"
    by = session.get("user_name")
    if completed:
        conn.execute("UPDATE project_sections SET confirm_requested_at = ?, confirm_requested_by = ? WHERE id = ?",
                     (now_iso(), by, section_id))
        _propagate_section_check(conn, section, by)
    else:
        conn.execute("""UPDATE project_sections SET confirm_requested_at = NULL, confirm_requested_by = NULL,
                         completed_at = NULL, completed_by = NULL WHERE id = ?""", (section_id,))
        _propagate_section_uncheck(conn, section)
    conn.commit()
    conn.close()
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/sections/<int:section_id>/confirm", methods=["POST"])
@shop_role_required('admin', 'inspector')
def project_section_confirm(project_id, section_id):
    """An Inspector (or admin) signs off on a sub area someone else marked
    ready - this is what actually completes it. 'Send back' unchecks it
    instead, so whoever did the work knows it wasn't approved."""
    conn = get_db()
    section = conn.execute("""SELECT id, linked_squawk_kind, linked_squawk_id, linked_todo_id
                              FROM project_sections WHERE id = ? AND project_id = ?""",
                           (section_id, project_id)).fetchone()
    if not section:
        conn.close()
        abort(404)
    by = session.get("user_name")
    if request.form.get("action") == "send_back":
        conn.execute("""UPDATE project_sections SET confirm_requested_at = NULL, confirm_requested_by = NULL,
                         completed_at = NULL, completed_by = NULL, sent_back_at = ?, sent_back_by = ? WHERE id = ?""",
                     (now_iso(), by, section_id))
        _propagate_section_send_back(conn, section, by)
        flash("Sent back - unmarked as ready.", "warning")
    else:
        conn.execute("UPDATE project_sections SET completed_at = ?, completed_by = ? WHERE id = ?",
                     (now_iso(), by, section_id))
        _propagate_section_confirm(conn, section, by)
        flash("Confirmed complete.", "success")
    conn.commit()
    conn.close()
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/sections/<int:section_id>/rename", methods=["POST"])
@shop_role_required('admin', 'tech')
def project_section_rename(project_id, section_id):
    conn = get_db()
    section = conn.execute("SELECT id, name FROM project_sections WHERE id = ? AND project_id = ?",
                           (section_id, project_id)).fetchone()
    if not section:
        conn.close()
        abort(404)
    new_name = request.form.get("name", "").strip()
    if not new_name:
        flash("Enter a name for the discrepancy.", "danger")
        conn.close()
        return redirect(url_for("project_detail", project_id=project_id))
    clash = conn.execute("SELECT id FROM project_sections WHERE project_id = ? AND name = ? AND id != ?",
                         (project_id, new_name, section_id)).fetchone()
    if clash:
        flash(f"A discrepancy named '{new_name}' already exists.", "danger")
        conn.close()
        return redirect(url_for("project_detail", project_id=project_id))
    old_name = section["name"]
    conn.execute("UPDATE project_sections SET name = ? WHERE id = ?", (new_name, section_id))
    # Carries through to everything already tagged with the old name, so
    # history and totals stay grouped together under the new name instead
    # of splitting into two folders.
    conn.execute("UPDATE transactions SET section = ? WHERE project_id = ? AND section = ?",
                 (new_name, project_id, old_name))
    conn.execute("UPDATE labor_sessions SET section = ? WHERE project_id = ? AND section = ?",
                 (new_name, project_id, old_name))
    conn.commit()
    conn.close()
    flash(f"Discrepancy renamed to '{new_name}'.", "success")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/sections/<int:section_id>/notes", methods=["POST"])
@shop_role_required('admin', 'tech')
def project_section_notes(project_id, section_id):
    """Idea "Discrepancy List": notes is shop-only - never shown on the
    invoice (project_invoice_csv) or in My Aircraft (customer._project_bill).
    description is the write-up an owner actually sees there, once it's
    filled in."""
    conn = get_db()
    section = conn.execute("SELECT id FROM project_sections WHERE id = ? AND project_id = ?",
                           (section_id, project_id)).fetchone()
    if not section:
        conn.close()
        abort(404)
    notes = request.form.get("notes", "").strip()
    description = request.form.get("description", "").strip()
    conn.execute("UPDATE project_sections SET notes = ?, description = ? WHERE id = ?",
                 (notes, description, section_id))
    conn.commit()
    conn.close()
    flash("Saved.", "success")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/api/scan", methods=["POST"])
@shop_role_required('admin', 'tech', 'apprentice', 'inspector')
def api_scan():
    # Changes stock and charges jobs, so it needs a Shop role - it used to be
    # login-only, which let a flight student or CFI with no shop access add or
    # remove parts. silent=True: a malformed body is a 400, not a crash.
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Bad request."}), 400
    barcode = (data.get("barcode") or "").strip()
    action = data.get("action")  # 'in' | 'out'
    qty = data.get("qty")
    project_id = data.get("project_id") or None
    note = (data.get("note") or "").strip()
    performed_by = (data.get("performed_by") or "").strip() or None
    section = (data.get("section") or "").strip() or None
    source = data.get("source")
    if source not in ("camera_scan", "usb_scanner"):
        source = "usb_scanner"

    if not barcode:
        return jsonify({"ok": False, "error": "No barcode provided."}), 400
    qty = _parse_qty(qty)
    if qty is None:
        return jsonify({"ok": False, "error": "Quantity must be a number greater than zero."}), 400
    if action not in ("in", "out"):
        return jsonify({"ok": False, "error": "Invalid action."}), 400
    if action == "out" and not project_id:
        return jsonify({"ok": False, "error": "A project is required for stock out."}), 400
    if not performed_by:
        return jsonify({"ok": False, "error": "Select who's scanning (\"Scanning as\") first."}), 400

    conn = get_db()
    part = get_part_by_barcode(conn, barcode)
    if not part:
        conn.close()
        return jsonify({"ok": False, "error": "unknown_barcode", "barcode": barcode}), 404

    if action == "out" and project_id:
        proj = conn.execute("SELECT id, status FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)).fetchone()
        if not proj:
            conn.close()
            return jsonify({"ok": False, "error": "unknown_project"}), 404
        if proj["status"] in ("completed", "archived"):
            conn.close()
            return jsonify({"ok": False, "error": f"That job is {proj['status']} - reopen it first to add parts."}), 400

    if action == "out" and part["qty_on_hand"] < qty:
        conn.close()
        return jsonify({"ok": False, "error": f"Only {part['qty_on_hand']:g} {part['unit']} in stock."}), 400
    # Expired shelf-life part: the tech has to confirm ("Use anyway?"), and
    # it's noted on the project's transaction.
    exp = part_expiry(part) if action == "out" else None
    if exp and exp["level"] == "expired":
        if data.get("confirm_expired") is not True:
            conn.close()
            return jsonify({"ok": False, "error": "expired", "expired_on": usdate(exp["date"]),
                            "name": part["name"]}), 409
        note = ((note + " - ") if note else "") + f"Used past expiration (expired {usdate(exp['date'])})"

    delta = qty if action == "in" else -qty
    conn.execute("UPDATE parts SET qty_on_hand = qty_on_hand + ?, updated_at = ? WHERE id = ?",
                 (delta, now_iso(), part["id"]))
    conn.execute("""INSERT INTO transactions (part_id, project_id, type, qty, note, performed_by, section, source, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (part["id"], project_id if action == "out" else None, action, qty, note, performed_by,
                  section if action == "out" else None, source, now_iso()))
    conn.commit()
    updated = conn.execute("SELECT * FROM parts WHERE id = ?", (part["id"],)).fetchone()
    conn.close()

    d = part_to_dict(updated, include_cost=can_see_shop_costs())
    d["ok"] = True
    return jsonify(d)


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Shelf-life expiration (QA feat-shelf-life-expiry): one optional date per
# part. A part with no date looks exactly as before - no tags, no tile, no
# warnings. Dated parts get an amber "Expires soon" tag within 60 days and a
# red "Expired" tag after; scanning out an expired part asks "Use anyway?".
# ---------------------------------------------------------------------------
EXPIRY_SOON_DAYS = 60


def part_expiry(part):
    """{'date', 'days_left', 'level'} for a part with an expiration date
    (level 'expired' / 'soon' / 'ok'), else None."""
    try:
        raw = part["expiration_date"]
    except (KeyError, IndexError):
        return None
    if not raw:
        return None
    try:
        d = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    days = (d - date.today()).days
    level = "expired" if days < 0 else ("soon" if days <= EXPIRY_SOON_DAYS else "ok")
    return {"date": d.isoformat(), "days_left": days, "level": level}


app.jinja_env.globals["part_expiry"] = part_expiry


def _parse_expiration(form):
    """(date_or_None, error) from a form's expiration_date field. Blank is
    fine (no expiration tracked)."""
    raw = (form.get("expiration_date") or "").strip()
    if not raw:
        return None, None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat(), None
    except ValueError:
        return None, "Expiration date must be a real date."


def _expiry_stats(conn):
    rows = conn.execute("SELECT expiration_date FROM parts WHERE retired_at IS NULL AND expiration_date IS NOT NULL "
                        "AND expiration_date != '' ORDER BY expiration_date").fetchall()
    if not rows:
        return None
    today = date.today().isoformat()
    upcoming = [r["expiration_date"] for r in rows if r["expiration_date"] >= today]
    return {"count": len(rows), "expired": sum(1 for r in rows if r["expiration_date"] < today),
            "soon": sum(1 for r in rows if today <= r["expiration_date"] <= (date.today() + timedelta(days=EXPIRY_SOON_DAYS)).isoformat()),
            "next": upcoming[0] if upcoming else None}


@app.route("/parts/expiring")
@shop_role_required('admin', 'tech')
def parts_expiring():
    """Parts by Expiration Date: every dated part, soonest first (expired
    on top in red). Parts without a date never show here."""
    conn = get_db()
    parts = conn.execute("""SELECT * FROM parts WHERE retired_at IS NULL AND expiration_date IS NOT NULL
                            AND expiration_date != '' ORDER BY expiration_date, name""").fetchall()
    conn.close()
    return render_template("parts_expiring.html", parts=parts)


@app.route("/parts/<int:part_id>/expiration", methods=["GET", "POST"])
@shop_role_required('admin')
def part_expiration_update(part_id):
    """Asked right after receiving an order for a dated part: the new
    stock's expiration date (Skip keeps the old one)."""
    conn = get_db()
    part = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    if not part:
        conn.close()
        abort(404)
    if request.method == "POST":
        exp, err = _parse_expiration(request.form)
        if err or not exp:
            conn.close()
            flash(err or "Pick the new expiration date, or press Skip.", "danger")
            return redirect(url_for("part_expiration_update", part_id=part_id))
        conn.execute("UPDATE parts SET expiration_date = ?, updated_at = ? WHERE id = ?", (exp, now_iso(), part_id))
        conn.commit()
        conn.close()
        flash(f"{part['name']} now expires {usdate(exp)}.", "success")
        return redirect(url_for("orders_list"))
    conn.close()
    return render_template("part_expiration_update.html", part=part)


@app.route("/parts")
@shop_role_required('admin', 'tech')
def parts_list():
    q = request.args.get("q", "").strip()
    show_retired = request.args.get("retired") == "1"
    show_low_stock = request.args.get("low_stock") == "1"
    conn = get_db()
    part_count = conn.execute("SELECT COUNT(*) c FROM parts WHERE retired_at IS NULL").fetchone()["c"]
    total_value = conn.execute("SELECT COALESCE(SUM(qty_on_hand * unit_cost),0) v FROM parts WHERE retired_at IS NULL").fetchone()["v"]
    retired_count = conn.execute("SELECT COUNT(*) c FROM parts WHERE retired_at IS NOT NULL").fetchone()["c"]
    expiry_stats = _expiry_stats(conn)
    if show_retired:
        parts = conn.execute("SELECT * FROM parts WHERE retired_at IS NOT NULL ORDER BY name").fetchall()
        part_covers = _part_covers(conn, parts)
        conn.close()
        return render_template("parts.html", parts=parts, q=q, by_category=None, show_retired=True,
                               show_low_stock=False,
                               part_count=part_count, total_value=total_value, part_covers=part_covers,
                               retired_count=retired_count, expiry_stats=expiry_stats)
    if show_low_stock:
        # Dashboard's Low Stock Items box links here (QA finding
        # ux-shop-stat-boxes-by-role) - same rows get_low_stock() already
        # flags on the shop dashboard, just as a real filtered list.
        parts = get_low_stock(conn)
        part_covers = _part_covers(conn, parts)
        conn.close()
        return render_template("parts.html", parts=parts, q=q, by_category=None, show_retired=False,
                               show_low_stock=True,
                               part_count=part_count, total_value=total_value, part_covers=part_covers,
                               retired_count=retired_count, expiry_stats=expiry_stats)
    if q:
        like = f"%{q}%"
        parts = conn.execute("""SELECT * FROM parts
                                 WHERE retired_at IS NULL AND (name LIKE ? OR barcode LIKE ? OR category LIKE ? OR location LIKE ?)
                                 ORDER BY name""", (like, like, like, like)).fetchall()
        part_covers = _part_covers(conn, parts)
        conn.close()
        return render_template("parts.html", parts=parts, q=q, by_category=None, show_retired=False,
                               show_low_stock=False,
                               part_count=part_count, total_value=total_value, part_covers=part_covers,
                               retired_count=retired_count, expiry_stats=expiry_stats)

    parts = conn.execute("SELECT * FROM parts WHERE retired_at IS NULL ORDER BY category, name").fetchall()
    part_covers = _part_covers(conn, parts)
    conn.close()
    # Group into categories for the click-to-expand browse view.
    by_category = {}
    for p in parts:
        cat = p["category"] or "Uncategorized"
        by_category.setdefault(cat, []).append(p)
    return render_template("parts.html", parts=parts, q=q, by_category=by_category, show_retired=False,
                           show_low_stock=False,
                           part_count=part_count, total_value=total_value, part_covers=part_covers,
                           retired_count=retired_count, expiry_stats=expiry_stats)


def _part_covers(conn, parts):
    """The cover photo (or, absent one, the most recently added photo) for
    each part, for the small thumbnail on the Parts list."""
    covers = {}
    for p in parts:
        photo = conn.execute(
            "SELECT filename FROM photos WHERE part_id = ? ORDER BY is_cover DESC, created_at DESC LIMIT 1",
            (p["id"],)).fetchone()
        if photo:
            covers[p["id"]] = photo["filename"]
    return covers


@app.route("/parts/export.csv")
@shop_role_required('admin')
def parts_export_csv():
    conn = get_db()
    parts = conn.execute("SELECT * FROM parts ORDER BY category, name").fetchall()
    conn.close()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "SKU", "Category", "Description", "Unit Cost", "Sell Price", "Qty On Hand",
                      "Unit", "Reorder Point", "Location", "Supplier"])
    for p in parts:
        writer.writerow([p["name"], p["barcode"], p["category"], p["description"], p["unit_cost"], p["sell_price"],
                          p["qty_on_hand"], p["unit"], p["reorder_point"], p["location"], p["supplier"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": "attachment; filename=parts_export.csv"})


@app.route("/parts/new", methods=["GET", "POST"])
@shop_role_required('admin')
def part_new():
    conn = get_db()
    if request.method == "POST":
        barcode = (request.form.get("barcode") or "").strip()
        generate = request.form.get("generate_barcode") == "on"
        if generate or not barcode:
            barcode = gen_internal_barcode(conn)
        name = request.form.get("name", "").strip()
        if not name:
            flash("Part name is required.", "danger")
            conn.close()
            return render_template("part_form.html", part=None, categories=CATEGORIES_DEFAULT,
                                   form=request.form, notify_low_stock_checked=bool(request.form.get("notify_low_stock")))
        # The barcode column is unique regardless of retired status, so this
        # has to check for one too (get_part_by_barcode excludes retired
        # parts, which would otherwise let this slip through to a raw
        # database error on insert).
        existing = conn.execute("SELECT * FROM parts WHERE barcode = ?", (barcode.strip(),)).fetchone()
        if existing:
            flash(f"A part with barcode '{barcode}' already exists ({existing['name']}{' - retired' if existing['retired_at'] else ''}).", "danger")
            conn.close()
            return render_template("part_form.html", part=None, categories=CATEGORIES_DEFAULT,
                                   form=request.form, notify_low_stock_checked=bool(request.form.get("notify_low_stock")))
        qty = _parse_qty(request.form.get("qty_on_hand") or 0, allow_zero=True)
        reorder = _parse_qty(request.form.get("reorder_point") or 0, allow_zero=True)
        cost = _parse_qty(request.form.get("unit_cost") or 0, allow_zero=True)
        sell_price = _parse_qty(request.form.get("sell_price") or 0, allow_zero=True)
        if None in (qty, reorder, cost, sell_price):
            flash("Quantity, reorder point, cost, and sell price must be numbers (0 or more).", "danger")
            conn.close()
            return render_template("part_form.html", part=None, categories=CATEGORIES_DEFAULT,
                                   form=request.form, notify_low_stock_checked=bool(request.form.get("notify_low_stock")))
        expiration_date, exp_err = _parse_expiration(request.form)
        if exp_err:
            flash(exp_err, "danger")
            conn.close()
            return render_template("part_form.html", part=None, categories=CATEGORIES_DEFAULT,
                                   form=request.form, notify_low_stock_checked=bool(request.form.get("notify_low_stock")))

        notify_low_stock = 1 if request.form.get("notify_low_stock") else 0
        cur = conn.execute("""INSERT INTO parts (barcode, name, short_name, part_number, description, category, location, unit,
                               qty_on_hand, reorder_point, unit_cost, sell_price, supplier, notify_low_stock, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (barcode, name, request.form.get("short_name", "").strip() or None,
                             request.form.get("part_number", "").strip() or None,
                             request.form.get("description", "").strip(),
                             request.form.get("category", "").strip(), request.form.get("location", "").strip(),
                             request.form.get("unit", "ea").strip() or "ea", qty, reorder, cost, sell_price,
                             request.form.get("supplier", "").strip(), notify_low_stock, now_iso(), now_iso()))
        new_id = cur.lastrowid
        if expiration_date:
            conn.execute("UPDATE parts SET expiration_date = ? WHERE id = ?", (expiration_date, new_id))
        if qty > 0:
            conn.execute("""INSERT INTO transactions (part_id, project_id, type, qty, note, source, created_at)
                             VALUES (?, NULL, 'in', ?, 'Initial stock', 'assigned', ?)""", (new_id, qty, now_iso()))
        conn.commit()
        conn.close()
        flash(f"Part '{name}' added with barcode {barcode}.", "success")
        return redirect(url_for("part_detail", part_id=new_id))

    conn.close()
    return render_template("part_form.html", part=None, categories=CATEGORIES_DEFAULT, form={},
                           notify_low_stock_checked=True)


@app.route("/parts/<int:part_id>")
@shop_role_required('admin', 'tech')
def part_detail(part_id):
    conn = get_db()
    part = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    if not part:
        conn.close()
        abort(404)
    tx = conn.execute("""SELECT t.*, pr.name as project_name FROM transactions t
                          LEFT JOIN projects pr ON pr.id = t.project_id
                          WHERE t.part_id = ? ORDER BY t.created_at DESC, t.id DESC LIMIT 50""",
                       (part_id,)).fetchall()
    photos = conn.execute("SELECT * FROM photos WHERE part_id = ? ORDER BY is_cover DESC, created_at DESC",
                           (part_id,)).fetchall()
    conn.close()
    return render_template("part_detail.html", part=part, tx=tx, photos=photos)


@app.route("/parts/<int:part_id>/print-label", methods=["POST"])
@shop_role_required('admin', 'tech')
def part_print_label(part_id):
    conn = get_db()
    part = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    conn.close()
    if not part:
        abort(404)
    try:
        from label_printer import print_part_label
        print_part_label(part)
        flash(f"Label sent to printer for {part['name']}.", "success")
    except Exception as e:
        flash(f"Couldn't print label: {e}", "danger")
    return redirect(url_for("part_detail", part_id=part_id))


@app.route("/parts/<int:part_id>/edit", methods=["GET", "POST"])
@shop_role_required('admin')
def part_edit(part_id):
    conn = get_db()
    part = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    if not part:
        conn.close()
        abort(404)
    if request.method == "POST":
        reorder = _parse_qty(request.form.get("reorder_point") or 0, allow_zero=True)
        cost = _parse_qty(request.form.get("unit_cost") or 0, allow_zero=True)
        sell_price = _parse_qty(request.form.get("sell_price") or 0, allow_zero=True)
        if not request.form.get("name", "").strip():
            flash("Part name is required.", "danger")
            conn.close()
            return render_template("part_form.html", part=part, categories=CATEGORIES_DEFAULT, form=request.form,
                                   notify_low_stock_checked=bool(request.form.get("notify_low_stock")))
        if None in (reorder, cost, sell_price):
            flash("Reorder point, cost, and sell price must be numbers (0 or more).", "danger")
            conn.close()
            return render_template("part_form.html", part=part, categories=CATEGORIES_DEFAULT, form=request.form,
                                   notify_low_stock_checked=bool(request.form.get("notify_low_stock")))
        expiration_date, exp_err = _parse_expiration(request.form)
        if exp_err:
            flash(exp_err, "danger")
            conn.close()
            return render_template("part_form.html", part=part, categories=CATEGORIES_DEFAULT, form=request.form,
                                   notify_low_stock_checked=bool(request.form.get("notify_low_stock")))
        conn.execute("UPDATE parts SET expiration_date = ? WHERE id = ?", (expiration_date, part_id))
        notify_low_stock = 1 if request.form.get("notify_low_stock") else 0
        conn.execute("""UPDATE parts SET name=?, short_name=?, part_number=?, description=?, category=?, location=?, unit=?,
                         reorder_point=?, unit_cost=?, sell_price=?, supplier=?, notify_low_stock=?, updated_at=? WHERE id=?""",
                     (request.form.get("name", "").strip(), request.form.get("short_name", "").strip() or None,
                      request.form.get("part_number", "").strip() or None,
                      request.form.get("description", "").strip(),
                      request.form.get("category", "").strip(), request.form.get("location", "").strip(),
                      request.form.get("unit", "ea").strip() or "ea", reorder, cost, sell_price,
                      request.form.get("supplier", "").strip(), notify_low_stock, now_iso(), part_id))
        conn.commit()
        conn.close()
        flash("Part updated.", "success")
        return redirect(url_for("part_detail", part_id=part_id))
    conn.close()
    return render_template("part_form.html", part=part, categories=CATEGORIES_DEFAULT, form=dict(part),
                           notify_low_stock_checked=bool(part["notify_low_stock"]))


@app.route("/parts/<int:part_id>/adjust", methods=["POST"])
@shop_role_required('admin')
def part_adjust(part_id):
    conn = get_db()
    part = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    if not part:
        conn.close()
        abort(404)
    performed_by = request.form.get("performed_by", "").strip() or None
    if not performed_by:
        flash("Select who's making this adjustment (\"Scanning as\") first.", "danger")
        conn.close()
        return redirect(url_for("part_detail", part_id=part_id))
    new_qty = _parse_qty(request.form.get("new_qty"), allow_zero=True)
    if new_qty is None:
        flash("New quantity must be a number, 0 or more.", "danger")
        conn.close()
        return redirect(url_for("part_detail", part_id=part_id))
    delta = new_qty - part["qty_on_hand"]
    conn.execute("UPDATE parts SET qty_on_hand = ?, updated_at = ? WHERE id = ?",
                 (new_qty, now_iso(), part_id))
    conn.execute("""INSERT INTO transactions (part_id, project_id, type, qty, note, performed_by, source, created_at)
                     VALUES (?, NULL, 'adjust', ?, ?, ?, 'assigned', ?)""",
                 (part_id, delta, request.form.get("note", "Manual count adjustment"), performed_by, now_iso()))
    conn.commit()
    conn.close()
    flash("Stock count adjusted.", "success")
    return redirect(url_for("part_detail", part_id=part_id))


@app.route("/parts/<int:part_id>/delete", methods=["POST"])
@shop_role_required('admin')
def part_delete(part_id):
    conn = get_db()
    # Deleting used to also wipe every transaction row for this part, so a
    # completed/billed job would silently lose its parts and their cost.
    # Delete now stays available only for a part that was never actually
    # used on a job (entered by mistake, wrong barcode, etc.) - one that has
    # real history gets Retired instead (see part_retire), which keeps every
    # past transaction intact.
    job_count = conn.execute(
        "SELECT COUNT(DISTINCT project_id) c FROM transactions WHERE part_id = ? AND project_id IS NOT NULL",
        (part_id,)).fetchone()["c"]
    if job_count:
        conn.close()
        flash(f"This part is on {job_count} job{'s' if job_count != 1 else ''}, so it can't be deleted. "
              f"Use Retire instead to hide it without losing that history.", "danger")
        return redirect(url_for("part_detail", part_id=part_id))
    # Orders and the To-Order list point at the part too, and the database
    # refuses to delete a part something still points at (that used to show
    # an error page). An open order or To-Order entry, or one that was
    # already received, blocks Delete with a plain reason - Retire instead,
    # so the order can still be received and its history still reads right.
    open_orders = conn.execute("SELECT COUNT(*) c FROM orders WHERE part_id = ? AND status = 'pending'",
                               (part_id,)).fetchone()["c"]
    received_orders = conn.execute("SELECT COUNT(*) c FROM orders WHERE part_id = ? AND status = 'received'",
                                   (part_id,)).fetchone()["c"]
    to_order = conn.execute("SELECT COUNT(*) c FROM order_wishlist WHERE part_id = ? AND status = 'open'",
                            (part_id,)).fetchone()["c"]
    reason = ("it's on an open order" if open_orders else
              "it's on the To-Order list" if to_order else
              "it has received orders in its history" if received_orders else None)
    if reason:
        conn.close()
        flash(f"This part can't be deleted because {reason}. Use Retire instead to hide it without "
              f"losing that.", "danger")
        return redirect(url_for("part_detail", part_id=part_id))
    # Nothing still needs the part. Cancelled orders and closed To-Order
    # entries keep their written description; photos are KEPT (Frank's call)
    # - just no longer attached to a part - rather than removed.
    conn.execute("UPDATE orders SET part_id = NULL WHERE part_id = ?", (part_id,))
    conn.execute("UPDATE order_wishlist SET part_id = NULL WHERE part_id = ?", (part_id,))
    conn.execute("UPDATE photos SET part_id = NULL WHERE part_id = ?", (part_id,))
    conn.execute("DELETE FROM transactions WHERE part_id = ?", (part_id,))
    conn.execute("DELETE FROM parts WHERE id = ?", (part_id,))
    conn.commit()
    conn.close()
    flash("Part deleted.", "success")
    return redirect(url_for("parts_list"))


@app.route("/parts/<int:part_id>/retire", methods=["POST"])
@shop_role_required('admin')
def part_retire(part_id):
    """Hides a part from the parts list and scan lookups without touching
    its history - for a part that's genuinely used up/discontinued but has
    real transaction history, where Delete is blocked (see part_delete)."""
    conn = get_db()
    part = conn.execute("SELECT id FROM parts WHERE id = ?", (part_id,)).fetchone()
    if not part:
        conn.close()
        abort(404)
    conn.execute("UPDATE parts SET retired_at = ?, updated_at = ? WHERE id = ?", (now_iso(), now_iso(), part_id))
    conn.commit()
    conn.close()
    flash("Part retired - hidden from the parts list and scanning, but its history is kept.", "success")
    return redirect(url_for("parts_list"))


@app.route("/parts/<int:part_id>/unretire", methods=["POST"])
@shop_role_required('admin')
def part_unretire(part_id):
    conn = get_db()
    part = conn.execute("SELECT id FROM parts WHERE id = ?", (part_id,)).fetchone()
    if not part:
        conn.close()
        abort(404)
    conn.execute("UPDATE parts SET retired_at = NULL, updated_at = ? WHERE id = ?", (now_iso(), part_id))
    conn.commit()
    conn.close()
    flash("Part un-retired - back on the parts list and scannable again.", "success")
    return redirect(url_for("parts_list", retired="1"))


# ---------------------------------------------------------------------------
# Labels (printable barcode sheet)
# ---------------------------------------------------------------------------

@app.route("/labels")
@shop_role_required('admin', 'tech')
def labels():
    conn = get_db()
    ids = request.args.get("ids", "")
    if ids:
        id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
        q_marks = ",".join("?" * len(id_list))
        parts = conn.execute(f"SELECT * FROM parts WHERE id IN ({q_marks}) ORDER BY name", id_list).fetchall() \
            if id_list else []
    else:
        parts = conn.execute("SELECT * FROM parts ORDER BY name").fetchall()
    conn.close()
    return render_template("labels.html", parts=parts)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@app.route("/projects/parts-used")
@shop_role_required('admin', 'tech', 'apprentice', 'inspector')
def projects_parts_used():
    """Master list of every part used on every project (net of returns),
    one row per project + part + Sub Area, searchable as you type."""
    status = request.args.get("status", "all")
    if status not in ("all", "active", "on_hold", "completed", "archived"):
        status = "all"
    conn = get_db()
    rows = conn.execute("""
        SELECT pr.id as project_id, pr.code as project_code, pr.name as project_name, pr.status as project_status,
               a.tag as plane_tag, p.id as part_id, p.name as part_name, p.barcode, p.unit, p.unit_cost,
               COALESCE(NULLIF(TRIM(t.section), ''), 'General') as section,
               SUM(CASE WHEN t.type = 'out' THEN t.qty ELSE -t.qty END) as qty_used,
               MIN(t.created_at) as first_used, MAX(t.created_at) as last_used
        FROM transactions t
        JOIN parts p ON p.id = t.part_id
        JOIN projects pr ON pr.id = t.project_id
        LEFT JOIN assets a ON a.id = pr.asset_id
        WHERE t.project_id IS NOT NULL AND pr.deleted_at IS NULL AND t.type IN ('out', 'in')
          AND (? = 'all' OR pr.status = ?)
        GROUP BY pr.id, p.id, COALESCE(NULLIF(TRIM(t.section), ''), 'General')
        HAVING qty_used > 0
        ORDER BY MAX(t.created_at) DESC, pr.code, p.name
    """, (status, status)).fetchall()
    conn.close()
    items = [dict(r, cost=(r["qty_used"] or 0) * (r["unit_cost"] or 0)) for r in rows]
    return render_template("projects_parts_used.html", items=items, status=status,
                           q=request.args.get("q", ""))


@app.route("/projects")
@shop_role_required('admin', 'tech', 'apprentice', 'inspector')
def projects_list():
    conn = get_db()
    status_filter = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    # "Deleted" is its own pill (like Archived), showing what's in Recently
    # Deleted without leaving the Projects page - the restore/purge actions
    # themselves still live on the trash page (project_restore/project_purge)
    # so there's exactly one place that does the actual DB work.
    if status_filter == "deleted":
        query = """SELECT projects.*, a.id as asset_display_id, a.tag as asset_display_tag, a.name as asset_display_name
                   FROM projects LEFT JOIN assets a ON a.id = projects.asset_id WHERE projects.deleted_at IS NOT NULL"""
        params = []
        if q:
            query += " AND (projects.name LIKE ? OR projects.code LIKE ? OR projects.description LIKE ? OR a.tag LIKE ? OR a.name LIKE ?)"
            like = f"%{q}%"
            params += [like, like, like, like, like]
        query += " ORDER BY projects.deleted_at DESC"
        projects = conn.execute(query, params).fetchall()
        proj_costs = {p["id"]: 0 for p in projects}
        proj_cover = {}
        conn.close()
        return render_template("projects.html", projects=projects, by_year=None, proj_costs=proj_costs,
                               proj_cover=proj_cover, q=q, status_filter=status_filter)
    query = """SELECT projects.*, a.id as asset_display_id, a.tag as asset_display_tag, a.name as asset_display_name
               FROM projects LEFT JOIN assets a ON a.id = projects.asset_id WHERE projects.deleted_at IS NULL"""
    params = []
    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)
    else:
        # Archived projects are hidden from the default/status-tab views;
        # they're only shown via the explicit "Archived" pill.
        query += " AND status != 'archived'"
    if q:
        query += " AND (projects.name LIKE ? OR projects.code LIKE ? OR projects.description LIKE ? OR a.tag LIKE ? OR a.name LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like, like, like]
    query += " ORDER BY projects.created_at DESC"
    projects = conn.execute(query, params).fetchall()
    proj_costs = {}
    proj_cover = {}
    for p in projects:
        row = conn.execute("""SELECT COALESCE(SUM(t.qty * pt.unit_cost),0) as cost
                               FROM transactions t JOIN parts pt ON pt.id = t.part_id
                               WHERE t.project_id = ? AND t.type='out'""", (p["id"],)).fetchone()
        proj_costs[p["id"]] = row["cost"]
        photo = conn.execute(
            "SELECT filename FROM photos WHERE project_id = ? ORDER BY is_cover DESC, created_at DESC LIMIT 1",
            (p["id"],)).fetchone()
        if photo:
            proj_cover[p["id"]] = photo["filename"]

    # Browse view: group into year folders (like the Parts category browse)
    # whenever the user isn't actively searching.
    by_year = None
    if not q:
        by_year = {}
        for p in projects:
            yy = (p["code"] or "").split("-")[0]
            year_label = ("20" + yy) if yy.isdigit() and len(yy) == 2 else "Other"
            by_year.setdefault(year_label, []).append(p)
        by_year = dict(sorted(by_year.items(), key=lambda kv: kv[0], reverse=True))

    conn.close()
    return render_template("projects.html", projects=projects, by_year=by_year, proj_costs=proj_costs,
                           proj_cover=proj_cover, q=q, status_filter=status_filter)


# Quick Type buttons on New/Edit Project (project_form.html) - the same
# fixed set the description-filler buttons offer. A Task Template (Manage >
# Task Templates, admins only - see task_templates()) maps one of these to
# a preset list of Sub Areas that get auto-created when it's picked on a
# brand new project. Admins can add their own beyond these 4 built-ins from
# Manage > Task Templates (task_template_types table) - see _all_quick_types().
QUICK_TYPES = ["Annual Inspection", "100hr Inspection", "Oil Change", "Maintenance"]


def _all_quick_types(conn):
    """The built-in Quick Types plus any admin-added custom ones, in the
    order New/Edit Project's buttons and Task Templates should show them."""
    custom = [r["name"] for r in conn.execute(
        "SELECT name FROM task_template_types ORDER BY sort_order, name").fetchall()]
    return QUICK_TYPES + custom


def _quick_type_form_context(conn):
    """quick_types + each type's optional areas (id/name), for the New/Edit
    Project Quick Type buttons and their optional-service checkboxes."""
    quick_types = _all_quick_types(conn)
    optional_areas_by_type = {t: [{"id": r["id"], "name": r["name"]} for r in conn.execute(
        "SELECT id, name FROM task_template_areas WHERE quick_type = ? AND is_optional = 1 ORDER BY sort_order, name",
        (t,)).fetchall()] for t in quick_types}
    return quick_types, optional_areas_by_type


@app.route("/projects/new", methods=["GET", "POST"])
@shop_role_required('admin', 'tech')
def project_new():
    conn = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Project name is required.", "danger")
            # A simulator (assets.is_simulator) isn't a real aircraft - no
            # maintenance projects - so it stays out of this picker too.
            assets = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_simulator = 0 AND is_owner_placeholder = 0 ORDER BY tag").fetchall()
            quick_types, optional_areas_by_type = _quick_type_form_context(conn)
            conn.close()
            return render_template("project_form.html", project=None, assets=assets, quick_types=quick_types,
                                   optional_areas_by_type=optional_areas_by_type)
        code = gen_project_code(conn)
        asset_id = request.form.get("asset_id") or None
        # QA fix qa-project-missing-plane: the plane picked on the form can
        # be gone by the time Save is pressed (permanently deleted in
        # another tab, or an old form resubmitted) - without this check
        # the INSERT below hits a FOREIGN KEY constraint (an error page)
        # instead of a plain "pick again" message, and the typed project
        # details are lost.
        if asset_id and not conn.execute(
                "SELECT 1 FROM assets WHERE id = ? AND deleted_at IS NULL", (asset_id,)).fetchone():
            flash("That aircraft is no longer on file. Pick another aircraft (or none) and save again.", "danger")
            assets = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_simulator = 0 AND is_owner_placeholder = 0 ORDER BY tag").fetchall()
            quick_types, optional_areas_by_type = _quick_type_form_context(conn)
            conn.close()
            return render_template("project_form.html", project=None, assets=assets, quick_types=quick_types,
                                   optional_areas_by_type=optional_areas_by_type, preselect_name=name)
        scheduled_date = request.form.get("scheduled_date", "").strip() or None
        scheduled_end_date = request.form.get("scheduled_end_date", "").strip() or None
        # A start date with no "Through" defaults to a week-long block - most
        # maintenance jobs run about that long, so it shows a realistic
        # window on the calendar instead of looking like a single day.
        if scheduled_date and not scheduled_end_date:
            try:
                scheduled_end_date = (date.fromisoformat(scheduled_date) + timedelta(days=7)).isoformat()
            except ValueError:
                pass
        if scheduled_end_date and scheduled_date and scheduled_end_date < scheduled_date:
            scheduled_end_date = scheduled_date
        scheduled_color = request.form.get("scheduled_color", "").strip() or None
        cur = conn.execute(
            """INSERT INTO projects (code, name, description, status, asset_id, scheduled_date,
                                      scheduled_end_date, scheduled_color, prework_checklist, standard_items, created_at)
               VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)""",
            (code, name, request.form.get("description", "").strip(), asset_id, scheduled_date,
             scheduled_end_date, scheduled_color, request.form.get("prework_checklist", "").strip() or None,
             request.form.get("standard_items", "").strip() or None, now_iso()))
        new_id = cur.lastrowid
        conn.execute("UPDATE projects SET intake_status = 'pending' WHERE id = ?", (new_id,))
        # Whichever Quick Type buttons were picked (see project_form.html's
        # quick_types hidden field) auto-add that type's REQUIRED preset Sub
        # Areas - Manage > Task Templates is where an admin sets those up.
        # An OPTIONAL area (is_optional) only comes along if its own
        # checkbox was ticked (optional_area_ids), instead of every
        # optional service on the template piling onto every project.
        quick_types = [t for t in (request.form.get("quick_types") or "").split(",") if t]
        added_areas = []
        for t in quick_types:
            for r in conn.execute("SELECT name FROM task_template_areas WHERE quick_type = ? AND is_optional = 0 ORDER BY sort_order, name",
                                  (t,)).fetchall():
                conn.execute("INSERT OR IGNORE INTO project_sections (project_id, name, created_at) VALUES (?, ?, ?)",
                             (new_id, r["name"], now_iso()))
                added_areas.append(r["name"])
        optional_ids = [int(x) for x in (request.form.get("optional_area_ids") or "").split(",") if x.isdigit()]
        if optional_ids and quick_types:
            placeholders = ",".join("?" * len(optional_ids))
            type_placeholders = ",".join("?" * len(quick_types))
            for r in conn.execute(
                f"""SELECT name FROM task_template_areas WHERE id IN ({placeholders})
                    AND is_optional = 1 AND quick_type IN ({type_placeholders})""",
                optional_ids + quick_types).fetchall():
                conn.execute("INSERT OR IGNORE INTO project_sections (project_id, name, created_at) VALUES (?, ?, ?)",
                             (new_id, r["name"], now_iso()))
                added_areas.append(r["name"])
        # An Annual on a plane with ADs: its open and recurring ADs go on the
        # Job Sheet list so the IA checks them off during the annual.
        ads_added = 0
        if asset_id and ("annual" in name.lower() or any(t.lower() == "annual" for t in quick_types)):
            ads_added = add_ads_to_annual_project(conn, new_id, asset_id)
        conn.commit()
        conn.close()
        msg = f"Project '{name}' created as {code}. Fill in the intake check before starting work."
        if added_areas:
            msg += f" Discrepanc{'ies' if len(added_areas) != 1 else 'y'} added: {', '.join(added_areas)}."
        if ads_added:
            msg += f" {ads_added} AD{'s' if ads_added != 1 else ''} added to the Job Sheet."
        flash(msg, "success")
        return redirect(url_for("project_intake", project_id=new_id))
    assets = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_simulator = 0 AND is_owner_placeholder = 0 ORDER BY tag").fetchall()
    preselect_asset_id = request.args.get("asset_id")
    preselect_asset_tag = None
    if preselect_asset_id:
        a = conn.execute("SELECT tag FROM assets WHERE id = ?", (preselect_asset_id,)).fetchone()
        preselect_asset_tag = a["tag"] if a else None
    quick_types, optional_areas_by_type = _quick_type_form_context(conn)
    conn.close()
    # Clicking a plane's to-do item (asset_detail.html) links here with
    # ?name=<the to-do text> so the project starts pre-filled from it,
    # instead of a blank form defaulting to just the tail number - still
    # freely editable before saving. Marking the to-do itself complete is
    # still a separate, manual step (plane_todo_toggle) - creating the
    # project doesn't do that on its own. It also sends ?quick_type=Maintenance
    # so that Quick Type button starts pressed (a to-do is maintenance work
    # by definition) instead of the form opening with no type picked.
    preselect_name = request.args.get("name", "").strip()
    preselect_quick_type = request.args.get("quick_type", "").strip()
    if preselect_quick_type not in quick_types:
        preselect_quick_type = ""
    return render_template("project_form.html", project=None, assets=assets, preselect_asset_id=preselect_asset_id,
                           preselect_asset_tag=preselect_asset_tag, preselect_name=preselect_name,
                           preselect_quick_type=preselect_quick_type, quick_types=quick_types,
                           optional_areas_by_type=optional_areas_by_type)


@app.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@shop_role_required('admin', 'tech')
def project_edit(project_id):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    assets = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_simulator = 0 AND is_owner_placeholder = 0 ORDER BY tag").fetchall()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Project name is required.", "danger")
            quick_types = _all_quick_types(conn)
            conn.close()
            return render_template("project_form.html", project=project, assets=assets, quick_types=quick_types)
        asset_id = request.form.get("asset_id") or None
        # QA fix qa-project-missing-plane: see project_new's identical check.
        if asset_id and not conn.execute(
                "SELECT 1 FROM assets WHERE id = ? AND deleted_at IS NULL", (asset_id,)).fetchone():
            flash("That aircraft is no longer on file. Pick another aircraft (or none) and save again.", "danger")
            quick_types = _all_quick_types(conn)
            kept = dict(project)
            kept.update(name=name, description=request.form.get("description", "").strip(),
                        scheduled_date=request.form.get("scheduled_date", "").strip() or None,
                        scheduled_end_date=request.form.get("scheduled_end_date", "").strip() or None,
                        scheduled_color=request.form.get("scheduled_color", "").strip() or None,
                        prework_checklist=request.form.get("prework_checklist", "").strip() or None,
                        standard_items=request.form.get("standard_items", "").strip() or None,
                        asset_id=None)
            conn.close()
            return render_template("project_form.html", project=kept, assets=assets, quick_types=quick_types)
        scheduled_date = request.form.get("scheduled_date", "").strip() or None
        scheduled_end_date = request.form.get("scheduled_end_date", "").strip() or None
        if scheduled_date and not scheduled_end_date:
            try:
                scheduled_end_date = (date.fromisoformat(scheduled_date) + timedelta(days=7)).isoformat()
            except ValueError:
                pass
        if scheduled_end_date and scheduled_date and scheduled_end_date < scheduled_date:
            scheduled_end_date = scheduled_date
        scheduled_color = request.form.get("scheduled_color", "").strip() or None
        conn.execute("""UPDATE projects SET name = ?, description = ?, asset_id = ?, scheduled_date = ?,
                         scheduled_end_date = ?, scheduled_color = ?, prework_checklist = ?, standard_items = ? WHERE id = ?""",
                     (name, request.form.get("description", "").strip(), asset_id, scheduled_date,
                      scheduled_end_date, scheduled_color, request.form.get("prework_checklist", "").strip() or None,
                      request.form.get("standard_items", "").strip() or None, project_id))
        conn.commit()
        conn.close()
        flash("Project updated.", "success")
        return redirect(url_for("project_detail", project_id=project_id))
    quick_types = _all_quick_types(conn)
    conn.close()
    return render_template("project_form.html", project=project, assets=assets, quick_types=quick_types)


# ---------------------------------------------------------------------------
# Found items: owner approval of extra work (QA feat-owner-squawk-approval).
# A tech/admin adds a found item to a job (description, photos, estimate);
# the plane's owner gets a text/email with a link to their My Aircraft page
# on the customer portal, where they Approve or Decline it (see
# customer.customer_found_item_decide). Approved items become a Sub Area on
# the project so the work and parts land there; declined ones stay on
# record. The owner is whoever is linked to the job's plane in Customers.
# ---------------------------------------------------------------------------

def _found_items_for_project(conn, project_id):
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM found_items WHERE project_id = ? ORDER BY (status = 'waiting') DESC, created_at DESC, id DESC", (project_id,)).fetchall()]
    for it in items:
        it["photos"] = [r["filename"] for r in conn.execute(
            "SELECT filename FROM photos WHERE found_item_id = ? ORDER BY id", (it["id"],)).fetchall()]
        it["messages"] = found_item_messages(conn, it["id"])
    return items


def _project_owner_customers(conn, project):
    if not project["asset_id"]:
        return []
    return conn.execute("""SELECT c.* FROM customers c JOIN customer_assets ca ON ca.customer_id = c.id
                           WHERE ca.asset_id = ? AND c.active = 1""", (project["asset_id"],)).fetchall()


def _found_default_labor_rate(conn=None):
    """The shop labor rate last used on a found item, so it doesn't have to
    be typed every time (Settings has no separate shop billing rate)."""
    own = conn is None
    conn = conn or get_db()
    row = conn.execute("SELECT est_labor_rate FROM found_items WHERE est_labor_rate > 0 ORDER BY id DESC LIMIT 1").fetchone()
    if own:
        conn.close()
    return row["est_labor_rate"] if row else None


def _notify_owner_job_ready(conn, project):
    """Texts/emails every owner linked to the plane that this job is done
    and ready for pickup - same channels/settings as found items above
    (QA finding feat-job-closeout-owner-ready). Returns how many owners
    were reached."""
    if not project["asset_id"]:
        return 0
    asset = conn.execute("SELECT tag FROM assets WHERE id = ?", (project["asset_id"],)).fetchone()
    tag = asset["tag"] if asset else "your aircraft"
    total = _project_all_time_total(conn, project["id"])
    link = url_for("customer.customer_asset", asset_id=project["asset_id"], _external=True)
    msg = f"{tag} is ready for pickup. Total ${total:.2f}. See the work done in your portal: {link}"
    settings = notify.get_settings(conn)
    reached = 0
    for c in _project_owner_customers(conn, project):
        ok_mail, _e = notify.send_email(settings, c["email"], f"{tag} is ready for pickup", msg)
        ok_sms, _e = notify.send_sms(settings, c["phone"], msg) if c["phone"] else (False, None)
        reached += 1 if (ok_mail or ok_sms) else 0
    return reached


def _project_closeout_checklist(conn, project, usage_by_section, plane_squawks, labor_sessions):
    """What's still loose on this job, for the "Close out this job" pop-up
    on Mark Completed (QA finding feat-job-closeout-owner-ready): each
    applicable check is either green ("ok", nothing to do) or amber ("warn",
    with a one-tap link/action to go fix it) - a check that doesn't apply to
    this job (e.g. no plane attached, nothing billable) is left out
    entirely. Reuses the data project_detail() already loaded; see
    NOT DEPLOYED YET in the runner instructions before changing this."""
    items = []

    # Discrepancies / Sub Areas already on this job (the Parts tab list).
    non_general = {name: data for name, data in usage_by_section.items() if name != "General"}
    if non_general:
        open_names = [name for name, data in non_general.items() if not data.get("completed_at")]
        if not open_names:
            text = (f"{next(iter(non_general))} discrepancy marked Done" if len(non_general) == 1
                    else f"All {len(non_general)} discrepancies marked Done")
            items.append({"status": "ok", "text": text})
        else:
            text = (f"{open_names[0]} discrepancy still open" if len(open_names) == 1
                    else f"{len(open_names)} discrepancies still open: " + ", ".join(open_names[:3])
                    + ("..." if len(open_names) > 3 else ""))
            items.append({"status": "warn", "text": text, "anchor": "tab-parts", "action_label": "Open it"})

    # This plane's open squawks (same list shown in the sidebar card).
    if project["asset_id"]:
        if plane_squawks:
            for sq in plane_squawks[:3]:
                label = (sq.get("notes") or "(no details given)").strip().replace("\n", " ")[:60]
                items.append({"status": "warn", "text": f"Squawk still open: {label}",
                              "anchor": f"squawk-{sq['kind']}-{sq['squawk_id']}", "action_label": "Open it"})
            if len(plane_squawks) > 3:
                items.append({"status": "warn", "text": f"+{len(plane_squawks) - 3} more open squawk(s)"})
        else:
            tag = project["asset_display_tag"] or "this plane"
            items.append({"status": "ok", "text": f"No open squawks on {tag}"})

    # Logbook entries saved against this job (logbook.py).
    saved = conn.execute("SELECT COUNT(*) c FROM logbook_entries WHERE project_id = ? AND deleted_at IS NULL",
                         (project["id"],)).fetchone()["c"]
    if saved:
        items.append({"status": "ok", "text": "Logbook entry saved"})
    else:
        items.append({"status": "warn", "text": "No logbook entry saved yet",
                      "url": url_for("logbook.project_logbook", project_id=project["id"]), "action_label": "Draft it"})

    # Billing (QA feat-shop-job-payments): only applies if there's anything
    # to bill in the first place.
    total_bill = _project_all_time_total(conn, project["id"])
    if total_bill > 0:
        payment_status = project["payment_status"] or "not_invoiced"
        # period=all, since this job's billing period may not be the
        # current month and the row has to actually be on the page for the
        # #proj-<id> anchor to land on it.
        billing_url = url_for("shop_billing", period="all") + f"#proj-{project['id']}"
        if payment_status == "paid":
            items.append({"status": "ok", "text": f"Paid in full (${total_bill:.2f})"})
        elif payment_status == "invoiced":
            items.append({"status": "warn", "text": f"${total_bill:.2f} invoiced, not paid yet",
                          "url": billing_url, "action_label": "Open it"})
        else:
            items.append({"status": "warn", "text": f"${total_bill:.2f} not invoiced yet",
                          "url": billing_url, "action_label": "Send invoice"})

    # Who's still clocked in on this job (see the labor timer widget /
    # /api/labor/stop for the same manual-stop fallback).
    running = [s for s in labor_sessions if not s["ended_at"]]
    if running:
        names = ", ".join(sorted({s["laborer_name"] for s in running}))
        items.append({"status": "warn", "text": f"Still clocked in: {names}",
                      "stop_ids": [s["id"] for s in running],
                      "action_label": "Stop timer" + ("s" if len(running) > 1 else "")})
    else:
        items.append({"status": "ok", "text": "No workers still clocked in"})

    return items


def _notify_owner_found_items(conn, project):
    """Texts/emails every owner linked to the plane about the found items
    still waiting on them. Returns how many owners were reached."""
    waiting = conn.execute("SELECT COUNT(*) c FROM found_items WHERE project_id = ? AND status = 'waiting'",
                           (project["id"],)).fetchone()["c"]
    if not waiting:
        return 0
    asset = conn.execute("SELECT tag FROM assets WHERE id = ?", (project["asset_id"],)).fetchone()
    tag = asset["tag"] if asset else "your aircraft"
    link = url_for("customer.customer_asset", asset_id=project["asset_id"], _external=True)
    msg = (f"We found {waiting} item{'s' if waiting != 1 else ''} on {tag} that need{'s' if waiting == 1 else ''} "
           f"your OK. See photos and prices, then approve or decline: {link}")
    settings = notify.get_settings(conn)
    reached = 0
    for c in _project_owner_customers(conn, project):
        ok_mail, _e = notify.send_email(settings, c["email"], f"{tag}: items need your approval", msg)
        ok_sms, _e = notify.send_sms(settings, c["phone"], msg) if c["phone"] else (False, None)
        reached += 1 if (ok_mail or ok_sms) else 0
    return reached


@app.route("/projects/<int:project_id>/found-items", methods=["POST"])
@shop_role_required('admin', 'tech', 'inspector')
def found_item_new(project_id):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    description = (request.form.get("description") or "").strip()[:500]
    parts = _parse_qty(request.form.get("est_parts") or "0", allow_zero=True)
    hours = _parse_qty(request.form.get("est_labor_hours") or "0", allow_zero=True)
    rate = _parse_qty(request.form.get("est_labor_rate") or "0", allow_zero=True)
    if not description or parts is None or hours is None or rate is None:
        conn.close()
        flash("Describe what you found, and use numbers (0 or more) for the estimate.", "danger")
        return redirect(url_for("project_detail", project_id=project_id) + "#found-items")
    total = round(parts + hours * rate, 2)
    cur = conn.execute("""INSERT INTO found_items (project_id, asset_id, description, est_parts, est_labor_hours, est_labor_rate,
                           est_total, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?)""",
                       (project_id, project["asset_id"], description, parts, hours, rate, total, session.get("user_name"), now_iso()))
    item_id = cur.lastrowid
    conn.commit()
    _saved, err = _add_photos(conn, request.files.getlist("photos"), "found_item_id", item_id)
    if err:
        flash(err, "warning")
    if request.form.get("notify_owner") == "on":
        reached = _notify_owner_found_items(conn, project)
        conn.execute("UPDATE found_items SET notified_at = ? WHERE project_id = ? AND status = 'waiting'",
                     (now_iso(), project_id))
        conn.commit()
        flash("Found item added and the owner was notified." if reached else
              "Found item added. The owner couldn't be texted or emailed (no owner linked to this plane, "
              "or email/SMS isn't set up) - it's waiting on their My Aircraft page.", "success" if reached else "warning")
    else:
        flash("Found item added - it's waiting on the owner's My Aircraft page.", "success")
    conn.close()
    return redirect(url_for("project_detail", project_id=project_id) + "#found-items")


@app.route("/found-items/<int:item_id>/notify", methods=["POST"])
@shop_role_required('admin', 'tech', 'inspector')
def found_item_notify(item_id):
    conn = get_db()
    item = conn.execute("SELECT * FROM found_items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        conn.close()
        abort(404)
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (item["project_id"],)).fetchone()
    reached = _notify_owner_found_items(conn, project)
    if reached:
        conn.execute("UPDATE found_items SET notified_at = ? WHERE project_id = ? AND status = 'waiting'",
                     (now_iso(), project["id"]))
        conn.commit()
    conn.close()
    flash("Owner notified again." if reached else "Couldn't reach the owner by text or email.",
          "success" if reached else "warning")
    return redirect(url_for("project_detail", project_id=item["project_id"]) + "#found-items")


@app.route("/found-items/<int:item_id>/message", methods=["POST"])
@shop_role_required('admin', 'tech', 'inspector')
def found_item_message_new(item_id):
    """The shop's side of the back-and-forth on one found item - a note
    (with an optional photo) added to its conversation thread, visible to
    the owner on their My Aircraft page. Works whatever the item's current
    status is, so the shop can keep explaining or answering a question
    even after the owner has already approved or declined."""
    conn = get_db()
    item = conn.execute("SELECT * FROM found_items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        conn.close()
        abort(404)
    body = (request.form.get("body") or "").strip()[:1000]
    if not body:
        conn.close()
        flash("Type a note before sending.", "danger")
        return redirect(url_for("project_detail", project_id=item["project_id"]) + "#found-items")
    cur = conn.execute("""INSERT INTO found_item_messages (found_item_id, author_type, author_name, body, created_at)
                          VALUES (?, 'shop', ?, ?, ?)""",
                       (item_id, session.get("user_name"), body, now_iso()))
    conn.commit()
    _saved, err = _add_photos(conn, request.files.getlist("photos"), "found_item_message_id", cur.lastrowid)
    if err:
        flash(err, "warning")
    conn.close()
    flash("Note added.", "success")
    return redirect(url_for("project_detail", project_id=item["project_id"]) + "#found-items")


@app.route("/found-items/<int:item_id>/delete", methods=["POST"])
@shop_role_required('admin')
def found_item_delete(item_id):
    """Removes a found item the owner hasn't answered yet (added by mistake).
    Answered ones stay - they're the record of what the owner decided."""
    conn = get_db()
    item = conn.execute("SELECT * FROM found_items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        conn.close()
        abort(404)
    if item["status"] != "waiting":
        conn.close()
        flash("The owner already answered this one, so it stays on the record.", "warning")
        return redirect(url_for("project_detail", project_id=item["project_id"]) + "#found-items")
    conn.execute("UPDATE photos SET found_item_id = NULL WHERE found_item_id = ?", (item_id,))
    conn.execute("DELETE FROM found_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    flash("Found item removed.", "success")
    return redirect(url_for("project_detail", project_id=item["project_id"]) + "#found-items")


@app.route("/projects/<int:project_id>")
@shop_role_required('admin', 'tech', 'apprentice', 'inspector')
def project_detail(project_id):
    conn = get_db()
    project = conn.execute("""SELECT projects.*, a.id as asset_display_id, a.tag as asset_display_tag,
                               a.name as asset_display_name, a.maint_oil_type as asset_maint_oil_type,
                               a.maint_tire_nose as asset_maint_tire_nose, a.maint_tire_mains as asset_maint_tire_mains,
                               a.maint_other as asset_maint_other
                               FROM projects LEFT JOIN assets a ON a.id = projects.asset_id
                               WHERE projects.id = ?""", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    # A new project's intake check comes first (small Skip on that page).
    if project["intake_status"] == "pending":
        conn.close()
        return redirect(url_for("project_intake", project_id=project_id))
    intake = None
    if project["intake_json"]:
        try:
            intake = json.loads(project["intake_json"])
        except (TypeError, ValueError):
            intake = None
    usage_rows = conn.execute("""
        SELECT p.id as part_id, p.name, p.barcode, p.unit, p.unit_cost,
               t.section as section,
               SUM(CASE WHEN t.type='out' THEN t.qty ELSE -t.qty END) as qty_used
        FROM transactions t JOIN parts p ON p.id = t.part_id
        WHERE t.project_id = ?
        GROUP BY p.id, t.section
        HAVING qty_used > 0
        ORDER BY p.name
    """, (project_id,)).fetchall()
    total_cost = sum((row["qty_used"] or 0) * (row["unit_cost"] or 0) for row in usage_rows)

    # Group parts usage into sub-area "folders" (e.g. Brakes, Engine) so a
    # project covering multiple systems on the same plane/job can be browsed
    # by area, not just as one flat parts list. Anything scanned without a
    # section picked falls into "General".
    usage_by_section = {}
    for row in usage_rows:
        label = row["section"] or "General"
        usage_by_section.setdefault(label, {"rows": [], "cost": 0.0})
        usage_by_section[label]["rows"].append(row)
        usage_by_section[label]["cost"] += (row["qty_used"] or 0) * (row["unit_cost"] or 0)
    # Sub areas created ahead of time (via "Add Sub Area") but with no parts
    # scanned into them yet should still show up as an empty folder, so the
    # area is visibly there and ready rather than looking like it vanished.
    empty_sections = [r["name"] for r in conn.execute(
        "SELECT name FROM project_sections WHERE project_id = ?", (project_id,)
    ).fetchall()]
    for name in empty_sections:
        usage_by_section.setdefault(name, {"rows": [], "cost": 0.0})

    # A section can also show up here purely because a scan was tagged with
    # that name (transactions.section), without "Add Sub Area" ever having
    # been used - give it a real project_sections row now so it can be
    # checked off / renamed like any other, same as the others.
    for name in usage_by_section:
        if name != "General":
            conn.execute("INSERT OR IGNORE INTO project_sections (project_id, name, created_at) VALUES (?, ?, ?)",
                         (project_id, name, now_iso()))
    conn.commit()
    section_meta = {r["name"]: dict(r) for r in conn.execute(
        """SELECT id, name, completed_at, completed_by, confirm_requested_at, confirm_requested_by,
                  sent_back_at, sent_back_by, notes, description,
                  linked_squawk_kind, linked_squawk_id, linked_todo_id
           FROM project_sections WHERE project_id = ?""", (project_id,)).fetchall()}
    for name, section_data in usage_by_section.items():
        meta = section_meta.get(name)
        section_data["id"] = meta["id"] if meta else None
        section_data["completed_at"] = meta["completed_at"] if meta else None
        section_data["completed_by"] = meta["completed_by"] if meta else None
        section_data["confirm_requested_at"] = meta["confirm_requested_at"] if meta else None
        section_data["confirm_requested_by"] = meta["confirm_requested_by"] if meta else None
        section_data["sent_back_at"] = meta["sent_back_at"] if meta else None
        section_data["sent_back_by"] = meta["sent_back_by"] if meta else None
        section_data["notes"] = meta["notes"] if meta else None
        section_data["description"] = meta["description"] if meta else None
        # A Discrepancy that came from "Fix on this job"/"Do on this job"
        # keeps the squawk's/to-do's own Reported/Assigned/Working/
        # Inspection/Done pills and assigned tech showing here too, instead
        # of that getting lost in the move (idea "Reported Assigned Working
        # Inspection Done"; builds on the "Assigned to <name>" work from QA
        # finding ux-squawk-on-project).
        section_data["linked_squawk"] = None
        section_data["linked_todo"] = None
        if meta and meta["linked_squawk_kind"]:
            section_data["linked_squawk"] = _squawk_by_kind_id(
                conn, meta["linked_squawk_kind"], meta["linked_squawk_id"])
        elif meta and meta["linked_todo_id"]:
            section_data["linked_todo"] = _todo_by_id(conn, meta["linked_todo_id"])
            if section_data["linked_todo"] is not None:
                # It's linked to this very Discrepancy, so todo_step_pills'
                # "has a linked Sub Area" check (t.link) is always true here.
                section_data["linked_todo"]["link"] = True

    # Open sections first alphabetically, then ones awaiting confirmation,
    # then fully completed ones at the bottom ordered by when they were
    # completed; "General" always last since it isn't a real checkable area.
    usage_by_section = dict(sorted(usage_by_section.items(),
                                   key=lambda kv: (kv[0] == "General", kv[1]["completed_at"] is not None,
                                                    kv[1]["confirm_requested_at"] is not None,
                                                    kv[1]["completed_at"] or kv[1]["confirm_requested_at"] or "", kv[0])))

    tx = conn.execute("""SELECT t.*, p.name as part_name, p.barcode as part_barcode FROM transactions t
                          JOIN parts p ON p.id = t.part_id
                          WHERE t.project_id = ? ORDER BY t.created_at DESC, t.id DESC""",
                       (project_id,)).fetchall()
    all_parts = conn.execute("SELECT * FROM parts ORDER BY name").fetchall()
    photos = conn.execute("SELECT * FROM photos WHERE project_id = ? ORDER BY is_cover DESC, created_at DESC",
                           (project_id,)).fetchall()
    open_orders = conn.execute("SELECT * FROM orders WHERE project_id = ? AND status = 'pending' ORDER BY ordered_date",
                                (project_id,)).fetchall()

    labor_sessions = conn.execute("""
        SELECT ls.*, l.name as laborer_name
        FROM labor_sessions ls JOIN laborers l ON l.id = ls.laborer_id
        WHERE ls.project_id = ?
        ORDER BY ls.started_at DESC
    """, (project_id,)).fetchall()
    labor_total_cost = sum((s["cost"] or 0) for s in labor_sessions if s["ended_at"])
    labor_total_hours = sum((s["hours"] or 0) for s in labor_sessions if s["ended_at"])

    known_sections = [r["section"] for r in conn.execute("""
        SELECT section FROM (
            SELECT name as section FROM project_sections WHERE project_id = ?
            UNION
            SELECT section FROM transactions
                WHERE project_id = ? AND section IS NOT NULL AND TRIM(section) != ''
        )
        ORDER BY section
    """, (project_id, project_id)).fetchall()]
    found_items = _found_items_for_project(conn, project_id)
    has_owner = bool(project["asset_id"] and _project_owner_customers(conn, project))
    # QA feat-shop-job-payments: what Mark Completed should warn about, if
    # anything - 0 once the job's marked Paid.
    unpaid_amount = 0 if (project["payment_status"] or "not_invoiced") == "paid" else _project_all_time_total(conn, project_id)

    # QA finding ux-squawk-on-project: the plane's own open squawks and
    # to-dos, right on the job they're likely to get fixed on, each tagged
    # with whichever Sub Area (on this job or another) already stands in
    # for it, if any - see _find_or_create_linked_section.
    plane_squawks, plane_todos_open = [], []
    if project["asset_id"]:
        for sq in get_plane_open_squawks(conn, project["asset_id"]):
            d = dict(sq)
            link = conn.execute("""SELECT ps.project_id, p.code, p.name FROM project_sections ps
                                   JOIN projects p ON p.id = ps.project_id
                                   WHERE ps.linked_squawk_kind = ? AND ps.linked_squawk_id = ?""",
                                (sq["kind"], sq["squawk_id"])).fetchone()
            d["link"] = dict(link) if link else None
            plane_squawks.append(d)
        todo_rows = conn.execute("""SELECT pt.*, u.name as assigned_to_name FROM plane_todos pt
                                    LEFT JOIN users u ON u.id = pt.assigned_to
                                    WHERE pt.asset_id = ? AND pt.done = 0 ORDER BY pt.created_at""",
                                 (project["asset_id"],)).fetchall()
        for t in todo_rows:
            d = dict(t)
            link = conn.execute("""SELECT ps.project_id, p.code, p.name FROM project_sections ps
                                   JOIN projects p ON p.id = ps.project_id
                                   WHERE ps.linked_todo_id = ?""", (t["id"],)).fetchone()
            d["link"] = dict(link) if link else None
            plane_todos_open.append(d)

    # QA finding feat-job-closeout-owner-ready: what's still loose on this
    # job, shown in the "Close out this job" pop-up on Mark Completed, plus
    # a preview of the "Tell the owner it's ready" message it can send using
    # the same email/text channels as Found Items above.
    closeout_checklist = [] if project["status"] in ("completed", "archived") else \
        _project_closeout_checklist(conn, project, usage_by_section, plane_squawks, labor_sessions)
    owner_notify_preview = ""
    if has_owner:
        owner_names = [c["name"] for c in _project_owner_customers(conn, project)]
        who = owner_names[0] if len(owner_names) == 1 else f"{owner_names[0]} +{len(owner_names) - 1} more"
        tag = project["asset_display_tag"] or "This aircraft"
        owner_notify_preview = (f'Email + text to {who}: "{tag} is ready for pickup. '
                                f'Total ${_project_all_time_total(conn, project_id):.2f}. '
                                f'See the work done in your portal."')
    conn.close()
    return render_template("project_detail.html", project=project, usage_by_section=usage_by_section,
                           total_cost=total_cost, tx=tx, all_parts=all_parts, photos=photos, open_orders=open_orders,
                           labor_sessions=labor_sessions, labor_total_cost=labor_total_cost,
                           labor_total_hours=labor_total_hours, known_sections=known_sections, intake=intake,
                           found_items=found_items, found_has_owner=has_owner, unpaid_amount=unpaid_amount,
                           plane_squawks=plane_squawks, plane_todos_open=plane_todos_open,
                           closeout_checklist=closeout_checklist, owner_notify_preview=owner_notify_preview,
                           found_labor_rate=_found_default_labor_rate(conn=None))


# ---------------------------------------------------------------- project intake
# Checked on the plane before anyone touches it, right after a project is
# created. Each check is OK / Issue (+ note); issues, squawks and damage
# ticked "address in this project" each get their own Sub Area.
INTAKE_CHECKS = [
    ("mags", "Mag drop", "Run-up RPM drop, left and right mag"),
    ("oil", "Oil usage", "Oil level / quarts added since last visit, burn rate"),
    ("brakes", "Brake pedal feel", "Firm, even, no sponginess or fade"),
    ("gauges", "Gauges working", "Engine and flight instruments all reading"),
]
INTAKE_EXTRA_ROWS = 6  # squawk / damage lines offered on the form


def _intake_from_form(form, files=None):
    """(data, errors) from the intake form. `files` (request.files), when
    given, lets each damage line pick up its own photo - see the
    damage_{i}_photo inputs in project_intake.html."""
    data = {"checks": [], "squawks": [], "damage": [], "notes": (form.get("notes") or "").strip()[:1000]}
    errors = []
    for key, label, _hint in INTAKE_CHECKS:
        status = form.get(f"{key}_status")
        if status not in ("ok", "issue"):
            errors.append(f"{label}: pick OK or Issue.")
        item = {"key": key, "label": label, "status": status,
                "note": (form.get(f"{key}_note") or "").strip()[:300],
                "address": status == "issue" and form.get(f"{key}_address") == "1"}
        if key == "mags":
            item["left"] = (form.get("mags_left") or "").strip()[:10]
            item["right"] = (form.get("mags_right") or "").strip()[:10]
        if key == "oil":
            item["qts"] = (form.get("oil_qts") or "").strip()[:10]
        if status == "issue" and not item["note"]:
            errors.append(f"{label}: say what the issue is.")
        data["checks"].append(item)
    for kind in ("squawks", "damage"):
        for i in range(INTAKE_EXTRA_ROWS):
            text = (form.get(f"{kind}_{i}") or "").strip()[:200]
            if text:
                item = {"text": text, "address": form.get(f"{kind}_{i}_address") == "1"}
                if kind == "damage":
                    # Timestamped at save, so it's on record that the damage
                    # was already there at check-in, not caused in the shop.
                    item["at"] = now_iso()
                    photo = files.get(f"damage_{i}_photo") if files else None
                    if photo and photo.filename and allowed_image(photo.filename):
                        item["photo"] = save_upload(photo)
                data[kind].append(item)
    return data, errors


def _intake_sub_areas(data):
    """Sub Area names for check/damage items ticked "address in this
    project". Squawks are handled separately in project_intake - each one
    becomes a real plane squawk, claimed on this job via the same
    Sub-Area-linking "Fix on this job" uses, instead of a plain named area
    (QA finding ux-squawk-on-project: one list, not two)."""
    names = []
    for c in data["checks"]:
        if c.get("address"):
            names.append(c["label"])
    for it in data["damage"]:
        if it.get("address"):
            names.append(f"Damage: {it['text'][:60]}")
    return names


@app.route("/projects/<int:project_id>/intake", methods=["GET", "POST"])
@shop_role_required('admin', 'tech')
def project_intake(project_id):
    conn = get_db()
    project = conn.execute("""SELECT projects.*, a.tag as asset_display_tag FROM projects
                              LEFT JOIN assets a ON a.id = projects.asset_id WHERE projects.id = ?""",
                           (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    existing = None
    if project["intake_json"]:
        try:
            existing = json.loads(project["intake_json"])
        except (TypeError, ValueError):
            existing = None
    if request.method == "POST":
        if request.form.get("action") == "skip":
            if project["intake_status"] in (None, "pending"):
                conn.execute("UPDATE projects SET intake_status = 'skipped', intake_at = ?, intake_by = ? WHERE id = ?",
                             (now_iso(), session.get("user_name"), project_id))
                conn.commit()
            conn.close()
            flash("Intake check skipped.", "warning")
            return redirect(url_for("project_detail", project_id=project_id))
        data, errors = _intake_from_form(request.form, request.files)
        if errors:
            conn.close()
            for e in errors:
                flash(e, "danger")
            return render_template("project_intake.html", project=project, checks=INTAKE_CHECKS,
                                   rows=INTAKE_EXTRA_ROWS, data=data, form=request.form)
        new_areas = _intake_sub_areas(data)
        for name in new_areas:
            conn.execute("INSERT OR IGNORE INTO project_sections (project_id, name, created_at) VALUES (?, ?, ?)",
                         (project_id, name, now_iso()))
        # Squawks entered here become real plane squawks too, so there's one
        # list, not two - and one ticked "address in this project" gets
        # claimed on this job the same way "Fix on this job" does. A project
        # with no plane attached falls back to a plain named Sub Area, since
        # there's no plane to attach a real squawk to.
        by, uid = session.get("user_name"), session.get("user_id")
        for it in data["squawks"]:
            if project["asset_id"]:
                cur = conn.execute(
                    "INSERT INTO plane_squawks (asset_id, notes, reported_by, reported_at) VALUES (?, ?, ?, ?)",
                    (project["asset_id"], it["text"], by, now_iso()))
                it["squawk_id"] = cur.lastrowid
                if it.get("address"):
                    _find_or_create_linked_section(conn, project_id, it["text"],
                                                   linked_squawk_kind="quick", linked_squawk_id=it["squawk_id"])
                    conn.execute("""UPDATE plane_squawks SET acknowledged_at = ?, acknowledged_by = ?,
                                    assigned_to = ?, worker_acknowledged_at = ?, worker_acknowledged_by = ?
                                    WHERE id = ?""", (now_iso(), by, uid, now_iso(), by, it["squawk_id"]))
                    new_areas.append(it["text"][:60])
            elif it.get("address"):
                conn.execute("INSERT OR IGNORE INTO project_sections (project_id, name, created_at) VALUES (?, ?, ?)",
                             (project_id, f"Squawk: {it['text'][:60]}", now_iso()))
                new_areas.append(f"Squawk: {it['text'][:60]}")
        conn.execute("""UPDATE projects SET intake_status = 'done', intake_json = ?, intake_at = ?, intake_by = ?
                        WHERE id = ?""", (json.dumps(data), now_iso(), session.get("user_name"), project_id))
        conn.commit()
        conn.close()
        flash("Intake check saved." + (f" Discrepanc{'ies' if len(new_areas) != 1 else 'y'} added: {', '.join(new_areas)}."
                                       if new_areas else ""), "success")
        return redirect(url_for("project_detail", project_id=project_id))
    conn.close()
    return render_template("project_intake.html", project=project, checks=INTAKE_CHECKS,
                           rows=INTAKE_EXTRA_ROWS, data=existing, form=None)


def _group_usage_by_section(usage_rows):
    """Groups already-fetched usage rows (each carrying a `section` key) into
    an ordered dict of section label -> list of rows. Named sections come
    first (alphabetical), "General" (untagged) comes last."""
    grouped = {}
    for row in usage_rows:
        label = row["section"] or "General"
        grouped.setdefault(label, []).append(row)
    return dict(sorted(grouped.items(), key=lambda kv: (kv[0] == "General", kv[0])))


@app.route("/projects/<int:project_id>/export.csv")
@shop_role_required('admin')
def project_export_csv(project_id):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    usage = conn.execute("""
        SELECT p.name, p.barcode, p.unit, p.unit_cost, t.section as section,
               SUM(CASE WHEN t.type='out' THEN t.qty ELSE -t.qty END) as qty_used
        FROM transactions t JOIN parts p ON p.id = t.part_id
        WHERE t.project_id = ?
        GROUP BY p.id, t.section
        HAVING qty_used > 0
        ORDER BY p.name
    """, (project_id,)).fetchall()
    conn.close()
    grouped = _group_usage_by_section(usage)

    buf = io.StringIO()
    writer = csv.writer(buf)
    grand_total = 0.0
    for section, rows in grouped.items():
        writer.writerow([section])
        writer.writerow(["Item", "SKU", "Quantity", "Unit", "Unit Cost", "Total"])
        section_total = 0.0
        for u in rows:
            total = (u["qty_used"] or 0) * (u["unit_cost"] or 0)
            section_total += total
            writer.writerow([u["name"], u["barcode"], u["qty_used"], u["unit"], u["unit_cost"], round(total, 2)])
        writer.writerow(["", "", "", "", "Subtotal", round(section_total, 2)])
        writer.writerow([])
        grand_total += section_total
    writer.writerow(["", "", "", "", "Grand Total", round(grand_total, 2)])
    fname = f"project_{project['code']}_materials.csv".replace("/", "-")
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.route("/projects/<int:project_id>/invoice.csv")
@shop_role_required('admin')
def project_invoice_csv(project_id):
    """Customer-facing export: part name, quantity used, and sale price only.
    Deliberately leaves out anything the customer shouldn't see - no barcode/
    SKU, no supplier, no purchase (unit) cost. Grouped by area/sub-system
    (e.g. Brakes, Engine) with its own header and subtotal, like a proper
    invoice broken down by system worked on."""
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    usage = conn.execute("""
        SELECT p.name, p.unit, p.sell_price, t.section as section,
               SUM(CASE WHEN t.type='out' THEN t.qty ELSE -t.qty END) as qty_used
        FROM transactions t JOIN parts p ON p.id = t.part_id
        WHERE t.project_id = ?
        GROUP BY p.id, t.section
        HAVING qty_used > 0
        ORDER BY p.name
    """, (project_id,)).fetchall()
    labor = conn.execute("""
        SELECT ls.*, l.name as laborer_name
        FROM labor_sessions ls JOIN laborers l ON l.id = ls.laborer_id
        WHERE ls.project_id = ? AND ls.ended_at IS NOT NULL
        ORDER BY ls.started_at
    """, (project_id,)).fetchall()
    conn.close()
    grouped = _group_usage_by_section(usage)

    buf = io.StringIO()
    writer = csv.writer(buf)
    grand_total = 0.0
    for section, rows in grouped.items():
        writer.writerow([section])
        writer.writerow(["Item", "Quantity", "Unit", "Sale Price", "Total"])
        section_total = 0.0
        for u in rows:
            total = (u["qty_used"] or 0) * (u["sell_price"] or 0)
            section_total += total
            writer.writerow([u["name"], u["qty_used"], u["unit"], u["sell_price"], round(total, 2)])
        writer.writerow(["", "", "", "Subtotal", round(section_total, 2)])
        writer.writerow([])
        grand_total += section_total

    if labor:
        writer.writerow(["Labor"])
        writer.writerow(["Laborer", "Task", "Hours", "Rate", "Total"])
        labor_total = 0.0
        for s in labor:
            cost = s["cost"] or 0
            labor_total += cost
            writer.writerow([s["laborer_name"], s["section"] or "General", round(s["hours"] or 0, 2),
                              s["rate"], round(cost, 2)])
        writer.writerow(["", "", "", "Labor Subtotal", round(labor_total, 2)])
        writer.writerow([])
        grand_total += labor_total

    writer.writerow(["", "", "", "Grand Total", round(grand_total, 2)])
    fname = f"project_{project['code']}_invoice.csv".replace("/", "-")
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.route("/projects/<int:project_id>/add_part", methods=["POST"])
@shop_role_required('admin', 'tech')
def project_add_part(project_id):
    conn = get_db()
    # deleted_at check matches the Scan page - no charging parts to a job
    # that's sitting in Recently Deleted.
    project = conn.execute("SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    part_id = request.form.get("part_id")
    performed_by = request.form.get("performed_by", "").strip() or None
    if not performed_by:
        flash("Select who's assigning this part (\"Scanning as\") first.", "danger")
        conn.close()
        return redirect(url_for("project_detail", project_id=project_id))
    qty = _parse_qty(request.form.get("qty"))
    if qty is None:
        flash("Quantity must be a number greater than zero.", "danger")
        conn.close()
        return redirect(url_for("project_detail", project_id=project_id))
    part = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    if not part:
        flash("Part not found.", "danger")
        conn.close()
        return redirect(url_for("project_detail", project_id=project_id))
    if qty <= 0:
        flash("Quantity must be greater than zero.", "danger")
        conn.close()
        return redirect(url_for("project_detail", project_id=project_id))
    if part["qty_on_hand"] < qty:
        flash(f"Only {part['qty_on_hand']:g} {part['unit']} of {part['name']} in stock.", "danger")
        conn.close()
        return redirect(url_for("project_detail", project_id=project_id))

    note = "Assigned to project"
    exp = part_expiry(part)
    if exp and exp["level"] == "expired":
        # The form's confirm() (project_detail.html) sets confirm_expired;
        # without it the part isn't used.
        if request.form.get("confirm_expired") != "1":
            flash(f"{part['name']} expired on {usdate(exp['date'])} - not assigned. Confirm \"Use anyway\" to use it.", "danger")
            conn.close()
            return redirect(url_for("project_detail", project_id=project_id))
        note += f" - used past expiration (expired {usdate(exp['date'])})"
    section = request.form.get("section", "").strip() or None
    conn.execute("UPDATE parts SET qty_on_hand = qty_on_hand - ?, updated_at = ? WHERE id = ?",
                 (qty, now_iso(), part_id))
    conn.execute("""INSERT INTO transactions (part_id, project_id, type, qty, note, performed_by, section, source, created_at)
                     VALUES (?, ?, 'out', ?, ?, ?, ?, 'assigned', ?)""",
                 (part_id, project_id, qty, note, performed_by, section, now_iso()))
    conn.commit()
    conn.close()
    flash(f"Assigned {qty:g} {part['unit']} of {part['name']} to project.", "success")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/status", methods=["POST"])
@shop_role_required('admin', 'tech')
def project_status(project_id):
    new_status = request.form.get("status")
    if new_status not in ("active", "completed", "on_hold", "archived"):
        abort(400)
    conn = get_db()
    current = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not current:
        conn.close()
        abort(404)
    # QA fix qa-project-double-complete: pressing Complete on a job that's
    # already completed (a double tap, or pressing it again later) used to
    # re-run the 100-hour/oil-change recording every time, duplicating the
    # plane's maintenance history entry. Already completed -> completed is
    # a no-op; reopening it first (to any other status) and completing it
    # again still records normally.
    if new_status == "completed" and current["status"] == "completed":
        conn.close()
        return redirect(url_for("project_detail", project_id=project_id))
    completed_at = now_iso() if new_status == "completed" else None
    completed_by = session.get("user_name") if new_status == "completed" else None
    conn.execute("UPDATE projects SET status = ?, completed_at = ?, completed_by = ? WHERE id = ?",
                 (new_status, completed_at, completed_by, project_id))
    conn.commit()
    if new_status == "completed":
        _record_inspection_from_project(conn, project_id)
        # QA finding feat-job-closeout-owner-ready: "Tell the owner it's
        # ready" checkbox on the Close out this job pop-up - same
        # email/text channels as Found Items, no new notification channel.
        if request.form.get("notify_owner") == "on":
            reached = _notify_owner_job_ready(conn, current)
            conn.execute("UPDATE projects SET ready_notified_at = ?, ready_notified_by = ? WHERE id = ?",
                         (now_iso(), session.get("user_name"), project_id))
            conn.commit()
            flash("Marked completed and the owner was told it's ready." if reached else
                  "Marked completed. The owner couldn't be texted or emailed (no owner linked to this plane, "
                  "or email/SMS isn't set up).", "success" if reached else "warning")
    conn.close()
    return redirect(url_for("project_detail", project_id=project_id))


def _project_inspection_kinds(name):
    """Which tracked maintenance a project is, from its name/quick type:
    '100hour' for a 100-hour ("100hr Inspection", "100 hour") and/or
    'oil_change' for an oil change."""
    n = (name or "").lower()
    kinds = []
    if re.search(r"\b100\s*-?\s*(hr|hrs|hour|hours)\b", n):
        kinds.append("100hour")
    if "oil" in n:
        kinds.append("oil_change")
    return kinds


def _record_inspection_from_project(conn, project_id):
    """Completing a 100-hr or Oil Change project on a plane records it as
    done at the plane's current tach (so the 100-hour countdown restarts),
    and a 100-hr completion returns a grounded plane to service
    (QA feat-100hr-countdown-grounding)."""
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project or not project["asset_id"]:
        return
    kinds = _project_inspection_kinds(project["name"])
    if not kinds:
        return
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (project["asset_id"],)).fetchone()
    if not asset or asset["tach_hours"] is None:
        return
    for kind in kinds:
        item = conn.execute("""SELECT * FROM maintenance_items WHERE asset_id = ? AND category = ? AND active = 1
                               AND type = 'hours' ORDER BY id LIMIT 1""", (asset["id"], kind)).fetchone()
        if not item:
            if kind != "100hour":
                continue
            cur = conn.execute("""INSERT INTO maintenance_items (asset_id, name, type, category, hour_type, interval_hours,
                                   active, created_at, updated_at) VALUES (?, '100-Hour Inspection', 'hours', '100hour',
                                   'tach', 100, 1, ?, ?)""", (asset["id"], now_iso(), now_iso()))
            item = conn.execute("SELECT * FROM maintenance_items WHERE id = ?", (cur.lastrowid,)).fetchone()
        reading = asset_meter(asset, item["hour_type"])
        conn.execute("UPDATE maintenance_items SET last_done_hours = ?, last_done_date = ?, updated_at = ? WHERE id = ?",
                     (reading, now_iso()[:10], now_iso(), item["id"]))
        conn.execute("""INSERT INTO maintenance_log (item_id, completed_at, completed_hours, performed_by, note, project_id)
                         VALUES (?, ?, ?, ?, ?, ?)""",
                     (item["id"], now_iso(), reading, session.get("user_name"),
                      f"Project {project['code']} completed", project_id))
    conn.commit()
    if "100hour" in kinds and asset["grounded_at"]:
        from flight import return_plane_to_service
        return_plane_to_service(conn, asset["id"], session.get("user_name"))
        flash(f"{asset['tag']} is back in service - 100-hour recorded at tach {asset['tach_hours']:g}.", "success")
    else:
        flash(f"Recorded on {asset['tag']}'s maintenance at tach {asset['tach_hours']:g}.", "info")


_PROJECT_CODE_RE = re.compile(r"^(\d{2})-(\d+)$")


def _parse_project_code(code):
    """('26-003') -> (26, 3), or None if it isn't the auto YY-NNN format
    gen_project_code() generates (a hand-typed custom code isn't part of
    the auto-numbering sequence, so it's never a renumbering candidate)."""
    m = _PROJECT_CODE_RE.match(code or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _projects_after(conn, yy, num):
    """Active (non-deleted) projects in year `yy` numbered after `num`, as
    (id, num) pairs sorted lowest number first - the ones a
    renumber-after-delete would shift down by one to close the gap left at
    `num`."""
    rows = conn.execute("SELECT id, code FROM projects WHERE deleted_at IS NULL AND code LIKE ?",
                        (f"{yy:02d}-%",)).fetchall()
    after = []
    for r in rows:
        parsed = _parse_project_code(r["code"])
        if parsed and parsed[0] == yy and parsed[1] > num:
            after.append((r["id"], parsed[1]))
    return sorted(after, key=lambda x: x[1])


@app.route("/projects/<int:project_id>/delete", methods=["POST"])
@shop_role_required('admin')
def project_delete(project_id):
    """Archives the project instead of deleting it - all of its transaction
    history, parts usage, and photos stay intact and searchable; it's just
    hidden from the default project views."""
    conn = get_db()
    project = conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    conn.execute("UPDATE projects SET status = 'archived' WHERE id = ?", (project_id,))
    conn.commit()
    conn.close()
    flash(f"Project '{project['name']}' archived. Its history is kept - find it under the Archived filter.", "success")
    return redirect(url_for("projects_list"))


@app.route("/projects/<int:project_id>/trash", methods=["POST"])
@shop_role_required('admin')
def project_trash(project_id):
    """Soft-deletes a project: hidden everywhere except Recently Deleted,
    where it can be restored or permanently purged."""
    conn = get_db()
    project = conn.execute("SELECT name, code FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    conn.execute("UPDATE projects SET deleted_at = ? WHERE id = ?", (now_iso(), project_id))
    conn.commit()
    parsed = _parse_project_code(project["code"])
    after = _projects_after(conn, *parsed) if parsed else []
    conn.close()
    flash(f"Project '{project['name']}' moved to Recently Deleted.", "success")
    if after:
        return redirect(url_for("project_renumber_confirm", yy=parsed[0], num=parsed[1]))
    return redirect(url_for("projects_list"))


@app.route("/projects/renumber")
@shop_role_required('admin')
def project_renumber_confirm():
    """Opt-in prompt after deleting a project, offered only when a gap was
    actually left in the auto-numbering: renumbering shifts every later
    project's code down by one to close it, which is exactly what a
    printed TASK- QR label (see project_labor_codes.html) has that
    project's OLD code baked into - a project involved here needs new
    labels printed after this. Never automatic; the flash on the previous
    page already confirmed the delete itself."""
    yy = request.args.get("yy", type=int)
    num = request.args.get("num", type=int)
    if yy is None or num is None:
        abort(400)
    conn = get_db()
    ids = [pid for pid, _n in _projects_after(conn, yy, num)]
    after = []
    if ids:
        rows = conn.execute("SELECT id, code, name FROM projects WHERE id IN (%s)" % ",".join("?" * len(ids)),
                            ids).fetchall()
        after = sorted(rows, key=lambda p: _parse_project_code(p["code"])[1])
    conn.close()
    if not after:
        return redirect(url_for("projects_list"))
    return render_template("project_renumber_confirm.html", yy=yy, num=num, after=after)


@app.route("/projects/renumber", methods=["POST"])
@shop_role_required('admin')
def project_renumber_apply():
    yy = request.form.get("yy", type=int)
    num = request.form.get("num", type=int)
    if yy is None or num is None:
        abort(400)
    conn = get_db()
    # If a resubmitted/double-clicked request already closed this gap, an
    # active project is already sitting on this exact code - nothing left
    # to shift, and trying again would collide with it. Bail out quietly.
    already = conn.execute("SELECT 1 FROM projects WHERE deleted_at IS NULL AND code = ?",
                            (f"{yy:02d}-{num:03d}",)).fetchone()
    if already:
        conn.close()
        flash("Nothing to renumber - that gap is already closed.", "info")
        return redirect(url_for("projects_list"))
    # The deleted project is soft-deleted, not gone - its row still holds
    # this exact code (UNIQUE across every project, deleted or not), so
    # that slot has to be freed before anything can move into it. Mangling
    # it here (not at delete time) keeps a plain trash/restore untouched
    # for the far more common case where nobody renumbers.
    conn.execute("UPDATE projects SET code = code || '-old' || id WHERE deleted_at IS NOT NULL AND code = ?",
                 (f"{yy:02d}-{num:03d}",))
    after = _projects_after(conn, yy, num)  # lowest number first - shift in this order so no code ever collides
    try:
        for pid, n in after:
            conn.execute("UPDATE projects SET code = ? WHERE id = ?", (f"{yy:02d}-{n - 1:03d}", pid))
        conn.commit()
    except sqlite3.IntegrityError:
        # Belt-and-suspenders: a duplicate/resubmitted request slipped past
        # the check above (e.g. near-simultaneous clicks). Don't half-apply
        # a renumber - roll back and tell the user rather than 500ing.
        conn.rollback()
        conn.close()
        flash("Couldn't renumber - it looks like this gap was already closed by another request. Refresh and try again if needed.", "warning")
        return redirect(url_for("projects_list"))
    conn.close()
    flash(f"Renumbered {len(after)} project{'s' if len(after) != 1 else ''} to close the gap. "
          "Reprint any TASK- QR labels for them - the old ones won't scan anymore.", "warning")
    return redirect(url_for("projects_list"))


@app.route("/projects/<int:project_id>/restore", methods=["POST"])
@shop_role_required('admin')
def project_restore(project_id):
    conn = get_db()
    project = conn.execute("SELECT name, code FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    # A code that no longer parses as plain YY-NNN was mangled by
    # project_renumber_apply to free its slot for another project - that
    # slot's taken now, so this one gets a fresh number instead of coming
    # back with the leftover mangled code.
    new_code = None if _parse_project_code(project["code"]) else gen_project_code(conn)
    if new_code:
        conn.execute("UPDATE projects SET deleted_at = NULL, code = ? WHERE id = ?", (new_code, project_id))
    else:
        conn.execute("UPDATE projects SET deleted_at = NULL WHERE id = ?", (project_id,))
    conn.commit()
    conn.close()
    flash(f"Project '{project['name']}' restored." + (f" Given a new code: {new_code}." if new_code else ""), "success")
    return redirect(url_for("trash_page"))


def _project_purge_preview(conn, project_id):
    """What Delete Forever would touch on this job besides the job itself -
    parts still net "taken" against it and hours clocked on it - so the
    Recently Deleted page can ask what to do with them first (QA fix
    qa-project-purge-erases-pay) instead of silently erasing a worker's pay
    or a part's usage history."""
    parts = conn.execute("""
        SELECT p.id as part_id, p.name, p.barcode, p.unit,
               SUM(CASE WHEN t.type = 'out' THEN t.qty WHEN t.type = 'in' THEN -t.qty ELSE 0 END) as qty
        FROM transactions t JOIN parts p ON p.id = t.part_id
        WHERE t.project_id = ? AND t.type IN ('out', 'in')
        GROUP BY t.part_id HAVING qty > 0 ORDER BY p.name
    """, (project_id,)).fetchall()
    hours = conn.execute("""
        SELECT ls.id, ls.hours, ls.cost, ls.section, ls.started_at, l.name as laborer_name
        FROM labor_sessions ls JOIN laborers l ON l.id = ls.laborer_id
        WHERE ls.project_id = ? AND ls.hours IS NOT NULL ORDER BY ls.started_at
    """, (project_id,)).fetchall()
    hours_out = []
    for h in hours:
        h = dict(h)
        try:
            h["date_label"] = datetime.strptime(h["started_at"][:10], "%Y-%m-%d").strftime("%b %-d")
        except ValueError:
            h["date_label"] = h["started_at"][:10]
        hours_out.append(h)
    return {
        "parts": [dict(r) for r in parts],
        "hours": hours_out,
        "hours_total": round(sum(h["hours"] or 0 for h in hours), 2),
    }


def _purge_project(conn, project_id, parts_action=None, hours_action=None):
    """parts_action: 'return' puts net-taken quantities back on the shelf
    (with a new "Returned - job deleted" transaction so a recount still
    adds up) and detaches the rest of the job's transactions; anything else
    (including None, a job with no parts) just detaches them, annotated
    "(deleted job)" so a part's own history keeps them instead of losing
    that record. hours_action: 'transfer' moves the job's labor_sessions to
    General Shop time (same laborer/dates/rate/pay, just no project) so pay
    totals don't change; anything else (including None) deletes them, same
    as the original behavior. Returns a summary dict for the flash message."""
    summary = {"parts_returned": 0, "hours_transferred": 0.0, "hours_removed": 0.0}
    if parts_action == "return":
        for row in conn.execute("""
                SELECT part_id, SUM(CASE WHEN type = 'out' THEN qty WHEN type = 'in' THEN -qty ELSE 0 END) as qty
                FROM transactions WHERE project_id = ? AND type IN ('out', 'in')
                GROUP BY part_id HAVING qty > 0""", (project_id,)).fetchall():
            conn.execute("UPDATE parts SET qty_on_hand = qty_on_hand + ?, updated_at = ? WHERE id = ?",
                         (row["qty"], now_iso(), row["part_id"]))
            conn.execute("""INSERT INTO transactions (part_id, project_id, type, qty, note, performed_by, source, created_at)
                             VALUES (?, NULL, 'in', ?, 'Returned - job deleted', ?, 'assigned', ?)""",
                         (row["part_id"], row["qty"], session.get("user_name"), now_iso()))
            summary["parts_returned"] += row["qty"]
        conn.execute("UPDATE transactions SET project_id = NULL WHERE project_id = ?", (project_id,))
    else:
        conn.execute("""UPDATE transactions SET project_id = NULL,
                         note = CASE WHEN note IS NULL OR note = '' THEN 'Deleted job' ELSE note || ' (deleted job)' END
                         WHERE project_id = ?""", (project_id,))
    conn.execute("DELETE FROM photos WHERE project_id = ?", (project_id,))
    conn.execute("UPDATE orders SET project_id = NULL WHERE project_id = ?", (project_id,))
    conn.execute("UPDATE maintenance_log SET project_id = NULL WHERE project_id = ?", (project_id,))
    # QA fix qa-project-purge-crash: logbook entries and owner-approved
    # (found) items are kept, not deleted - each already has its own
    # asset_id, so detaching the job link here (instead of leaving it
    # pointing at a row that's about to not exist) is enough to keep them
    # on the plane's record without a FOREIGN KEY crash.
    conn.execute("UPDATE logbook_entries SET project_id = NULL WHERE project_id = ?", (project_id,))
    conn.execute("UPDATE found_items SET project_id = NULL WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM project_sections WHERE project_id = ?", (project_id,))
    if hours_action == "transfer":
        row = conn.execute("SELECT COALESCE(SUM(hours), 0) t FROM labor_sessions WHERE project_id = ? AND hours IS NOT NULL",
                           (project_id,)).fetchone()
        summary["hours_transferred"] = row["t"] or 0.0
        conn.execute("UPDATE labor_sessions SET project_id = NULL, section = NULL WHERE project_id = ?", (project_id,))
    else:
        row = conn.execute("SELECT COALESCE(SUM(hours), 0) t FROM labor_sessions WHERE project_id = ? AND hours IS NOT NULL",
                           (project_id,)).fetchone()
        summary["hours_removed"] = row["t"] or 0.0
        conn.execute("DELETE FROM labor_sessions WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return summary


def _purge_summary_clause(summary):
    """'3 parts returned to stock; 4.0 hours moved to General Shop time' -
    or '' if this purge didn't touch any parts/hours (nothing to report)."""
    parts = []
    if summary["parts_returned"]:
        parts.append(f"{summary['parts_returned']:g} parts returned to stock")
    if summary["hours_transferred"]:
        parts.append(f"{summary['hours_transferred']:g} hours moved to General Shop time")
    if summary["hours_removed"]:
        parts.append(f"{summary['hours_removed']:g} hours removed from pay")
    return "; ".join(parts)


def _purge_summary_message(name, summary):
    clause = _purge_summary_clause(summary)
    return f"Job '{name}' deleted. {clause}." if clause else f"Project '{name}' permanently deleted. Its logbook entries and owner-approved items were kept on the plane's record."


@app.route("/projects/<int:project_id>/purge", methods=["POST"])
@shop_role_required('admin')
def project_purge(project_id):
    conn = get_db()
    project = conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    preview = _project_purge_preview(conn, project_id)
    parts_action = request.form.get("parts_action")
    hours_action = request.form.get("hours_action")
    if preview["parts"] and parts_action not in ("return", "deleted"):
        conn.close()
        flash("Choose what happens to the parts taken for this job before deleting it.", "danger")
        return redirect(url_for("trash_page"))
    if preview["hours"] and hours_action not in ("remove", "transfer"):
        conn.close()
        flash("Choose what happens to the hours clocked on this job before deleting it.", "danger")
        return redirect(url_for("trash_page"))
    summary = _purge_project(conn, project_id, parts_action=parts_action, hours_action=hours_action)
    conn.commit()
    conn.close()
    flash(_purge_summary_message(project["name"], summary), "success")
    return redirect(url_for("trash_page"))


@app.route("/projects/<int:project_id>/checklist")
@login_required
def project_checklist_print(project_id):
    """Printable job sheet: the prework checklist and standard-items-performed
    list, as checkboxes, in a professional format ready to print and clip to
    the job or hand to the customer."""
    conn = get_db()
    project = conn.execute("""SELECT projects.*, a.tag as asset_tag, a.name as asset_name
                               FROM projects LEFT JOIN assets a ON a.id = projects.asset_id
                               WHERE projects.id = ?""", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    # Scan codes printed across the top of the sheet: the plane (a link to
    # its aircraft page), the project code (selects the job on the Scan
    # page), and one TASK- code per sub-area (selects job + sub-area, same
    # codes as the Labor Codes page).
    sub_areas = [r["name"] for r in conn.execute(
        "SELECT name FROM project_sections WHERE project_id = ? ORDER BY id", (project_id,)).fetchall()]
    conn.close()
    plane_url = (url_for("asset_detail", asset_id=project["asset_id"], _external=True)
                 if project["asset_id"] else None)
    prework_items = [ln.strip() for ln in (project["prework_checklist"] or "").splitlines() if ln.strip()]
    standard_items = [ln.strip() for ln in (project["standard_items"] or "").splitlines() if ln.strip()]
    return render_template("project_checklist.html", project=project,
                           prework_items=prework_items, standard_items=standard_items,
                           sub_areas=sub_areas, plane_url=plane_url)


@app.route("/projects/<int:project_id>/label")
@shop_role_required('admin', 'tech')
def project_label(project_id):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    conn.close()
    if not project:
        abort(404)
    return render_template("project_label.html", project=project)


@app.route("/projects/<int:project_id>/print-label", methods=["POST"])
@shop_role_required('admin', 'tech')
def project_print_label(project_id):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    asset = None
    if project and project["asset_id"]:
        asset = conn.execute("SELECT tag FROM assets WHERE id = ?", (project["asset_id"],)).fetchone()
    conn.close()
    if not project:
        abort(404)
    try:
        from label_printer import print_project_label
        print_project_label(project, asset["tag"] if asset else None)
        flash(f"Label sent to printer for {project['name']}.", "success")
    except Exception as e:
        flash(f"Couldn't print label: {e}", "danger")
    return redirect(url_for("project_label", project_id=project_id))


def _project_section_or_404(conn, project_id, section_id):
    row = conn.execute("""SELECT ps.id, ps.name, p.id AS project_id, p.code, p.name AS project_name, a.tag AS asset_tag
                          FROM project_sections ps JOIN projects p ON p.id = ps.project_id
                          LEFT JOIN assets a ON a.id = p.asset_id
                          WHERE ps.id = ? AND ps.project_id = ?""", (section_id, project_id)).fetchone()
    if not row:
        conn.close()
        abort(404)
    return row


@app.route("/projects/<int:project_id>/sections/<int:section_id>/label")
@shop_role_required('admin', 'tech', 'inspector')
def project_section_label(project_id, section_id):
    """One sub area's QR code (e.g. "Plugs"), ready to print and stick on
    the job. It's made from the project code + sub-area name (TASK-26-001::Plugs,
    the same code the Labor Codes page and Job Sheet use), so every sub
    area has one automatically - scanning it selects that job and area."""
    conn = get_db()
    section = _project_section_or_404(conn, project_id, section_id)
    conn.close()
    return render_template("project_section_label.html", section=section,
                           code=f"TASK-{section['code']}::{section['name']}")


@app.route("/projects/<int:project_id>/sections/<int:section_id>/print-label", methods=["POST"])
@shop_role_required('admin', 'tech', 'inspector')
def project_section_print_label(project_id, section_id):
    conn = get_db()
    section = _project_section_or_404(conn, project_id, section_id)
    conn.close()
    try:
        from label_printer import print_task_label
        print_task_label(section["project_name"], f"{section['code']} · {section['name']}",
                         f"TASK-{section['code']}::{section['name']}")
        flash(f"Label sent to printer for {section['name']}.", "success")
    except Exception as e:
        flash(f"Couldn't print label: {e}", "danger")
    return redirect(url_for("project_section_label", project_id=project_id, section_id=section_id))


@app.route("/projects/<int:project_id>/labor_codes")
@login_required
def project_labor_codes(project_id):
    """Printable QR codes for this project's labor tracking: one for the
    project as a whole ("General") and one per area/sub-system that's been
    used on it, so a laborer can scan the specific task they're working."""
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    sections = conn.execute("""
        SELECT section FROM (
            SELECT name as section FROM project_sections WHERE project_id = ?
            UNION
            SELECT section FROM transactions
                WHERE project_id = ? AND section IS NOT NULL AND TRIM(section) != ''
        )
        ORDER BY section
    """, (project_id, project_id)).fetchall()
    conn.close()
    section_names = [r["section"] for r in sections]
    return render_template("project_labor_codes.html", project=project, sections=section_names)


# ---------------------------------------------------------------------------
# Assets (planes / recurring equipment) - profiles that outlive any one
# project/year, so plane-specific data and history can live in one place.
# ---------------------------------------------------------------------------

@app.route("/assets")
@shop_role_required('admin', 'tech')
def assets_list():
    conn = get_db()
    q = request.args.get("q", "").strip()
    # A flight-school simulator (assets.is_simulator) isn't a real aircraft
    # for the shop to track - it stays in Flight School's own Planes list.
    query = "SELECT * FROM assets WHERE deleted_at IS NULL AND is_simulator = 0 AND is_owner_placeholder = 0"
    params = []
    if q:
        query += " AND (tag LIKE ? OR name LIKE ? OR make LIKE ? OR model LIKE ? OR owner LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like, like, like]
    query += " ORDER BY tag"
    assets = conn.execute(query, params).fetchall()
    project_counts = {}
    asset_covers = {}
    # Hours left to each aircraft's next 100-hour inspection, reusing the
    # same flight.hundred_hr_status() the Flight School side shows on its
    # Planes list - None for an asset with no active hours-based 100-Hour
    # Inspection item set up (or no tach reading yet).
    hundred = {}
    for a in assets:
        row = conn.execute("SELECT COUNT(*) c FROM projects WHERE asset_id = ?", (a["id"],)).fetchone()
        project_counts[a["id"]] = row["c"]
        photo = conn.execute(
            "SELECT filename FROM photos WHERE asset_id = ? ORDER BY is_cover DESC, created_at DESC LIMIT 1",
            (a["id"],)).fetchone()
        if photo:
            asset_covers[a["id"]] = photo["filename"]
        else:
            fallback = _asset_project_cover(conn, a["id"])
            if fallback:
                asset_covers[a["id"]] = fallback["filename"]
        hundred[a["id"]] = hundred_hr_status(conn, a)
    conn.close()
    return render_template("assets.html", assets=assets, project_counts=project_counts, q=q,
                           asset_covers=asset_covers, hundred=hundred)


def _asset_project_cover(conn, asset_id):
    """Fallback picture for an aircraft/asset with no photo of its own: the
    cover (or newest) photo from its newest project. Display-only - nothing
    is copied, so the aircraft picks up its own photo as soon as one is
    added and the project's photos stay the project's."""
    row = conn.execute("""SELECT ph.filename, p.id AS project_id, p.name AS project_name
                          FROM photos ph JOIN projects p ON p.id = ph.project_id
                          WHERE p.asset_id = ? AND p.deleted_at IS NULL
                          ORDER BY p.created_at DESC, ph.is_cover DESC, ph.created_at DESC
                          LIMIT 1""", (asset_id,)).fetchone()
    return dict(row) if row else None

@app.route("/assets/new", methods=["GET", "POST"])
@shop_role_required('admin')
def asset_new():
    if request.method == "POST":
        tag = request.form.get("tag", "").strip()
        if not tag:
            flash("Tail / serial number is required.", "danger")
            return render_template("asset_form.html", asset=None)
        conn = get_db()
        existing = conn.execute("SELECT id FROM assets WHERE tag = ?", (tag,)).fetchone()
        if existing:
            flash(f"An asset with tag '{tag}' already exists.", "danger")
            conn.close()
            return render_template("asset_form.html", asset=None)
        hobbs_hours = _parse_float(request.form.get("hobbs_hours"))
        tach_hours = _parse_float(request.form.get("tach_hours"))
        is_flight_asset = 1 if request.form.get("is_flight_asset") else 0
        show_on_map = 1 if request.form.get("show_on_map") else 0
        cur = conn.execute("""INSERT INTO assets (tag, name, make, model, serial_number, year, owner,
                               hobbs_hours, hobbs_updated_at, tach_hours, tach_updated_at,
                               engine_make, engine_model, engine_serial, prop_make, prop_model, prop_serial,
                               rental_rate, is_flight_asset, icao24_hex, show_on_map, notes,
                               maint_oil_type, maint_tire_nose, maint_tire_mains, maint_other,
                               created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (tag, request.form.get("name", "").strip() or tag,
                             request.form.get("make", "").strip(), request.form.get("model", "").strip(),
                             request.form.get("serial_number", "").strip(), request.form.get("year", "").strip(),
                             request.form.get("owner", "").strip(), hobbs_hours,
                             now_iso() if hobbs_hours is not None else None, tach_hours,
                             now_iso() if tach_hours is not None else None,
                             request.form.get("engine_make", "").strip(), request.form.get("engine_model", "").strip(),
                             request.form.get("engine_serial", "").strip(), request.form.get("prop_make", "").strip(),
                             request.form.get("prop_model", "").strip(), request.form.get("prop_serial", "").strip(),
                             _parse_float(request.form.get("rental_rate")), is_flight_asset,
                             request.form.get("icao24_hex", "").strip().upper() or None, show_on_map,
                             request.form.get("notes", "").strip(),
                             request.form.get("maint_oil_type", "").strip() or None,
                             request.form.get("maint_tire_nose", "").strip() or None,
                             request.form.get("maint_tire_mains", "").strip() or None,
                             request.form.get("maint_other", "").strip() or None,
                             now_iso(), now_iso()))
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
        flash(f"Aircraft '{tag}' created.", "success")
        return redirect(url_for("asset_detail", asset_id=new_id))
    return render_template("asset_form.html", asset=None)


@app.route("/assets/quick_new", methods=["POST"])
@shop_role_required('admin', 'tech')
def asset_quick_new():
    """Minimal aircraft profile started right from the plane dropdown (see
    the "+ Add New" option in project_form.html) - just a tail number, so
    a project can be linked to a plane that isn't in the system yet without
    leaving the page. Flagged profile_incomplete until someone opens Edit
    Profile and saves the rest (see asset_edit, which clears the flag)."""
    tag = (request.form.get("tag") or "").strip()
    if not tag:
        return jsonify({"error": "Enter a tail / serial number."}), 400
    conn = get_db()
    existing = conn.execute("SELECT id FROM assets WHERE tag = ?", (tag,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": f"An asset with tag '{tag}' already exists."}), 400
    cur = conn.execute("""INSERT INTO assets (tag, name, profile_incomplete, created_at, updated_at)
                          VALUES (?, ?, 1, ?, ?)""", (tag, tag, now_iso(), now_iso()))
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"id": new_id, "tag": tag})


@app.route("/assets/<int:asset_id>")
@shop_role_required('admin', 'tech', 'inspector')
def asset_detail(asset_id):
    """Profile page for one plane/asset: its saved data plus combined history
    across every project ever tagged to it (each year gets its own project
    number, but this page pulls them all together)."""
    conn = get_db()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    projects = conn.execute(
        "SELECT * FROM projects WHERE asset_id = ? ORDER BY created_at DESC", (asset_id,)
    ).fetchall()
    project_blocks = []
    total_cost = 0.0
    for p in projects:
        usage = conn.execute("""
            SELECT pt.name, pt.unit, pt.unit_cost,
                   SUM(CASE WHEN t.type='out' THEN t.qty ELSE -t.qty END) as qty_used
            FROM transactions t JOIN parts pt ON pt.id = t.part_id
            WHERE t.project_id = ?
            GROUP BY pt.id
            HAVING qty_used > 0
            ORDER BY pt.name
        """, (p["id"],)).fetchall()
        cost = sum((u["qty_used"] or 0) * (u["unit_cost"] or 0) for u in usage)
        total_cost += cost
        project_blocks.append({"project": p, "usage": usage, "cost": cost})

    maint_rows = conn.execute(
        "SELECT * FROM maintenance_items WHERE asset_id = ? AND active = 1 ORDER BY name", (asset_id,)
    ).fetchall()
    maintenance_items = [{"item": m, "status": maintenance_status(m, asset_meter(asset, m["hour_type"]))}
                          for m in maint_rows]

    # Flight School: this plane's oil-added history, otherwise invisible
    # from the Maintenance side.
    asset_flights = []
    oil_log = []
    total_oil_added = 0.0
    if asset["is_flight_asset"]:
        asset_flights = conn.execute("""
            SELECT f.*, s.name as student_name, c.name as cfi_name
            FROM flights f
            JOIN students s ON s.id = f.student_id
            LEFT JOIN cfis c ON c.id = f.cfi_id
            WHERE f.asset_id = ?
            ORDER BY f.flight_date DESC, f.id DESC
        """, (asset_id,)).fetchall()
        oil_log = [f for f in asset_flights if f["oil_added_qt"]]
        total_oil_added = sum(f["oil_added_qt"] or 0 for f in asset_flights)

    # Open squawks against this plane - flagged during a logged flight, or
    # reported directly (see asset_squawk_new()). A quick squawk can exist
    # for any asset, not just a Flight School plane.
    open_squawks = conn.execute(f"""
        SELECT {_FLIGHT_SQUAWK_COLS}
        FROM flights f
        JOIN assets a ON a.id = f.asset_id
        JOIN students s ON s.id = f.student_id
        LEFT JOIN cfis c ON c.id = f.cfi_id
        LEFT JOIN users au ON au.id = f.squawk_assigned_to
        WHERE f.squawk = 1 AND f.squawk_acknowledged_at IS NULL AND a.id = ?
        UNION ALL
        SELECT {_QUICK_SQUAWK_COLS}
        FROM plane_squawks q
        JOIN assets a ON a.id = q.asset_id
        LEFT JOIN users au ON au.id = q.assigned_to
        WHERE q.acknowledged_at IS NULL AND a.id = ?
        ORDER BY event_date DESC, squawk_id DESC
    """, (asset_id, asset_id)).fetchall()

    # Squawks for the plane's To-Do list specifically: unlike open_squawks
    # above (the "Needs Acknowledgement" banner - unacknowledged only),
    # this also keeps an already-acknowledged squawk on the to-do list
    # (shown amber there) until it's actually repaired, since acknowledging
    # it isn't the same as it being done.
    todo_squawks = get_plane_open_squawks(conn, asset_id)
    done_squawks = get_plane_done_squawks(conn, asset_id)

    # Cylinder compression checks - logged from the Maintenance side (any
    # asset, not just Flight School planes), so it belongs here regardless
    # of is_flight_asset.
    latest_compression = conn.execute(
        "SELECT * FROM compression_checks WHERE asset_id = ? ORDER BY checked_date DESC, id DESC LIMIT 1",
        (asset_id,)).fetchone()
    compression_count = conn.execute(
        "SELECT COUNT(*) c FROM compression_checks WHERE asset_id = ?", (asset_id,)).fetchone()["c"]

    photos = conn.execute("SELECT * FROM photos WHERE asset_id = ? ORDER BY is_cover DESC, created_at DESC",
                           (asset_id,)).fetchall()
    project_cover = None if photos else _asset_project_cover(conn, asset_id)
    todos = conn.execute(
        """SELECT pt.*, u.name as assigned_to_name FROM plane_todos pt
           LEFT JOIN users u ON u.id = pt.assigned_to
           WHERE pt.asset_id = ? ORDER BY pt.done, pt.created_at DESC""", (asset_id,)
    ).fetchall()
    assignable_workers = get_assignable_workers(conn)
    asset_manuals = manuals_for_asset(conn, asset)
    owner_student = None
    if asset["is_owner_placeholder"] and asset["owner_student_id"]:
        owner_student = conn.execute("SELECT id, name FROM students WHERE id = ?", (asset["owner_student_id"],)).fetchone()
    asset_ads = ads_for_asset(conn, asset)
    conn.close()
    return render_template("asset_detail.html", asset=asset, project_blocks=project_blocks, total_cost=total_cost,
                           asset_ads=asset_ads, ad_kinds=AD_KINDS, ad_methods=AD_METHODS, today_iso=date.today().isoformat(),
                           maintenance_items=maintenance_items, oil_log=oil_log, total_oil_added=total_oil_added,
                           open_squawks=open_squawks, todo_squawks=todo_squawks, done_squawks=done_squawks, photos=photos, todos=todos, project_cover=project_cover,
                           assignable_workers=assignable_workers,
                           latest_compression=latest_compression, compression_count=compression_count,
                           asset_manuals=asset_manuals, owner_student=owner_student)


@app.route("/assets/<int:asset_id>/promote_own_plane", methods=["POST"])
@shop_role_required('admin')
def asset_promote_own_plane(asset_id):
    """Turns a student's own-plane placeholder (see _get_or_create_own_plane_asset
    in flight.py, and the "Student's own plane" toggle on Schedule a Flight)
    into a real Fleet/Maintenance asset - for when the student brings that
    plane to us for maintenance. Needs a real N-number now, since it's about
    to show up in the Fleet like any other plane; not required before this
    (see the toggle - it's optional there)."""
    conn = get_db()
    asset = conn.execute("SELECT id FROM assets WHERE id = ? AND is_owner_placeholder = 1", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        flash("Not a student-owned placeholder plane.", "danger")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    n_number = request.form.get("n_number", "").strip().upper()
    if not n_number:
        conn.close()
        flash("Enter the plane's N-number to add it to the fleet.", "danger")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    conflict = conn.execute("SELECT id FROM assets WHERE tag = ? AND id != ?", (n_number, asset_id)).fetchone()
    if conflict:
        conn.close()
        flash(f"{n_number} is already used by another plane in the fleet.", "danger")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    conn.execute("UPDATE assets SET tag = ?, is_owner_placeholder = 0, updated_at = ? WHERE id = ?",
                 (n_number, now_iso(), asset_id))
    conn.commit()
    conn.close()
    flash(f"{n_number} added to the Fleet and Maintenance.", "success")
    return redirect(url_for("asset_detail", asset_id=asset_id))


@app.route("/assets/<int:asset_id>/squawk", methods=["POST"])
@shop_role_required('admin', 'tech')
def asset_squawk_new(asset_id):
    """Reports an issue straight against a plane, without going through
    Flight School's "log a flight" flow - see plane_squawks in schema.sql."""
    notes = request.form.get("notes", "").strip()
    if not notes:
        flash("Enter what's wrong before reporting.", "danger")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    conn = get_db()
    asset = conn.execute("SELECT id, deleted_at FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    if asset["deleted_at"]:
        conn.close()
        flash("That plane is in the trash.", "danger")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    conn.execute("INSERT INTO plane_squawks (asset_id, notes, reported_by, reported_at) VALUES (?, ?, ?, ?)",
                 (asset_id, notes, session.get("user_name"), now_iso()))
    conn.commit()
    conn.close()
    flash("Issue reported - it'll show up on the Squawks page until it's addressed.", "success")
    return redirect(url_for("asset_detail", asset_id=asset_id))


@app.route("/assets/<int:asset_id>/todo/new", methods=["POST"])
@shop_role_required('admin', 'tech')
def plane_todo_new(asset_id):
    """Adds an item to this plane's to-do list - separate from squawks
    (which come from a flown flight) and from Maintenance items (which are
    recurring/interval-based); this is just a plain running list. Can be
    handed to a specific laborer right away (assigned_to), so it shows up
    on their own My Tasks page - same idea as assigning a squawk."""
    description = request.form.get("description", "").strip()
    assigned_to_raw = request.form.get("assigned_to", "").strip()
    assigned_to = int(assigned_to_raw) if assigned_to_raw.isdigit() else None
    if description:
        conn = get_db()
        asset = conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not asset:
            conn.close()
            abort(404)
        conn.execute(
            "INSERT INTO plane_todos (asset_id, description, created_by, created_at, assigned_to) VALUES (?, ?, ?, ?, ?)",
            (asset_id, description, session.get("user_name"), now_iso(), assigned_to))
        conn.commit()
        conn.close()
        flash("To-do added.", "success")
    return redirect(url_for("asset_detail", asset_id=asset_id))


@app.route("/assets/<int:asset_id>/todo/<int:todo_id>/assign", methods=["POST"])
@shop_role_required('admin', 'tech')
def plane_todo_assign(asset_id, todo_id):
    """Hands (or reassigns/clears) a plane to-do to a laborer - it then
    shows on that person's My Tasks page until it's done."""
    assigned_to_raw = request.form.get("assigned_to", "").strip()
    assigned_to = int(assigned_to_raw) if assigned_to_raw.isdigit() else None
    conn = get_db()
    todo = conn.execute("SELECT id FROM plane_todos WHERE id = ? AND asset_id = ?", (todo_id, asset_id)).fetchone()
    if not todo:
        conn.close()
        abort(404)
    conn.execute("UPDATE plane_todos SET assigned_to = ? WHERE id = ?", (assigned_to, todo_id))
    conn.commit()
    conn.close()
    flash("Assigned." if assigned_to else "Assignment cleared.", "success")
    return redirect(request.referrer or url_for("asset_detail", asset_id=asset_id))


@app.route("/assets/<int:asset_id>/todo/<int:todo_id>/toggle", methods=["POST"])
@shop_role_required('admin', 'tech')
def plane_todo_toggle(asset_id, todo_id):
    """Checking a plane to-do's box no longer completes it outright - it
    requests confirmation instead (confirm_requested_at/by), same
    request/confirm two-step a project sub area or a squawk's repair
    already requires (see project_section_complete and squawk_repair).
    Unchecking a still-open one just cancels the request, back to plain
    open. A to-do already confirmed done can still be reopened here (unlike
    squawks/sections, which only reopen via "Send Back" during the awaiting
    step) - toggling a done row clears done, completed_at and confirmed_by."""
    conn = get_db()
    todo = conn.execute("SELECT * FROM plane_todos WHERE id = ? AND asset_id = ?", (todo_id, asset_id)).fetchone()
    if not todo:
        conn.close()
        abort(404)
    if todo["done"]:
        conn.execute("""UPDATE plane_todos SET done = 0, completed_at = NULL, confirmed_by = NULL,
                         confirm_requested_at = NULL, confirm_requested_by = NULL WHERE id = ?""", (todo_id,))
    elif todo["confirm_requested_at"]:
        conn.execute("UPDATE plane_todos SET confirm_requested_at = NULL, confirm_requested_by = NULL WHERE id = ?",
                     (todo_id,))
    else:
        conn.execute("UPDATE plane_todos SET confirm_requested_at = ?, confirm_requested_by = ? WHERE id = ?",
                     (now_iso(), session.get("user_name"), todo_id))
        flash("Marked ready - an Inspector or admin needs to confirm it.", "success")
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("asset_detail", asset_id=asset_id))


@app.route("/assets/<int:asset_id>/todo/<int:todo_id>/confirm", methods=["POST"])
@shop_role_required('admin', 'inspector')
def plane_todo_confirm(asset_id, todo_id):
    """An Inspector (or admin) signs off on a to-do someone else marked
    ready - this is what actually completes it. 'Send back' cancels the
    request instead, so whoever did the work knows it wasn't approved (same
    pattern as project_section_confirm/squawk_repair_confirm)."""
    conn = get_db()
    todo = conn.execute("SELECT id FROM plane_todos WHERE id = ? AND asset_id = ?", (todo_id, asset_id)).fetchone()
    if not todo:
        conn.close()
        abort(404)
    if request.form.get("action") == "send_back":
        conn.execute("""UPDATE plane_todos SET confirm_requested_at = NULL, confirm_requested_by = NULL,
                         sent_back_at = ?, sent_back_by = ? WHERE id = ?""",
                     (now_iso(), session.get("user_name"), todo_id))
        flash("Sent back - unmarked as done.", "warning")
    else:
        conn.execute("UPDATE plane_todos SET done = 1, completed_at = ?, confirmed_by = ? WHERE id = ?",
                     (now_iso(), session.get("user_name"), todo_id))
        flash("Confirmed complete.", "success")
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("asset_detail", asset_id=asset_id))


@app.route("/assets/<int:asset_id>/todo/<int:todo_id>/delete", methods=["POST"])
@shop_role_required('admin', 'tech')
def plane_todo_delete(asset_id, todo_id):
    conn = get_db()
    conn.execute("DELETE FROM plane_todos WHERE id = ? AND asset_id = ?", (todo_id, asset_id))
    conn.commit()
    conn.close()
    flash("To-do removed.", "success")
    return redirect(url_for("asset_detail", asset_id=asset_id))


@app.route("/my-tasks")
@shop_role_required('admin', 'tech')
def tech_spot():
    """A laborer's own work list: squawks handed to them, plane to-dos
    handed to them, and every fleet maintenance item that's due/overdue
    (not assigned to anyone in particular - any tech can pick one up) - all
    in one spot instead of hunting across Squawks and each plane's page."""
    conn = get_db()
    my_squawks = get_my_squawks(conn, session["user_id"])
    # Open (not yet checked off) and awaiting-confirmation to-dos are both
    # "not done yet", just split so the page can show the pending ones as
    # waiting on an Inspector rather than actionable. Completed shows what
    # this laborer has actually finished (see plane_todo_confirm) - the
    # requested "Completed" section on this page - most recent first.
    my_todos = conn.execute("""
        SELECT pt.*, a.tag as asset_tag FROM plane_todos pt
        JOIN assets a ON a.id = pt.asset_id
        WHERE pt.assigned_to = ? AND pt.done = 0 AND pt.confirm_requested_at IS NULL
        ORDER BY pt.created_at
    """, (session["user_id"],)).fetchall()
    my_todos_pending = conn.execute("""
        SELECT pt.*, a.tag as asset_tag FROM plane_todos pt
        JOIN assets a ON a.id = pt.asset_id
        WHERE pt.assigned_to = ? AND pt.done = 0 AND pt.confirm_requested_at IS NOT NULL
        ORDER BY pt.confirm_requested_at
    """, (session["user_id"],)).fetchall()
    my_todos_done = conn.execute("""
        SELECT pt.*, a.tag as asset_tag FROM plane_todos pt
        JOIN assets a ON a.id = pt.asset_id
        WHERE pt.assigned_to = ? AND pt.done = 1
        ORDER BY pt.completed_at DESC LIMIT 25
    """, (session["user_id"],)).fetchall()
    my_squawks_done = get_my_done_squawks(conn, session["user_id"])
    reminders = _fleet_maintenance_reminders(conn)
    conn.close()
    return render_template("tech_spot.html", my_squawks=my_squawks, my_todos=my_todos,
                           my_todos_pending=my_todos_pending, my_todos_done=my_todos_done,
                           my_squawks_done=my_squawks_done, reminders=reminders)


@app.route("/assets/<int:asset_id>/oil")
@shop_role_required('admin', 'tech')
def asset_oil(asset_id):
    """Oil consumption for one asset, tracked here in Maintenance (not just
    Flight School) - a running chart of quarts added over time, plus hours
    flown per quart burned and quarts added per top-off. Data comes from
    the Oil Added field on logged flights, so it's only populated for
    planes that have flight history."""
    conn = get_db()
    asset = conn.execute("SELECT * FROM assets WHERE id = ? AND deleted_at IS NULL", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    flights = conn.execute(
        "SELECT * FROM flights WHERE asset_id = ? ORDER BY flight_date, id", (asset_id,)
    ).fetchall()
    conn.close()

    total_hours = sum(_flight_hours(f) for f in flights)
    oil_events = [f for f in flights if f["oil_added_qt"]]
    total_oil = sum(f["oil_added_qt"] or 0 for f in oil_events)
    num_events = len(oil_events)
    qts_per_change = (total_oil / num_events) if num_events else None
    hours_per_qt = (total_hours / total_oil) if total_oil else None

    chart_labels, chart_cumulative = [], []
    events_detail = []
    cumulative_qt = 0.0
    hours_cursor = 0.0
    last_event_hours = 0.0
    for f in flights:
        hours_cursor += _flight_hours(f)
        if f["oil_added_qt"]:
            cumulative_qt += f["oil_added_qt"]
            events_detail.append({
                "flight_date": f["flight_date"], "qty": f["oil_added_qt"], "added_by": f["oil_added_by"],
                "hours_since_last": hours_cursor - last_event_hours,
                "cumulative_qt": cumulative_qt,
            })
            chart_labels.append(f["flight_date"])
            chart_cumulative.append(round(cumulative_qt, 2))
            last_event_hours = hours_cursor
    events_detail.reverse()

    return render_template("asset_oil.html", asset=asset, total_hours=total_hours,
                           total_oil=total_oil, num_events=num_events, qts_per_change=qts_per_change,
                           hours_per_qt=hours_per_qt, events_detail=events_detail,
                           chart_labels=chart_labels, chart_cumulative=chart_cumulative)


@app.route("/assets/<int:asset_id>/compression", methods=["GET", "POST"])
@shop_role_required('admin', 'tech')
def asset_compression(asset_id):
    """Cylinder compression checks for one asset, logged here in
    Maintenance - a running trend chart (one line per cylinder) plus a
    table of past readings, with a form to log a new one. Not limited to
    Flight School planes - any asset with an engine can have checks logged
    against it."""
    conn = get_db()
    asset = conn.execute("SELECT * FROM assets WHERE id = ? AND deleted_at IS NULL", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)

    if request.method == "POST":
        checked_date = request.form.get("checked_date", "").strip() or date.today().strftime("%Y-%m-%d")
        cyls = [_parse_float(request.form.get(f"cyl{i}")) for i in range(1, 7)]
        conn.execute("""INSERT INTO compression_checks (asset_id, checked_date, hours, master_orifice,
                         cyl1, cyl2, cyl3, cyl4, cyl5, cyl6, performed_by, notes, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (asset_id, checked_date, _parse_float(request.form.get("hours")),
                      _parse_float(request.form.get("master_orifice")),
                      cyls[0], cyls[1], cyls[2], cyls[3], cyls[4], cyls[5],
                      request.form.get("performed_by", "").strip() or session.get("user_name"),
                      request.form.get("notes", "").strip() or None, now_iso()))
        conn.commit()
        conn.close()
        flash("Compression check logged.", "success")
        return redirect(url_for("asset_compression", asset_id=asset_id))

    checks = conn.execute(
        "SELECT * FROM compression_checks WHERE asset_id = ? ORDER BY checked_date, id", (asset_id,)
    ).fetchall()
    conn.close()

    chart_labels = [c["checked_date"] for c in checks]
    cyl_series = {}
    for i in range(1, 7):
        key = f"cyl{i}"
        values = [c[key] for c in checks]
        if any(v is not None for v in values):
            cyl_series[key] = values

    latest = checks[-1] if checks else None
    history = list(reversed(checks))

    # Pre-select the cylinder count in the log-a-check form from however
    # many the last check actually used (most engines don't change), so
    # the form doesn't reset to a default every time.
    default_num_cylinders = 4
    if latest:
        used = max((i for i in range(1, 7) if latest[f"cyl{i}"] is not None), default=None)
        if used:
            default_num_cylinders = used

    return render_template("asset_compression.html", asset=asset, checks=history, latest=latest,
                           chart_labels=chart_labels, cyl_series=cyl_series, default_num_cylinders=default_num_cylinders,
                           today=date.today().strftime("%Y-%m-%d"))


@app.route("/assets/<int:asset_id>/edit", methods=["GET", "POST"])
@shop_role_required('admin')
def asset_edit(asset_id):
    conn = get_db()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    if request.method == "POST":
        tag = request.form.get("tag", "").strip()
        if not tag:
            flash("Tail / serial number is required.", "danger")
            conn.close()
            return render_template("asset_form.html", asset=asset)
        clash = conn.execute("SELECT id FROM assets WHERE tag = ? AND id != ?", (tag, asset_id)).fetchone()
        if clash:
            flash(f"Another asset already uses tag '{tag}'.", "danger")
            conn.close()
            return render_template("asset_form.html", asset=asset)
        is_flight_asset = 1 if request.form.get("is_flight_asset") else 0
        show_on_map = 1 if request.form.get("show_on_map") else 0
        # rental_rate isn't on this form anymore (set from Flight School by
        # an admin instead), so this update deliberately leaves it alone.
        # Same for schedule_color/solo_color/solo_allowed, which all live on
        # Flight School > Planes > Edit now (Idea "under edit aircraft in Fly
        # With Kate!" - combining them here too just split them across pages).
        conn.execute("""UPDATE assets SET tag=?, name=?, make=?, model=?, serial_number=?, year=?, owner=?,
                         engine_make=?, engine_model=?, engine_serial=?, prop_make=?, prop_model=?, prop_serial=?,
                         is_flight_asset=?, icao24_hex=?, show_on_map=?, notes=?,
                         maint_oil_type=?, maint_tire_nose=?, maint_tire_mains=?, maint_other=?,
                         profile_incomplete=0, updated_at=? WHERE id=?""",
                     (tag, request.form.get("name", "").strip() or tag, request.form.get("make", "").strip(),
                      request.form.get("model", "").strip(), request.form.get("serial_number", "").strip(),
                      request.form.get("year", "").strip(), request.form.get("owner", "").strip(),
                      request.form.get("engine_make", "").strip(), request.form.get("engine_model", "").strip(),
                      request.form.get("engine_serial", "").strip(), request.form.get("prop_make", "").strip(),
                      request.form.get("prop_model", "").strip(), request.form.get("prop_serial", "").strip(),
                      is_flight_asset, request.form.get("icao24_hex", "").strip().upper() or None, show_on_map,
                      request.form.get("notes", "").strip(),
                      request.form.get("maint_oil_type", "").strip() or None,
                      request.form.get("maint_tire_nose", "").strip() or None,
                      request.form.get("maint_tire_mains", "").strip() or None,
                      request.form.get("maint_other", "").strip() or None,
                      now_iso(), asset_id))
        conn.commit()
        conn.close()
        flash("Aircraft updated.", "success")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    conn.close()
    return render_template("asset_form.html", asset=asset)


@app.route("/assets/<int:asset_id>/update_hours", methods=["POST"])
@shop_role_required('admin', 'tech')
def asset_update_hours(asset_id):
    conn = get_db()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    hobbs = _parse_float(request.form.get("hobbs_hours"))
    tach = _parse_float(request.form.get("tach_hours"))
    if hobbs is None and tach is None:
        flash("Enter a Hobbs and/or Tach reading.", "danger")
        conn.close()
        return redirect(url_for("asset_detail", asset_id=asset_id))
    for reading, label in ((hobbs, "Hobbs"), (tach, "Tach")):
        if reading is not None and not (math.isfinite(reading) and reading >= 0):
            flash(f"Enter a valid {label} reading.", "danger")
            conn.close()
            return redirect(url_for("asset_detail", asset_id=asset_id))
    updated_by = f"Shop - {session.get('user_name')}"
    updates = []
    if hobbs is not None:
        conn.execute("UPDATE assets SET hobbs_hours = ?, hobbs_updated_at = ?, hobbs_updated_by = ?, updated_at = ? WHERE id = ?",
                     (hobbs, now_iso(), updated_by, now_iso(), asset_id))
        updates.append(f"Hobbs {hobbs:g}")
    if tach is not None:
        conn.execute("UPDATE assets SET tach_hours = ?, tach_updated_at = ?, tach_updated_by = ?, updated_at = ? WHERE id = ?",
                     (tach, now_iso(), updated_by, now_iso(), asset_id))
        updates.append(f"Tach {tach:g}")
    conn.commit()
    conn.close()
    flash(f"Reading updated: {', '.join(updates)}.", "success")
    return redirect(url_for("asset_detail", asset_id=asset_id))


@app.route("/assets/<int:asset_id>/trash", methods=["POST"])
@shop_role_required('admin')
def asset_trash(asset_id):
    """Soft-deletes an asset: hidden everywhere except Recently Deleted."""
    conn = get_db()
    asset = conn.execute("SELECT tag FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    conn.execute("UPDATE assets SET deleted_at = ? WHERE id = ?", (now_iso(), asset_id))
    conn.commit()
    conn.close()
    flash(f"Aircraft '{asset['tag']}' moved to Recently Deleted.", "success")
    return redirect(url_for("assets_list"))


@app.route("/assets/<int:asset_id>/restore", methods=["POST"])
@shop_role_required('admin')
def asset_restore(asset_id):
    conn = get_db()
    asset = conn.execute("SELECT tag FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    conn.execute("UPDATE assets SET deleted_at = NULL WHERE id = ?", (asset_id,))
    conn.commit()
    conn.close()
    flash(f"Aircraft '{asset['tag']}' restored.", "success")
    return redirect(url_for("trash_page"))


def _purge_asset(conn, asset_id):
    item_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM maintenance_items WHERE asset_id = ?", (asset_id,)).fetchall()]
    for item_id in item_ids:
        conn.execute("DELETE FROM maintenance_log WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM maintenance_items WHERE asset_id = ?", (asset_id,))
    conn.execute("UPDATE projects SET asset_id = NULL WHERE asset_id = ?", (asset_id,))
    # Same reasoning as _purge_project (QA fix qa-project-purge-crash):
    # these reference this asset too, so detach rather than leave a
    # dangling reference that would FOREIGN KEY-crash the delete.
    conn.execute("UPDATE logbook_entries SET asset_id = NULL WHERE asset_id = ?", (asset_id,))
    conn.execute("UPDATE found_items SET asset_id = NULL WHERE asset_id = ?", (asset_id,))
    conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))


@app.route("/assets/<int:asset_id>/purge", methods=["POST"])
@shop_role_required('admin')
def asset_purge(asset_id):
    conn = get_db()
    asset = conn.execute("SELECT tag FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    _purge_asset(conn, asset_id)
    conn.commit()
    conn.close()
    flash(f"Aircraft '{asset['tag']}' permanently deleted. Any linked projects were kept, just unlinked.", "success")
    return redirect(url_for("trash_page"))


# ---------------------------------------------------------------------------
# Recently Deleted (soft-deleted projects and assets)
# ---------------------------------------------------------------------------

@app.route("/trash")
@shop_role_required('admin')
def trash_page():
    conn = get_db()
    deleted_projects = conn.execute(
        "SELECT * FROM projects WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
    ).fetchall()
    deleted_assets = conn.execute(
        "SELECT * FROM assets WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
    ).fetchall()
    # QA fix qa-project-purge-erases-pay: what Delete Forever would do to
    # each job's parts/hours, keyed by project id for the page's own JS to
    # show a "what happens to these?" pop-up instead of the plain confirm
    # when a job actually has something tied to it.
    purge_previews = {p["id"]: _project_purge_preview(conn, p["id"]) for p in deleted_projects}
    any_parts_in_trash = any(p["parts"] for p in purge_previews.values())
    any_hours_in_trash = any(p["hours"] for p in purge_previews.values())
    conn.close()
    return render_template("trash.html", deleted_projects=deleted_projects, deleted_assets=deleted_assets,
                           purge_previews=purge_previews, any_parts_in_trash=any_parts_in_trash,
                           any_hours_in_trash=any_hours_in_trash)


@app.route("/trash/empty", methods=["POST"])
@shop_role_required('admin')
def trash_empty():
    conn = get_db()
    project_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM projects WHERE deleted_at IS NOT NULL").fetchall()]
    # Same "what happens to parts/hours" questions as a single Delete
    # Forever, asked once and applied to every job in the bin (QA fix
    # qa-project-purge-erases-pay) - only required when at least one of
    # them actually has parts or hours tied to it.
    previews = [_project_purge_preview(conn, pid) for pid in project_ids]
    any_parts = any(p["parts"] for p in previews)
    any_hours = any(p["hours"] for p in previews)
    parts_action = request.form.get("parts_action")
    hours_action = request.form.get("hours_action")
    if any_parts and parts_action not in ("return", "deleted"):
        conn.close()
        flash("Choose what happens to the parts taken for jobs in Recently Deleted before emptying it.", "danger")
        return redirect(url_for("trash_page"))
    if any_hours and hours_action not in ("remove", "transfer"):
        conn.close()
        flash("Choose what happens to the hours clocked on jobs in Recently Deleted before emptying it.", "danger")
        return redirect(url_for("trash_page"))
    totals = {"parts_returned": 0, "hours_transferred": 0.0, "hours_removed": 0.0}
    for pid in project_ids:
        s = _purge_project(conn, pid, parts_action=parts_action, hours_action=hours_action)
        for k in totals:
            totals[k] += s[k]
    asset_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE deleted_at IS NOT NULL").fetchall()]
    for aid in asset_ids:
        _purge_asset(conn, aid)
    conn.commit()
    conn.close()
    clause = _purge_summary_clause(totals)
    flash(f"Recently Deleted emptied. {clause}." if clause else "Recently Deleted emptied.", "success")
    return redirect(url_for("trash_page"))


# ---------------------------------------------------------------------------
# Maintenance items (hours- or calendar-based, per asset)
# ---------------------------------------------------------------------------

def _parse_float(val):
    val = (val or "").strip()
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _parse_rate(val):
    """Hourly pay rate from a form: blank means 0, otherwise a real number
    that's 0 or more. Returns None for anything else (-25, nan, inf, junk)
    so the form can be shown again - a negative rate would subtract from
    pay and labor bills, and nan/inf break every total it touches."""
    val = (val or "").strip()
    if not val:
        return 0.0
    rate = _parse_float(val)
    if rate is None or not math.isfinite(rate) or rate < 0:
        return None
    return rate


def _parse_int(val):
    val = (val or "").strip()
    if not val:
        return None
    try:
        return int(float(val))
    except ValueError:
        return None


@app.route("/assets/<int:asset_id>/maintenance/new", methods=["GET", "POST"])
@shop_role_required('admin')
def maintenance_new(asset_id):
    conn = get_db()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        mtype = request.form.get("type", "hours")
        if mtype not in ("hours", "calendar"):
            mtype = "hours"
        category = request.form.get("category", "scheduled_maint")
        if category not in ("annual", "100hour", "oil_change", "scheduled_maint"):
            category = "scheduled_maint"
        if not name:
            flash("Item name is required.", "danger")
            conn.close()
            return render_template("maintenance_form.html", asset=asset, item=None)
        hour_type = request.form.get("hour_type", "tach")
        if hour_type not in ("tach", "hobbs"):
            hour_type = "tach"
        interval_hours = _parse_float(request.form.get("interval_hours"))
        interval_days = _parse_int(request.form.get("interval_days"))
        last_done_hours = _parse_float(request.form.get("last_done_hours"))
        if mtype == "hours" and last_done_hours is None:
            last_done_hours = asset_meter(asset, hour_type)
        last_done_date = request.form.get("last_done_date", "").strip()
        if mtype == "calendar" and not last_done_date:
            last_done_date = now_iso()[:10]
        if mtype == "hours":
            remind_lead = _parse_float(request.form.get("remind_lead_hours"))
        else:
            remind_lead = _parse_float(request.form.get("remind_lead_days"))
        conn.execute("""INSERT INTO maintenance_items (asset_id, name, type, category, hour_type, interval_hours,
                         interval_days, last_done_hours, last_done_date, remind_lead, checklist, reference_info, notes,
                         active, created_at, updated_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                     (asset_id, name, mtype, category, hour_type, interval_hours, interval_days, last_done_hours,
                      last_done_date or None, remind_lead, request.form.get("checklist", "").strip(),
                      request.form.get("reference_info", "").strip(), request.form.get("notes", "").strip(),
                      now_iso(), now_iso()))
        conn.commit()
        conn.close()
        flash(f"Maintenance item '{name}' added.", "success")
        return redirect(url_for("asset_detail", asset_id=asset_id))
    conn.close()
    return render_template("maintenance_form.html", asset=asset, item=None)


@app.route("/maintenance/<int:item_id>/edit", methods=["GET", "POST"])
@shop_role_required('admin')
def maintenance_edit(item_id):
    conn = get_db()
    item = conn.execute("SELECT * FROM maintenance_items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        conn.close()
        abort(404)
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (item["asset_id"],)).fetchone()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        mtype = request.form.get("type", "hours")
        if mtype not in ("hours", "calendar"):
            mtype = "hours"
        category = request.form.get("category", "scheduled_maint")
        if category not in ("annual", "100hour", "oil_change", "scheduled_maint"):
            category = "scheduled_maint"
        if not name:
            flash("Item name is required.", "danger")
            conn.close()
            return render_template("maintenance_form.html", asset=asset, item=item)
        hour_type = request.form.get("hour_type", "tach")
        if hour_type not in ("tach", "hobbs"):
            hour_type = "tach"
        interval_hours = _parse_float(request.form.get("interval_hours"))
        interval_days = _parse_int(request.form.get("interval_days"))
        if mtype == "hours":
            remind_lead = _parse_float(request.form.get("remind_lead_hours"))
        else:
            remind_lead = _parse_float(request.form.get("remind_lead_days"))
        conn.execute("""UPDATE maintenance_items SET name=?, type=?, category=?, hour_type=?, interval_hours=?,
                         interval_days=?, remind_lead=?, checklist=?, reference_info=?, notes=?, updated_at=? WHERE id=?""",
                     (name, mtype, category, hour_type, interval_hours, interval_days, remind_lead,
                      request.form.get("checklist", "").strip(), request.form.get("reference_info", "").strip(),
                      request.form.get("notes", "").strip(), now_iso(), item_id))
        conn.commit()
        conn.close()
        flash("Maintenance item updated.", "success")
        return redirect(url_for("asset_detail", asset_id=item["asset_id"]))
    conn.close()
    return render_template("maintenance_form.html", asset=asset, item=item)


@app.route("/maintenance/<int:item_id>/complete", methods=["POST"])
@shop_role_required('admin', 'tech')
def maintenance_complete(item_id):
    conn = get_db()
    item = conn.execute("SELECT * FROM maintenance_items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        conn.close()
        abort(404)
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (item["asset_id"],)).fetchone()
    performed_by = request.form.get("performed_by", "").strip()
    if not performed_by:
        flash("Select who completed this maintenance first.", "danger")
        conn.close()
        return redirect(url_for("asset_detail", asset_id=item["asset_id"]))
    note = request.form.get("note", "").strip()
    completed_hours = None
    if item["type"] == "hours":
        current_reading = asset_meter(asset, item["hour_type"])
        completed_hours = _parse_float(request.form.get("completed_hours"))
        if completed_hours is None:
            completed_hours = current_reading
        conn.execute("UPDATE maintenance_items SET last_done_hours=?, updated_at=? WHERE id=?",
                     (completed_hours, now_iso(), item_id))
        if completed_hours is not None and (current_reading is None or completed_hours > current_reading):
            meter_col = "tach_hours" if item["hour_type"] != "hobbs" else "hobbs_hours"
            updated_col = "tach_updated_at" if item["hour_type"] != "hobbs" else "hobbs_updated_at"
            conn.execute(f"UPDATE assets SET {meter_col}=?, {updated_col}=?, updated_at=? WHERE id=?",
                         (completed_hours, now_iso(), now_iso(), asset["id"]))
    else:
        conn.execute("UPDATE maintenance_items SET last_done_date=?, updated_at=? WHERE id=?",
                     (now_iso()[:10], now_iso(), item_id))
    conn.execute("""INSERT INTO maintenance_log (item_id, completed_at, completed_hours, performed_by, note)
                     VALUES (?, ?, ?, ?, ?)""", (item_id, now_iso(), completed_hours, performed_by, note))
    conn.commit()
    conn.close()
    flash(f"'{item['name']}' marked complete.", "success")
    return redirect(url_for("asset_detail", asset_id=item["asset_id"]))


@app.route("/maintenance/<int:item_id>/delete", methods=["POST"])
@shop_role_required('admin')
def maintenance_delete(item_id):
    conn = get_db()
    item = conn.execute("SELECT * FROM maintenance_items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        conn.close()
        abort(404)
    asset_id = item["asset_id"]
    conn.execute("UPDATE maintenance_items SET active=0, updated_at=? WHERE id=?", (now_iso(), item_id))
    conn.commit()
    conn.close()
    flash(f"'{item['name']}' removed.", "success")
    return redirect(url_for("asset_detail", asset_id=asset_id))


# ---------------------------------------------------------------------------
# Photos (parts, projects, and assets)
# ---------------------------------------------------------------------------

def _add_photos(conn, files, column, owner_id):
    """Saves uploaded image files as photos for a part/project/asset and
    inserts one photos row per file. Returns (saved_count, error) - if
    writing a file to disk fails (e.g. the Pi is out of storage), nothing
    from this batch is committed and error is a message to flash instead
    of letting the OSError crash the request with a raw error page."""
    saved = 0
    try:
        for f in files:
            if f and f.filename and allowed_image(f.filename):
                stored_name = save_upload(f)
                conn.execute(f"INSERT INTO photos ({column}, filename, created_at) VALUES (?, ?, ?)",
                             (owner_id, stored_name, now_iso()))
                saved += 1
        conn.commit()
    except OSError:
        conn.rollback()
        app.logger.exception("Photo upload failed for %s %s", column, owner_id)
        return None, "Couldn't save that photo - the Pi may be low on storage. Check Admin -> System for details."
    return saved, None


@app.route("/assets/<int:asset_id>/photos", methods=["POST"])
@shop_role_required('admin', 'tech')
def asset_add_photos(asset_id):
    conn = get_db()
    asset = conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    files = request.files.getlist("photos")
    saved, error = _add_photos(conn, files, "asset_id", asset_id)
    conn.close()
    if error:
        flash(error, "danger")
    elif saved:
        flash(f"Added {saved} photo(s).", "success")
    else:
        flash("No valid image files were selected.", "danger")
    return redirect(url_for("asset_detail", asset_id=asset_id))


@app.route("/parts/<int:part_id>/photos", methods=["POST"])
@shop_role_required('admin', 'tech')
def part_add_photos(part_id):
    conn = get_db()
    part = conn.execute("SELECT id FROM parts WHERE id = ?", (part_id,)).fetchone()
    if not part:
        conn.close()
        abort(404)
    files = request.files.getlist("photos")
    saved, error = _add_photos(conn, files, "part_id", part_id)
    conn.close()
    if error:
        flash(error, "danger")
    elif saved:
        flash(f"Added {saved} photo(s).", "success")
    else:
        flash("No valid image files were selected.", "danger")
    return redirect(url_for("part_detail", part_id=part_id))


@app.route("/projects/<int:project_id>/photos", methods=["POST"])
@shop_role_required('admin', 'tech')
def project_add_photos(project_id):
    conn = get_db()
    project = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    files = request.files.getlist("photos")
    saved, error = _add_photos(conn, files, "project_id", project_id)
    conn.close()
    if error:
        flash(error, "danger")
    elif saved:
        flash(f"Added {saved} photo(s).", "success")
    else:
        flash("No valid image files were selected.", "danger")
    return redirect(url_for("project_detail", project_id=project_id))


def _photo_owner_redirect(photo):
    """Where a photo's delete/set-cover action sends you back to - whichever
    of part/project/asset it belongs to."""
    if photo["part_id"]:
        return redirect(url_for("part_detail", part_id=photo["part_id"]))
    if photo["project_id"]:
        return redirect(url_for("project_detail", project_id=photo["project_id"]))
    return redirect(url_for("asset_detail", asset_id=photo["asset_id"]))


@app.route("/photos/<int:photo_id>/delete", methods=["POST"])
@shop_role_required('admin')
def photo_delete(photo_id):
    conn = get_db()
    photo = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not photo:
        conn.close()
        abort(404)
    file_path = os.path.join(UPLOAD_DIR, photo["filename"])
    if os.path.exists(file_path):
        os.remove(file_path)
    conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    conn.commit()
    conn.close()
    return _photo_owner_redirect(photo)


@app.route("/photos/<int:photo_id>/set_cover", methods=["POST"])
@shop_role_required('admin', 'tech')
def photo_set_cover(photo_id):
    conn = get_db()
    photo = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not photo:
        conn.close()
        abort(404)
    # Only one photo per part/project/asset can be the cover, so clear any
    # sibling before marking this one.
    if photo["part_id"]:
        conn.execute("UPDATE photos SET is_cover = 0 WHERE part_id = ?", (photo["part_id"],))
    elif photo["project_id"]:
        conn.execute("UPDATE photos SET is_cover = 0 WHERE project_id = ?", (photo["project_id"],))
    else:
        conn.execute("UPDATE photos SET is_cover = 0 WHERE asset_id = ?", (photo["asset_id"],))
    conn.execute("UPDATE photos SET is_cover = 1 WHERE id = ?", (photo_id,))
    conn.commit()
    conn.close()
    flash("Cover photo updated.", "success")
    return _photo_owner_redirect(photo)


# ---------------------------------------------------------------------------
# Pending orders
# ---------------------------------------------------------------------------

@app.route("/orders")
@shop_role_required('admin')
def orders_list():
    """Orders view is groupable by vendor or by part, and searchable. A
    vendor group is further broken into date-based batches, since one
    vendor might have several separate orders placed on different days,
    each with its own set of parts."""
    conn = get_db()
    status_filter = request.args.get("status", "pending")
    view = request.args.get("view", "vendor")
    if view not in ("vendor", "part"):
        view = "vendor"
    base_sql = """SELECT o.*, p.name as part_name, pr.name as project_name, pr.code as project_code
                  FROM orders o
                  LEFT JOIN parts p ON p.id = o.part_id
                  LEFT JOIN projects pr ON pr.id = o.project_id"""
    if status_filter and status_filter != "all":
        rows = conn.execute(base_sql + " WHERE o.status = ? ORDER BY o.ordered_date DESC",
                             (status_filter,)).fetchall()
    else:
        rows = conn.execute(base_sql + " ORDER BY o.ordered_date DESC").fetchall()
    ship_rows = _batch_shipments(conn, [r["batch_id"] or f"o{r['id']}" for r in rows])
    conn.close()

    orders = []
    for r in rows:
        o = dict(r)
        o["item_name"] = o["part_name"] or o["description"]
        o["vendor_name"] = o["supplier"] or "No Vendor Specified"
        o["batch_key"] = o.get("batch_id") or f"o{o['id']}"
        o["shipments"] = [dict(tracking.to_json(sh), id=sh["id"], pkg_status=sh["status"] or "",
                               pending=o["status"] == "pending" and not sh["status"])
                          for sh in ship_rows.get(o["batch_key"], [])]
        o["search_blob"] = " ".join(str(v) for v in [
            o["item_name"], o["vendor_name"], o["project_name"] or "", o["project_code"] or "", o["note"] or "",
            " ".join(sh["number"] or "" for sh in o["shipments"])
        ]).lower()
        orders.append(o)

    groups = []
    if view == "vendor":
        by_vendor = {}
        vendor_order = []
        for o in orders:
            v = o["vendor_name"]
            if v not in by_vendor:
                by_vendor[v] = {}
                vendor_order.append(v)
            batch_date = (o["ordered_date"] or "")[:10]
            by_vendor[v].setdefault(batch_date, []).append(o)
        for v in sorted(vendor_order, key=lambda x: x.lower()):
            batches = []
            for d in sorted(by_vendor[v].keys(), reverse=True):
                lines = by_vendor[v][d]
                # Packages for the lines in this batch, listed once in its
                # header instead of under every item.
                seen, shipments = set(), []
                for ln in lines:
                    for sh in ln["shipments"]:
                        if sh["id"] not in seen:
                            seen.add(sh["id"])
                            shipments.append(dict(sh, pending=not sh["pkg_status"] and any(
                                x["status"] == "pending" for x in lines if x["batch_key"] == ln["batch_key"])))
                batches.append({"date": d, "lines": lines, "shipments": shipments,
                                 "subtotal": sum((i["unit_cost"] or 0) * (i["qty_ordered"] or 0) for i in lines)})
            groups.append({"label": v, "batches": batches,
                            "total": sum(b["subtotal"] for b in batches),
                            "count": sum(len(b["lines"]) for b in batches)})
    else:
        by_part = {}
        part_order = []
        for o in orders:
            k = o["item_name"]
            if k not in by_part:
                by_part[k] = []
                part_order.append(k)
            by_part[k].append(o)
        for k in sorted(part_order, key=lambda x: x.lower()):
            lines = by_part[k]
            groups.append({"label": k, "lines": lines,
                            "total": sum((i["unit_cost"] or 0) * (i["qty_ordered"] or 0) for i in lines),
                            "count": len(lines)})

    conn = get_db()
    wishlist_items = conn.execute("""
        SELECT w.*, p.name as part_name FROM order_wishlist w
        LEFT JOIN parts p ON p.id = w.part_id
        WHERE w.status = 'open'
        ORDER BY CASE w.urgency WHEN 'rush' THEN 0 WHEN 'needed_now' THEN 1 ELSE 2 END, w.created_at
    """).fetchall()
    parts_for_wishlist = conn.execute("SELECT * FROM parts ORDER BY name").fetchall()
    conn.close()

    cores_conn = get_db()
    cores_owed = _cores_owed(cores_conn)
    cores_conn.close()
    return render_template("orders.html", groups=groups, view=view, status_filter=status_filter, cores_owed=cores_owed,
                           order_count=len(orders), wishlist_items=wishlist_items, parts_for_wishlist=parts_for_wishlist)


@app.route("/orders/wishlist/new", methods=["POST"])
@shop_role_required('admin')
def order_wishlist_new():
    description = request.form.get("description", "").strip()
    part_id = request.form.get("part_id") or None
    urgency = request.form.get("urgency") or "no_rush"
    if urgency not in ("rush", "needed_now", "no_rush"):
        urgency = "no_rush"
    if not description and part_id:
        conn = get_db()
        p = conn.execute("SELECT name FROM parts WHERE id = ?", (part_id,)).fetchone()
        description = p["name"] if p else ""
        conn.close()
    if not description:
        flash("Enter what you need (or pick a part).", "danger")
        return redirect(url_for("orders_list"))
    conn = get_db()
    conn.execute(
        "INSERT INTO order_wishlist (description, part_id, urgency, notes, requested_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (description, part_id, urgency, request.form.get("notes", "").strip() or None,
         session.get("user_name"), now_iso()))
    conn.commit()
    conn.close()
    flash(f"Added '{description}' to the order list.", "success")
    return redirect(url_for("orders_list"))


@app.route("/orders/wishlist/<int:wishlist_id>/dismiss", methods=["POST"])
@shop_role_required('admin')
def order_wishlist_dismiss(wishlist_id):
    conn = get_db()
    conn.execute("UPDATE order_wishlist SET status='dismissed', resolved_at=? WHERE id=? AND status='open'",
                 (now_iso(), wishlist_id))
    conn.commit()
    conn.close()
    flash("Removed from the order list.", "info")
    return redirect(url_for("orders_list"))


def _order_tracking_from_form(form):
    """(tracking_number, carrier) from the New/Edit Order form - both None
    when left blank. Carrier blank means "work it out from the number"."""
    number = tracking.clean_number(form.get("tracking_number"))
    carrier = (form.get("tracking_carrier") or "").strip().lower()
    if carrier not in tracking.CARRIERS and carrier != "other":
        carrier = ""
    return (number or None), (carrier or None) if number else None


def _shipments_from_form(form):
    """[(number, carrier_or_None)] from the New/Edit Order form's repeating
    tracking-number rows (tracking_number / tracking_carrier, several of
    each). Blanks and repeats are dropped."""
    numbers = form.getlist("tracking_number") if hasattr(form, "getlist") else [form.get("tracking_number")]
    carriers = form.getlist("tracking_carrier") if hasattr(form, "getlist") else [form.get("tracking_carrier")]
    out, seen = [], set()
    for i, raw in enumerate(numbers):
        number = tracking.clean_number(raw)
        if not number or number in seen:
            continue
        seen.add(number)
        carrier = ((carriers[i] if i < len(carriers) else "") or "").strip().lower()
        if carrier not in tracking.CARRIERS and carrier != "other":
            carrier = ""
        out.append((number, carrier or None))
    return out


def _save_batch_shipments(conn, batch_id, shipments):
    """Makes the order batch's tracking numbers match `shipments`. A number
    that's still there keeps its cached status; a changed carrier forgets
    it; removed numbers are deleted."""
    existing = {r["tracking_number"]: r for r in conn.execute(
        "SELECT * FROM order_shipments WHERE batch_id = ?", (batch_id,)).fetchall()}
    wanted = dict(shipments)
    for number, row in existing.items():
        if number not in wanted:
            conn.execute("DELETE FROM order_shipments WHERE id = ?", (row["id"],))
        elif (row["tracking_carrier"] or None) != wanted[number]:
            conn.execute("""UPDATE order_shipments SET tracking_carrier=?, tracking_status=NULL, tracking_detail=NULL,
                             tracking_location=NULL, tracking_eta=NULL, tracking_events=NULL,
                             tracking_checked_at=NULL, tracking_delivered_at=NULL WHERE id=?""",
                         (wanted[number], row["id"]))
    for number, carrier in shipments:
        if number not in existing:
            conn.execute("INSERT INTO order_shipments (batch_id, tracking_number, tracking_carrier, created_at) "
                         "VALUES (?, ?, ?, ?)", (batch_id, number, carrier, now_iso()))


def _batch_shipments(conn, batch_ids):
    """{batch_id: [shipment rows]} for the given order batches."""
    batch_ids = [b for b in set(batch_ids) if b]
    if not batch_ids:
        return {}
    out = {}
    marks = ",".join("?" * len(batch_ids))
    for r in conn.execute(f"SELECT * FROM order_shipments WHERE batch_id IN ({marks}) ORDER BY id", batch_ids):
        out.setdefault(r["batch_id"], []).append(r)
    return out


def _refresh_shipment(conn, shipment):
    """Asks the carrier for a shipment's latest status when its cache is
    stale (tracking.needs_refresh) and it's still worth checking. Returns
    (fresh shipment row, error or None). A regular order batch is worth
    checking while any of its lines is still pending; a core's return
    shipment (batch_id "core<order_id>" - see order_core_shipped) is worth
    checking until that core's credit is received."""
    batch_id = shipment["batch_id"]
    if batch_id.startswith("core") and batch_id[4:].isdigit():
        pending = conn.execute("SELECT 1 FROM orders WHERE id = ? AND core_credited_at IS NULL",
                               (int(batch_id[4:]),)).fetchone()
    else:
        pending = conn.execute("SELECT 1 FROM orders WHERE COALESCE(batch_id, 'o' || id) = ? AND status = 'pending'",
                               (batch_id,)).fetchone()
    error = None
    if pending and tracking.needs_refresh(shipment):
        result = tracking.fetch_status(shipment)
        error = result.pop("error", None)
        if not error:
            result["tracking_checked_at"] = now_iso()
            cols = ", ".join(f"{k} = ?" for k in result)
            conn.execute(f"UPDATE order_shipments SET {cols} WHERE id = ?", (*result.values(), shipment["id"]))
            conn.commit()
            shipment = conn.execute("SELECT * FROM order_shipments WHERE id = ?", (shipment["id"],)).fetchone()
    return shipment, error


@app.route("/api/orders/shipments/<int:shipment_id>/tracking")
@shop_role_required('admin')
def api_shipment_tracking(shipment_id):
    """Latest status of one package, for the Orders page's live badges."""
    conn = get_db()
    shipment = conn.execute("SELECT * FROM order_shipments WHERE id = ?", (shipment_id,)).fetchone()
    if not shipment:
        conn.close()
        return jsonify({"ok": False, "error": "no_tracking"}), 404
    shipment, error = _refresh_shipment(conn, shipment)
    conn.close()
    return jsonify(dict(tracking.to_json(shipment), ok=True, error=error, shipment_id=shipment_id))


@app.route("/api/orders/<int:order_id>/tracking")
@shop_role_required('admin')
def api_order_tracking(order_id):
    """Older per-order address: the first package of that order's batch."""
    conn = get_db()
    order = conn.execute("SELECT id, COALESCE(batch_id, 'o' || id) AS batch_id FROM orders WHERE id = ?",
                         (order_id,)).fetchone()
    shipment = conn.execute("SELECT * FROM order_shipments WHERE batch_id = ? ORDER BY id LIMIT 1",
                            (order["batch_id"],)).fetchone() if order else None
    if not shipment:
        conn.close()
        return jsonify({"ok": False, "error": "no_tracking"}), 404
    shipment, error = _refresh_shipment(conn, shipment)
    conn.close()
    return jsonify(dict(tracking.to_json(shipment), ok=True, error=error, shipment_id=shipment["id"]))


def _order_core_from_form(form):
    """(is_exchange, core_charge, core_days, error) from New/Edit Order: an
    exchange unit owes its old core back to the supplier within core_days
    of receiving it, or core_charge gets billed."""
    if form.get("is_exchange") != "on":
        return 0, None, None, None
    raw_charge = (form.get("core_charge") or "").strip()
    charge = _parse_qty(raw_charge, allow_zero=True) if raw_charge else 0.0
    try:
        days = int((form.get("core_days") or "30").strip())
    except ValueError:
        days = None
    if charge is None:
        return 0, None, None, "Core charge must be a number, 0 or more."
    if days is None or not 1 <= days <= 365:
        return 0, None, None, "Days allowed for the core must be a whole number from 1 to 365."
    return 1, charge, days, None


def _parse_order_numbers(form):
    """Quantity and unit cost from the New/Edit Order form -> (qty, cost,
    error). Blank quantity means 1 and blank cost means 0. Quantity must be
    a real number above 0 and cost a real number 0 or more - a stray minus
    sign would turn Receive into a silent stock removal, and nan/inf wreck
    every total and the vendor CSV."""
    raw_qty = (form.get("qty_ordered") or "").strip()
    raw_cost = (form.get("unit_cost") or "").strip()
    qty = _parse_qty(raw_qty) if raw_qty else 1.0
    cost = _parse_qty(raw_cost, allow_zero=True) if raw_cost else 0.0
    if qty is None:
        return None, None, "Quantity must be a number greater than 0."
    if cost is None:
        return None, None, "Unit cost must be a number, 0 or more."
    return qty, cost, None


def _order_lines_from_form(conn, form):
    """The item rows of New Order -> ([line dicts], error). Each row is
    part_id / description / qty_ordered / unit_cost / is_exchange /
    core_charge / core_days (several of each, one per row, in order).
    Completely blank rows are skipped."""
    fields = ("part_id", "description", "qty_ordered", "unit_cost", "is_exchange", "core_charge", "core_days")
    cols = {f: form.getlist(f) for f in fields}
    n = max(len(v) for v in cols.values()) if cols else 0
    lines = []
    for i in range(n):
        row = {f: ((cols[f][i] if i < len(cols[f]) else "") or "").strip() for f in fields}
        part_id = row["part_id"] or None
        description = row["description"]
        if not part_id and not description:
            continue
        if not description and part_id:
            p = conn.execute("SELECT name FROM parts WHERE id = ?", (part_id,)).fetchone()
            description = p["name"] if p else ""
        label = f"Item {len(lines) + 1}: " if n > 1 else ""
        if not description:
            return None, f"{label}Enter what you're ordering (or pick a part)."
        qty, cost, number_error = _parse_order_numbers(row)
        is_exchange, core_charge, core_days, core_error = _order_core_from_form(row)
        if number_error or core_error:
            return None, label + (number_error or core_error)
        lines.append(dict(part_id=part_id, description=description, qty=qty, cost=cost, is_exchange=is_exchange,
                          core_charge=core_charge, core_days=core_days))
    if not lines:
        return None, "Enter what you're ordering (or pick a part)."
    return lines, None


def _project_for_order_form(conn, form):
    """The "For project" choice on New/Edit Order -> (project_id, error).
    "__new__" makes a new job right there from the name (and optional
    plane) typed in, the same way New Project starts one."""
    raw = (form.get("project_id") or "").strip()
    if raw != "__new__":
        return (raw or None), None
    name = (form.get("new_project_name") or "").strip()
    if not name:
        return None, "Type a name for the new project (or pick an existing one)."
    asset_id = (form.get("new_project_asset_id") or "").strip() or None
    if asset_id and not conn.execute("SELECT 1 FROM assets WHERE id = ? AND deleted_at IS NULL", (asset_id,)).fetchone():
        asset_id = None
    code = gen_project_code(conn)
    cur = conn.execute("INSERT INTO projects (code, name, description, status, asset_id, created_at) "
                       "VALUES (?, ?, '', 'active', ?, ?)", (code, name, asset_id, now_iso()))
    conn.execute("UPDATE projects SET intake_status = 'pending' WHERE id = ?", (cur.lastrowid,))
    return cur.lastrowid, None


def _order_form_lists(conn):
    parts = conn.execute("SELECT * FROM parts ORDER BY name").fetchall()
    projects = conn.execute("SELECT * FROM projects WHERE status='active' AND deleted_at IS NULL ORDER BY name").fetchall()
    assets = conn.execute("SELECT id, tag FROM assets WHERE deleted_at IS NULL AND is_simulator = 0 "
                          "AND is_owner_placeholder = 0 ORDER BY tag").fetchall()
    return dict(parts=parts, projects=projects, assets=assets)


def _form_rows(form):
    """Item and tracking rows to redraw the New Order form after an error."""
    if not form or not hasattr(form, "getlist"):
        return None, None
    fields = ("part_id", "description", "qty_ordered", "unit_cost", "is_exchange", "core_charge", "core_days")
    cols = {f: form.getlist(f) for f in fields}
    n = max(1, max(len(v) for v in cols.values()))
    items = [{f: (cols[f][i] if i < len(cols[f]) else "") for f in fields} for i in range(n)]
    nums, cars = form.getlist("tracking_number"), form.getlist("tracking_carrier")
    ships = [{"tracking_number": nums[i], "tracking_carrier": cars[i] if i < len(cars) else ""}
             for i in range(len(nums))]
    return items, ships


@app.route("/orders/new", methods=["GET", "POST"])
@shop_role_required('admin')
def order_new():
    """One New Order can hold several items from the same supplier (each
    saved as its own order line, so each is received, cancelled or tracked
    as a core on its own) and several tracking numbers for the packages it
    ships in. Lines entered together share a batch_id."""
    conn = get_db()
    if request.method == "POST":
        wishlist_id = request.form.get("wishlist_id") or None
        lines, error = _order_lines_from_form(conn, request.form)
        project_id = None
        if not error:
            project_id, error = _project_for_order_form(conn, request.form)
        if error:
            # Show the form again with everything still filled in. (The
            # parts/projects lists are read BEFORE closing the connection -
            # reading them after used to crash with an error page.)
            conn.rollback()
            flash(error, "danger")
            lists = _order_form_lists(conn)
            conn.close()
            items, ships = _form_rows(request.form)
            return render_template("order_form.html", form=request.form, items=items, ships=ships, **lists)
        batch_id = "b" + secrets.token_hex(6)
        supplier = request.form.get("supplier", "").strip()
        expected = request.form.get("expected_date") or None
        note = request.form.get("note", "").strip()
        for ln in lines:
            conn.execute("""INSERT INTO orders (part_id, description, qty_ordered, supplier, unit_cost,
                             project_id, status, ordered_date, expected_date, note, created_at,
                             is_exchange, core_charge, core_days, batch_id)
                             VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (ln["part_id"], ln["description"], ln["qty"], supplier, ln["cost"], project_id, now_iso(),
                          expected, note, now_iso(), ln["is_exchange"], ln["core_charge"], ln["core_days"], batch_id))
        _save_batch_shipments(conn, batch_id, _shipments_from_form(request.form))
        if wishlist_id:
            # This order started from a "things to order" entry - close that
            # entry out now that it's an actual order, rather than leaving
            # it sitting there alongside the real order.
            conn.execute("UPDATE order_wishlist SET status='ordered', resolved_at=? WHERE id=? AND status='open'",
                         (now_iso(), wishlist_id))
        conn.commit()
        conn.close()
        if len(lines) == 1:
            flash(f"Order for '{lines[0]['description']}' added.", "success")
        else:
            flash(f"Order added: {len(lines)} items{' from ' + supplier if supplier else ''}.", "success")
        return redirect(url_for("orders_list"))
    lists = _order_form_lists(conn)
    prefill = {}
    wishlist_id = request.args.get("wishlist_id")
    if wishlist_id:
        w = conn.execute("SELECT * FROM order_wishlist WHERE id = ? AND status = 'open'", (wishlist_id,)).fetchone()
        if w:
            prefill = {"description": w["description"], "part_id": str(w["part_id"]) if w["part_id"] else "",
                       "note": w["notes"] or "", "wishlist_id": str(wishlist_id)}
    qty, cost = "1", "0"
    reorder_part = request.args.get("part_id")
    if reorder_part and not wishlist_id:
        # Reorder button on Low Stock: part, usual supplier and a quantity
        # that brings it back up to its reorder point are already filled in.
        rp = conn.execute("SELECT * FROM parts WHERE id = ? AND retired_at IS NULL", (reorder_part,)).fetchone()
        if rp:
            missing = (rp["reorder_point"] or 0) - (rp["qty_on_hand"] or 0)
            qty = "%g" % max(1, missing)
            cost = "%g" % (rp["unit_cost"] or 0)
            prefill = {"part_id": str(rp["id"]), "description": rp["name"], "supplier": rp["supplier"] or ""}
    conn.close()
    items = [{"part_id": prefill.get("part_id", ""), "description": prefill.get("description", ""),
              "qty_ordered": qty, "unit_cost": cost, "is_exchange": "", "core_charge": "", "core_days": "30"}]
    return render_template("order_form.html", form=prefill, items=items, ships=[], **lists)


@app.route("/orders/<int:order_id>/edit", methods=["GET", "POST"])
@shop_role_required('admin')
def order_edit(order_id):
    """Lets a pending order's quantity, cost, supplier, dates, etc. be
    corrected/typed in before it's sent to the vendor or exported. The
    tracking numbers belong to the whole order (every item entered with it
    on the same New Order), so changing them here changes them for all."""
    conn = get_db()
    order = conn.execute("SELECT *, COALESCE(batch_id, 'o' || id) AS batch_key FROM orders WHERE id = ?",
                         (order_id,)).fetchone()
    if not order:
        conn.close()
        flash("Order not found.", "danger")
        return redirect(url_for("orders_list"))
    if order["status"] != "pending":
        # Received and cancelled orders are history: editing one would make
        # the record disagree with what actually came in (and was added to
        # stock), and change past order totals after the fact.
        conn.close()
        flash(f"Order #{order_id} is already {order['status']} and can't be edited.", "warning")
        return redirect(url_for("orders_list"))
    lists = _order_form_lists(conn)
    batch_count = conn.execute("SELECT COUNT(*) c FROM orders WHERE COALESCE(batch_id, 'o' || id) = ?",
                               (order["batch_key"],)).fetchone()["c"]
    ships = [dict(r) for r in _batch_shipments(conn, [order["batch_key"]]).get(order["batch_key"], [])]
    if request.method == "POST":
        description = request.form.get("description", "").strip()
        part_id = request.form.get("part_id") or None
        if not description and part_id:
            p = conn.execute("SELECT name FROM parts WHERE id = ?", (part_id,)).fetchone()
            description = p["name"] if p else ""
        _items, form_ships = _form_rows(request.form)
        if not description:
            flash("Enter what you're ordering (or pick a part).", "danger")
            conn.close()
            return render_template("order_form.html", form=request.form, order=order, ships=form_ships,
                                   batch_count=batch_count, **lists)
        qty, cost, number_error = _parse_order_numbers(request.form)
        is_exchange, core_charge, core_days, core_error = _order_core_from_form(request.form)
        number_error = number_error or core_error
        project_id = None
        if not number_error:
            project_id, number_error = _project_for_order_form(conn, request.form)
        if number_error:
            conn.rollback()
            flash(number_error, "danger")
            conn.close()
            return render_template("order_form.html", form=request.form, order=order, ships=form_ships,
                                   batch_count=batch_count, **lists)
        if not order["batch_id"]:
            conn.execute("UPDATE orders SET batch_id = ? WHERE id = ?", (order["batch_key"], order_id))
        _save_batch_shipments(conn, order["batch_key"], _shipments_from_form(request.form))
        conn.execute("""UPDATE orders SET part_id=?, description=?, qty_ordered=?, supplier=?, unit_cost=?,
                         project_id=?, expected_date=?, note=?,
                         is_exchange=?, core_charge=?, core_days=?
                         WHERE id=? AND status='pending'""",
                     (part_id, description, qty, request.form.get("supplier", "").strip(), cost,
                      project_id, request.form.get("expected_date") or None,
                      request.form.get("note", "").strip(),
                      is_exchange, core_charge, core_days, order_id))
        conn.commit()
        conn.close()
        flash(f"Order for '{description}' updated.", "success")
        return redirect(url_for("orders_list"))
    conn.close()
    return render_template("order_form.html", form=None, order=order, ships=ships, batch_count=batch_count, **lists)


@app.route("/orders/export")
@shop_role_required('admin', 'tech')
def orders_export():
    """CSV download of orders - ready to type up quantities/notes on the
    Orders page first (or via Edit), then export and send/email straight
    to a vendor. Filterable to one vendor and/or a status, same as the
    Orders page itself."""
    status_filter = request.args.get("status", "pending")
    vendor = request.args.get("vendor", "").strip()
    conn = get_db()
    base_sql = """SELECT o.*, p.name as part_name, pr.name as project_name, pr.code as project_code
                  FROM orders o
                  LEFT JOIN parts p ON p.id = o.part_id
                  LEFT JOIN projects pr ON pr.id = o.project_id"""
    if status_filter and status_filter != "all":
        rows = conn.execute(base_sql + " WHERE o.status = ? ORDER BY o.ordered_date DESC", (status_filter,)).fetchall()
    else:
        rows = conn.execute(base_sql + " ORDER BY o.ordered_date DESC").fetchall()
    conn.close()

    show_costs = can_see_shop_costs()
    out = io.StringIO()
    writer = csv.writer(out)
    header = ["Item", "Qty"]
    if show_costs:
        header += ["Unit Cost", "Est. Total"]
    header += ["Vendor", "Project", "Expected Date", "Note"]
    writer.writerow(header)
    for o in rows:
        vendor_name = o["supplier"] or "No Vendor Specified"
        if vendor and vendor_name != vendor:
            continue
        item_name = o["part_name"] or o["description"]
        qty = o["qty_ordered"] or 0
        row = [item_name, f"{qty:g}"]
        if show_costs:
            cost = o["unit_cost"] or 0
            row += [f"{cost:.2f}", f"{qty * cost:.2f}"]
        row += [vendor_name, o["project_code"] or "", o["expected_date"] or "", o["note"] or ""]
        writer.writerow(row)
    filename = f"orders_{(vendor or 'all').replace(' ', '_')}_{status_filter}.csv"
    return Response(out.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.route("/orders/<int:order_id>/receive", methods=["POST"])
@shop_role_required('admin')
def order_receive(order_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        abort(404)
    # Receive only ever ADDS stock. An older order saved with a zero,
    # negative or nan quantity (before the form checked) is refused here
    # rather than silently removing parts from the shelf.
    if _parse_qty(order["qty_ordered"]) is None and order["status"] not in ('received', 'cancelled'):
        conn.close()
        flash(f"Order #{order_id} has a quantity of {order['qty_ordered']} - edit it to a number above 0 "
              f"before receiving. Stock was not changed.", "danger")
        return redirect(url_for("orders_list"))
    # Claim the order atomically: only the request that actually flips it to
    # 'received' adds stock. Stops a double-click / Back-button resubmit / two
    # people receiving the same box from adding the quantity twice, and stops
    # a cancelled order from being received.
    claimed = conn.execute(
        "UPDATE orders SET status='received', received_date=? WHERE id=? AND status NOT IN ('received', 'cancelled')",
        (now_iso(), order_id)).rowcount
    if not claimed:
        conn.close()
        flash(f"Order #{order_id} is already {order['status']} - stock was not changed.", "warning")
        return redirect(url_for("orders_list"))

    part_id = order["part_id"]
    new_part_created = False
    if not part_id:
        # This order was for something not yet in inventory - create the
        # part now, automatically, using what we know from the order.
        barcode = gen_internal_barcode(conn)
        cur = conn.execute("""INSERT INTO parts (barcode, name, description, category, location, unit,
                               qty_on_hand, reorder_point, unit_cost, supplier, created_at, updated_at)
                               VALUES (?, ?, '', '', '', 'ea', 0, 0, ?, ?, ?, ?)""",
                            (barcode, order["description"], order["unit_cost"] or 0,
                             order["supplier"] or "", now_iso(), now_iso()))
        part_id = cur.lastrowid
        conn.execute("UPDATE orders SET part_id = ? WHERE id = ?", (part_id, order_id))
        new_part_created = True

    conn.execute("UPDATE parts SET qty_on_hand = qty_on_hand + ?, updated_at = ? WHERE id = ?",
                 (order["qty_ordered"], now_iso(), part_id))
    conn.execute("""INSERT INTO transactions (part_id, project_id, type, qty, note, source, created_at)
                     VALUES (?, NULL, 'in', ?, ?, 'assigned', ?)""",
                 (part_id, order["qty_ordered"], f"Received order #{order_id}", now_iso()))
    if order["is_exchange"]:
        # The core clock starts when the exchange unit arrives.
        due = (date.today() + timedelta(days=order["core_days"] or 30)).isoformat()
        conn.execute("UPDATE orders SET core_due_date = ? WHERE id = ?", (due, order_id))
        flash(f"Exchange unit - its old core is due back to {order['supplier'] or 'the supplier'} by "
              f"{usdate(due)}. It's listed under Cores owed on the Orders page.", "info")
    conn.commit()
    conn.close()

    if new_part_created:
        flash(f"Received - added '{order['description']}' as a new part with {order['qty_ordered']:g} in stock. "
              f"Set its category, location and reorder point when you get a chance.", "success")
        return redirect(url_for("part_detail", part_id=part_id))

    flash(f"Received and added {order['qty_ordered']:g} to stock.", "success")
    conn_exp = get_db()
    dated = conn_exp.execute("SELECT expiration_date FROM parts WHERE id = ?", (part_id,)).fetchone()
    conn_exp.close()
    if dated and dated["expiration_date"]:
        # A shelf-life part: ask for the new stock's date (Skip keeps the old one).
        return redirect(url_for("part_expiration_update", part_id=part_id))
    return redirect(url_for("orders_list"))


@app.route("/orders/<int:order_id>/core-shipped", methods=["POST"])
@shop_role_required('admin')
def order_core_shipped(order_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ? AND is_exchange = 1", (order_id,)).fetchone()
    if not order:
        conn.close()
        abort(404)
    tracking_no = tracking.clean_number(request.form.get("core_tracking")) or None
    conn.execute("UPDATE orders SET core_shipped_at = COALESCE(core_shipped_at, ?), core_tracking = ? WHERE id = ?",
                 (now_iso(), tracking_no, order_id))
    # Idea "order tracking live": the core's own return shipment, tracked
    # with the same order_shipments/live-status machinery as a regular
    # order (see _refresh_shipment's core-batch case below) - a synthetic
    # batch id keyed to this order rather than a real order batch.
    batch_id = f"core{order_id}"
    conn.execute("DELETE FROM order_shipments WHERE batch_id = ?", (batch_id,))
    if tracking_no:
        conn.execute("INSERT INTO order_shipments (batch_id, tracking_number, tracking_carrier, created_at) "
                     "VALUES (?, ?, ?, ?)", (batch_id, tracking_no, tracking.guess_carrier(tracking_no) or None, now_iso()))
    conn.commit()
    conn.close()
    flash("Core marked shipped. Press Credit received once the supplier credits it.", "success")
    return redirect(url_for("orders_list") + "#cores-owed")


@app.route("/orders/<int:order_id>/core-credited", methods=["POST"])
@shop_role_required('admin')
def order_core_credited(order_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ? AND is_exchange = 1", (order_id,)).fetchone()
    if not order:
        conn.close()
        abort(404)
    conn.execute("UPDATE orders SET core_credited_at = COALESCE(core_credited_at, ?), "
                 "core_shipped_at = COALESCE(core_shipped_at, ?) WHERE id = ?", (now_iso(), now_iso(), order_id))
    conn.commit()
    conn.close()
    flash("Core credit received - that one's closed out.", "success")
    return redirect(url_for("orders_list") + "#cores-owed")


def _cores_owed(conn):
    """Exchange cores still owed back (not credited yet), soonest due first,
    with days left and a color: red overdue, amber within 7 days."""
    rows = conn.execute("""SELECT o.*, p.name as part_name, pr.code as project_code, pr.name as project_name,
                                  a.tag as asset_tag
                           FROM orders o LEFT JOIN parts p ON p.id = o.part_id
                           LEFT JOIN projects pr ON pr.id = o.project_id
                           LEFT JOIN assets a ON a.id = pr.asset_id
                           WHERE o.is_exchange = 1 AND o.status = 'received' AND o.core_credited_at IS NULL
                           ORDER BY o.core_shipped_at IS NOT NULL, o.core_due_date""").fetchall()
    today = date.today()
    shipments_by_batch = _batch_shipments(conn, [f"core{r['id']}" for r in rows])
    out = []
    for r in rows:
        c = dict(r)
        try:
            c["days_left"] = (datetime.strptime(r["core_due_date"], "%Y-%m-%d").date() - today).days
        except (TypeError, ValueError):
            c["days_left"] = None
        c["urgency"] = ("shipped" if r["core_shipped_at"] else
                        "overdue" if c["days_left"] is not None and c["days_left"] < 0 else
                        "soon" if c["days_left"] is not None and c["days_left"] <= 7 else "ok")
        # Still worth live-refreshing until the core's credit is received -
        # _cores_owed already filters to core_credited_at IS NULL rows.
        c["shipments"] = [dict(sh, pending=True) for sh in shipments_by_batch.get(f"core{r['id']}", [])]
        out.append(c)
    return out


@app.route("/orders/<int:order_id>/cancel", methods=["POST"])
@shop_role_required('admin')
def order_cancel(order_id):
    conn = get_db()
    # A received order's stock is already on the shelf - cancelling it would
    # leave the order and inventory disagreeing.
    changed = conn.execute("UPDATE orders SET status='cancelled' WHERE id=? AND status != 'received'",
                           (order_id,)).rowcount
    conn.commit()
    conn.close()
    if not changed:
        flash("That order was already received, so it can't be cancelled. Adjust the part's count instead if needed.",
              "warning")
        return redirect(url_for("orders_list"))
    flash("Order cancelled.", "success")
    return redirect(url_for("orders_list"))


@app.route("/orders/shipments/<int:shipment_id>/<action>", methods=["POST"])
@shop_role_required('admin')
def order_shipment_mark(shipment_id, action):
    """Marks ONE tracking number (package) of an order received or cancelled.
    The order's other packages and its item lines are left alone; items are
    still received one at a time."""
    if action not in ("receive", "cancel", "reopen"):
        abort(404)
    conn = get_db()
    new = {"receive": "received", "cancel": "cancelled", "reopen": None}[action]
    changed = conn.execute("UPDATE order_shipments SET status=? WHERE id=?", (new, shipment_id)).rowcount
    conn.commit()
    conn.close()
    if not changed:
        abort(404)
    flash({"receive": "Package marked received.", "cancel": "Package cancelled.",
           "reopen": "Package put back as open."}[action], "success")
    return redirect(url_for("orders_list"))


# ---------------------------------------------------------------------------
# Labor tracking: laborers (with a scannable QR code + hourly rate) and
# scan-to-start/scan-to-stop timed labor_sessions against a project/task.
# ---------------------------------------------------------------------------

@app.route("/manage/task-templates", methods=["GET", "POST"])
@shop_role_required('admin')
def task_templates():
    """Manage > Task Templates: for each Quick Type button on New/Edit
    Project (the 4 built-ins plus any admin-added custom ones - see
    _all_quick_types()), a preset list of Sub Areas that get auto-created on
    a project the moment that Quick Type is picked. An area can be marked
    Optional - it then only comes along if its checkbox is ticked on New
    Project, instead of every area on the template always being added."""
    conn = get_db()
    if request.method == "POST":
        if request.form.get("new_type_name") is not None:
            # A whole new Quick Type button (beyond the 4 built-ins).
            new_type_name = request.form.get("new_type_name", "").strip()
            if not new_type_name:
                flash("Enter a name for the new Quick Type.", "danger")
            elif new_type_name in _all_quick_types(conn):
                flash(f"'{new_type_name}' already exists.", "danger")
            else:
                next_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM task_template_types").fetchone()["n"]
                conn.execute("INSERT INTO task_template_types (name, sort_order, created_at) VALUES (?, ?, ?)",
                             (new_type_name, next_order, now_iso()))
                conn.commit()
                flash(f"Added Quick Type '{new_type_name}'.", "success")
        else:
            quick_type = request.form.get("quick_type", "")
            name = request.form.get("name", "").strip()
            is_optional = 1 if request.form.get("is_optional") else 0
            if quick_type not in _all_quick_types(conn) or not name:
                flash("Pick a Quick Type and enter an area name.", "danger")
            else:
                next_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM task_template_areas WHERE quick_type = ?",
                                          (quick_type,)).fetchone()["n"]
                conn.execute("INSERT OR IGNORE INTO task_template_areas (quick_type, name, sort_order, is_optional, created_at) VALUES (?, ?, ?, ?, ?)",
                             (quick_type, name, next_order, is_optional, now_iso()))
                conn.commit()
                flash(f"Added '{name}' to {quick_type}{' (optional)' if is_optional else ''}.", "success")
        conn.close()
        return redirect(url_for("task_templates"))
    quick_types = _all_quick_types(conn)
    custom_type_rows = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM task_template_types").fetchall()}
    areas_by_type = {t: conn.execute("SELECT * FROM task_template_areas WHERE quick_type = ? ORDER BY sort_order, name",
                                     (t,)).fetchall() for t in quick_types}
    conn.close()
    return render_template("task_templates.html", quick_types=quick_types, areas_by_type=areas_by_type,
                           custom_type_rows=custom_type_rows)


@app.route("/manage/task-templates/<int:area_id>/edit", methods=["POST"])
@shop_role_required('admin')
def task_template_area_edit(area_id):
    """Renames a template area in place and/or flips its Optional flag -
    used to have to be deleted and re-added to change either."""
    name = request.form.get("name", "").strip()
    is_optional = 1 if request.form.get("is_optional") else 0
    conn = get_db()
    if not name:
        flash("Area name can't be blank.", "danger")
    else:
        conn.execute("UPDATE task_template_areas SET name = ?, is_optional = ? WHERE id = ?",
                     (name, is_optional, area_id))
        conn.commit()
        flash(f"Saved '{name}'.", "success")
    conn.close()
    return redirect(url_for("task_templates"))


@app.route("/manage/task-templates/<int:area_id>/delete", methods=["POST"])
@shop_role_required('admin')
def task_template_area_delete(area_id):
    conn = get_db()
    conn.execute("DELETE FROM task_template_areas WHERE id = ?", (area_id,))
    conn.commit()
    conn.close()
    flash("Removed.", "success")
    return redirect(url_for("task_templates"))


@app.route("/manage/task-templates/type/<int:type_id>/delete", methods=["POST"])
@shop_role_required('admin')
def task_template_type_delete(type_id):
    """Removes one of the custom Quick Types an admin added (not the 4
    built-ins, which aren't rows here at all) and every area under it."""
    conn = get_db()
    row = conn.execute("SELECT name FROM task_template_types WHERE id = ?", (type_id,)).fetchone()
    if row:
        conn.execute("DELETE FROM task_template_areas WHERE quick_type = ?", (row["name"],))
        conn.execute("DELETE FROM task_template_types WHERE id = ?", (type_id,))
        conn.commit()
        flash(f"Removed Quick Type '{row['name']}'.", "success")
    conn.close()
    return redirect(url_for("task_templates"))


@app.route("/labor/badges")
@shop_role_required('admin', 'tech')
def laborer_badges():
    """Print-only view of laborer codes for admin+tech - no edit/rate/active
    controls, so it doesn't need the full Laborers list's admin-only access."""
    conn = get_db()
    laborers = conn.execute("SELECT * FROM laborers WHERE active = 1 ORDER BY name").fetchall()
    conn.close()
    return render_template("laborer_badges.html", laborers=laborers)


@app.route("/laborers")
@shop_role_required('admin')
def laborers_list():
    conn = get_db()
    q = request.args.get("q", "").strip()
    query = ("SELECT laborers.*, u.name AS account_name, u.id AS account_id FROM laborers "
             "LEFT JOIN users u ON u.id = laborers.user_id WHERE 1=1")
    params = []
    if q:
        query += " AND laborers.name LIKE ?"
        params.append(f"%{q}%")
    query += " ORDER BY laborers.active DESC, laborers.name"
    laborers = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("laborers.html", laborers=laborers, q=q)


@app.route("/laborers/new", methods=["GET", "POST"])
@shop_role_required('admin')
def laborer_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "danger")
            return render_template("laborer_form.html", laborer=None)
        rate = _parse_rate(request.form.get("rate"))
        if rate is None:
            flash("Hourly rate must be a number, 0 or more.", "danger")
            return render_template("laborer_form.html", laborer=None,
                                   draft={"name": name, "rate": request.form.get("rate", "")})
        conn = get_db()
        code = gen_labor_code(conn)
        cur = conn.execute(
            "INSERT INTO laborers (name, code, rate, active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
            (name, code, rate, now_iso(), now_iso()))
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
        flash(f"Laborer '{name}' added. Print their code so they can scan in/out.", "success")
        return redirect(url_for("laborer_label", laborer_id=new_id))
    return render_template("laborer_form.html", laborer=None)


@app.route("/laborers/<int:laborer_id>/edit", methods=["GET", "POST"])
@shop_role_required('admin')
def laborer_edit(laborer_id):
    conn = get_db()
    laborer = conn.execute("SELECT * FROM laborers WHERE id = ?", (laborer_id,)).fetchone()
    if not laborer:
        conn.close()
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "danger")
            conn.close()
            return render_template("laborer_form.html", laborer=laborer)
        rate = _parse_rate(request.form.get("rate"))
        if rate is None:
            flash("Hourly rate must be a number, 0 or more.", "danger")
            conn.close()
            return render_template("laborer_form.html", laborer=laborer)
        active = 1 if request.form.get("active") == "on" else 0
        conn.execute("UPDATE laborers SET name = ?, rate = ?, active = ?, updated_at = ? WHERE id = ?",
                     (name, rate, active, now_iso(), laborer_id))
        conn.commit()
        conn.close()
        flash("Laborer updated.", "success")
        return redirect(url_for("laborers_list"))
    conn.close()
    return render_template("laborer_form.html", laborer=laborer)


@app.route("/laborers/<int:laborer_id>/label")
@shop_role_required('admin')
def laborer_label(laborer_id):
    conn = get_db()
    laborer = conn.execute("SELECT * FROM laborers WHERE id = ?", (laborer_id,)).fetchone()
    conn.close()
    if not laborer:
        abort(404)
    return render_template("laborer_label.html", laborer=laborer)


@app.route("/laborers/<int:laborer_id>/print-label", methods=["POST"])
@shop_role_required('admin', 'tech')
def laborer_print_label(laborer_id):
    conn = get_db()
    laborer = conn.execute("SELECT * FROM laborers WHERE id = ?", (laborer_id,)).fetchone()
    conn.close()
    if not laborer:
        abort(404)
    try:
        from label_printer import print_laborer_label
        print_laborer_label(laborer)
        flash(f"Label sent to printer for {laborer['name']}.", "success")
    except Exception as e:
        flash(f"Couldn't print label: {e}", "danger")
    is_admin = bool(session.get("is_master_admin") or session.get("shop_role") == "admin")
    if is_admin:
        return redirect(url_for("laborer_label", laborer_id=laborer_id))
    return redirect(url_for("laborer_badges"))


# ---------------------------------------------------------------------------
# Winds Aloft > Manage: Labor Pay, Billing, Stats - the shop-side versions of
# the Flight School's My Pay / Billing / Stats, built from labor_sessions
# (clocked labor, rate snapshotted at clock-in) and project part usage
# (transactions out minus returns). All three share one period picker.
# ---------------------------------------------------------------------------

SHOP_PERIODS = [("this_week", "This Week"), ("last_week", "Last Week"), ("this_month", "This Month"),
                ("last_month", "Last Month"), ("this_year", "This Year"), ("all", "All Time")]


def _shop_period(default="this_month"):
    """(start, end, period, label) for ?period= (one of SHOP_PERIODS) or a
    custom ?from=YYYY-MM-DD&to=YYYY-MM-DD. Dates inclusive, as YYYY-MM-DD
    strings; "all" gives a wide-open range."""
    today = date.today()
    d_from = (request.args.get("from") or "").strip()
    d_to = (request.args.get("to") or "").strip()
    if d_from or d_to:
        try:
            start = datetime.strptime(d_from, "%Y-%m-%d").date() if d_from else date(2000, 1, 1)
            end = datetime.strptime(d_to, "%Y-%m-%d").date() if d_to else today
            if end < start:
                start, end = end, start
            return start.isoformat(), end.isoformat(), "custom", f"{start.strftime('%d-%m-%Y')} to {end.strftime('%d-%m-%Y')}"
        except ValueError:
            pass
    period = request.args.get("period", default)
    if period not in dict(SHOP_PERIODS):
        period = default
    if period == "this_week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif period == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        end = start + timedelta(days=6)
    elif period == "this_month":
        start = today.replace(day=1)
        end = today
    elif period == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif period == "this_year":
        start = today.replace(month=1, day=1)
        end = today
    else:
        start, end = date(2000, 1, 1), date(2100, 1, 1)
    return start.isoformat(), end.isoformat(), period, dict(SHOP_PERIODS)[period]


def _shop_parts_usage(conn, start, end, project_id=None):
    """Parts used on projects in [start, end]: each out minus returns (in)
    with a project, valued at the part's sell price (billed) and unit cost."""
    sql = """SELECT t.project_id, p.id as part_id, p.name, p.unit,
                    SUM(CASE WHEN t.type = 'out' THEN t.qty ELSE -t.qty END) as qty,
                    COALESCE(p.sell_price, 0) as sell_price, COALESCE(p.unit_cost, 0) as unit_cost
             FROM transactions t JOIN parts p ON p.id = t.part_id
             WHERE t.project_id IS NOT NULL AND t.type IN ('out', 'in')
               AND date(t.created_at) BETWEEN ? AND ?"""
    params = [start, end]
    if project_id:
        sql += " AND t.project_id = ?"
        params.append(project_id)
    sql += " GROUP BY t.project_id, p.id HAVING qty > 0"
    return conn.execute(sql, params).fetchall()


@app.route("/shop/pay")
@shop_role_required('admin', 'tech', 'apprentice', 'inspector')
def shop_pay():
    """Labor Pay: each laborer's clocked hours and pay for a period, with
    the individual sessions underneath. Shop admins see everyone; anyone
    else sees only the laborer record with their own name (their pay).
    A shop admin can also jump straight to one laborer's pay (laborer_id),
    same idea as a CFI's own Pay page."""
    start, end, period, label = _shop_period("this_week")
    admin_view = bool(session.get("is_master_admin") or session.get("shop_role") == "admin")
    laborer_id = request.args.get("laborer_id", type=int) if admin_view else None
    laborer_name = None
    conn = get_db()
    sql = """SELECT ls.*, l.name as laborer_name, l.rate as laborer_rate, pr.code as project_code,
                    pr.name as project_name, pr.id as project_id
             FROM labor_sessions ls
             JOIN laborers l ON l.id = ls.laborer_id
             LEFT JOIN projects pr ON pr.id = ls.project_id
             WHERE date(ls.started_at) BETWEEN ? AND ?"""
    params = [start, end]
    if laborer_id:
        sql += " AND l.id = ?"
        params.append(laborer_id)
        laborer_row = conn.execute("SELECT name FROM laborers WHERE id = ?", (laborer_id,)).fetchone()
        laborer_name = laborer_row["name"] if laborer_row else None
    elif not admin_view:
        sql += " AND lower(trim(l.name)) = lower(trim(?))"
        params.append(session.get("user_name") or "")
    sql += " ORDER BY l.name, ls.started_at"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    groups, order = {}, []
    for r in rows:
        g = groups.get(r["laborer_id"])
        if not g:
            g = groups[r["laborer_id"]] = {"laborer_id": r["laborer_id"], "name": r["laborer_name"],
                                            "rate": r["laborer_rate"], "hours": 0.0, "pay": 0.0,
                                            "sessions": [], "running": 0, "days": [], "tasks": []}
            g["_days"], g["_tasks"] = {}, {}
            order.append(r["laborer_id"])
        g["sessions"].append(r)
        # Day breakdown: every stretch of time a worker clocked on one day,
        # in order (e.g. General Shop 7:00-9:15, then 26-001 Engine
        # 9:15-12:00 after they scanned that task mid-day).
        day_key = (r["started_at"] or "")[:10]
        d = g["_days"].get(day_key)
        if not d:
            d = g["_days"][day_key] = {"date": r["started_at"], "first": r["started_at"], "last": r["ended_at"],
                                       "hours": 0.0, "pay": 0.0, "segments": [], "running": False}
            g["days"].append(d)
        d["segments"].append(r)
        if r["ended_at"]:
            d["hours"] += r["hours"] or 0
            d["pay"] += r["cost"] or 0
            if not d["running"] and (not d["last"] or r["ended_at"] > d["last"]):
                d["last"] = r["ended_at"]
        else:
            d["running"] = True
            d["last"] = None
        # Cost by task (shown to shop admins only): what this worker's time
        # on each project / sub-area cost over the period.
        if r["ended_at"]:
            tkey = (r["project_id"], r["section"] or "")
            t = g["_tasks"].get(tkey)
            if not t:
                t = g["_tasks"][tkey] = {"project_id": r["project_id"], "project_code": r["project_code"],
                                         "project_name": r["project_name"], "section": r["section"],
                                         "hours": 0.0, "cost": 0.0, "stretches": 0}
                g["tasks"].append(t)
            t["hours"] += r["hours"] or 0
            t["cost"] += r["cost"] or 0
            t["stretches"] += 1
        if r["ended_at"]:
            g["hours"] += r["hours"] or 0
            g["pay"] += r["cost"] or 0
        else:
            g["running"] += 1
    laborers_pay = [groups[i] for i in order]
    for g in laborers_pay:
        g.pop("_days", None)
        g.pop("_tasks", None)
        g["tasks"].sort(key=lambda t: -t["cost"])
    return render_template("shop_pay.html", laborers_pay=laborers_pay, admin_view=admin_view,
                           total_hours=sum(g["hours"] for g in laborers_pay),
                           total_pay=sum(g["pay"] for g in laborers_pay),
                           periods=SHOP_PERIODS, period=period, period_label=label, start=start, end=end,
                           laborer_id=laborer_id, laborer_name=laborer_name)


def _project_all_time_total(conn, project_id):
    """This project's all-time billed total (parts at sell price + clocked
    labor) - same math as Billing's per-project total, just with no date
    filter. Used for the "unpaid job" warning on Mark Completed."""
    parts_total = sum(u["qty"] * u["sell_price"] for u in _shop_parts_usage(conn, "2000-01-01", "2100-01-01", project_id))
    labor_total = conn.execute("""SELECT COALESCE(SUM(cost), 0) c FROM labor_sessions
                                  WHERE project_id = ? AND ended_at IS NOT NULL""", (project_id,)).fetchone()["c"]
    return parts_total + (labor_total or 0)


PAYMENT_METHODS = ("Cash", "Card", "Check", "Venmo/Zelle", "Other")


@app.route("/shop/billing")
@shop_role_required('admin')
def shop_billing():
    """Billing: every project with labor or parts in the period - parts at
    sell price plus clocked labor - with a link to its customer invoice
    CSV. Totals across the top, plus each job's payment status (QA
    feat-shop-job-payments) and an "Owed to the shop" total for whatever's
    been invoiced in this period but not yet marked paid."""
    start, end, period, label = _shop_period("this_month")
    unpaid_only = request.args.get("unpaid") == "1"
    conn = get_db()
    projects = {}

    def proj(pid):
        if pid not in projects:
            p = conn.execute("""SELECT pr.id, pr.code, pr.name, pr.status, pr.payment_status,
                                       pr.invoiced_at, pr.paid_at, pr.paid_method, pr.card_last4, a.tag as asset_tag
                                FROM projects pr LEFT JOIN assets a ON a.id = pr.asset_id WHERE pr.id = ?""",
                             (pid,)).fetchone()
            projects[pid] = {"id": pid, "code": p["code"] if p else "?", "name": p["name"] if p else "(deleted)",
                             "status": p["status"] if p else "", "asset_tag": p["asset_tag"] if p else None,
                             "payment_status": (p["payment_status"] if p else None) or "not_invoiced",
                             "invoiced_at": p["invoiced_at"] if p else None, "paid_at": p["paid_at"] if p else None,
                             "paid_method": p["paid_method"] if p else None, "card_last4": p["card_last4"] if p else None,
                             "labor_hours": 0.0, "labor": 0.0, "parts": 0.0, "parts_cost": 0.0, "parts_count": 0}
        return projects[pid]

    for r in conn.execute("""SELECT project_id, SUM(hours) h, SUM(cost) c FROM labor_sessions
                             WHERE ended_at IS NOT NULL AND project_id IS NOT NULL
                               AND date(started_at) BETWEEN ? AND ?
                             GROUP BY project_id""", (start, end)).fetchall():
        p = proj(r["project_id"])
        p["labor_hours"] = r["h"] or 0
        p["labor"] = r["c"] or 0
    for u in _shop_parts_usage(conn, start, end):
        p = proj(u["project_id"])
        p["parts"] += u["qty"] * u["sell_price"]
        p["parts_cost"] += u["qty"] * u["unit_cost"]
        p["parts_count"] += 1
    wave_by_project = wave_billing.latest_for(conn, "project", projects.keys())
    for pid, p in projects.items():
        p["wave"] = wave_by_project.get(pid)
        p["customer_name"], p["customer_email"] = _project_customer(conn, pid)
    wave_accounts, wave_default = wave_billing.program_accounts(conn, "shop")
    conn.close()
    rows = sorted(projects.values(), key=lambda p: p["code"] or "")
    for p in rows:
        p["total"] = p["labor"] + p["parts"]
        p["invoiced_days_ago"] = (date.today() - datetime.strptime(p["invoiced_at"][:10], "%Y-%m-%d").date()).days \
            if p["invoiced_at"] else None
    totals = {k: sum(p[k] for p in rows) for k in ("labor_hours", "labor", "parts", "parts_cost", "total")}
    owed_rows = [p for p in rows if p["payment_status"] == "invoiced"]
    owed = {"total": sum(p["total"] for p in owed_rows), "jobs": len(owed_rows),
            "over_30": sum(1 for p in owed_rows if (p["invoiced_days_ago"] or 0) > 30)}
    if unpaid_only:
        rows = [p for p in rows if p["payment_status"] != "paid"]
    return render_template("shop_billing.html", projects=rows, totals=totals, owed=owed, unpaid_only=unpaid_only,
                           payment_methods=PAYMENT_METHODS, wave_connected=bool(wave_accounts),
                           wave_accounts=wave_accounts, wave_default=wave_default,
                           periods=SHOP_PERIODS, period=period, period_label=label, start=start, end=end)


@app.route("/shop/billing/<int:project_id>/mark-invoiced", methods=["POST"])
@shop_role_required('admin')
def project_mark_invoiced(project_id):
    conn = get_db()
    project = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    conn.execute("UPDATE projects SET payment_status = 'invoiced', invoiced_at = ?, invoiced_by = ? WHERE id = ?",
                 (now_iso(), session.get("user_name"), project_id))
    conn.commit()
    conn.close()
    flash("Marked invoiced.", "success")
    return redirect(request.referrer or url_for("shop_billing"))


@app.route("/shop/billing/<int:project_id>/mark-paid", methods=["POST"])
@shop_role_required('admin')
def project_mark_paid(project_id):
    conn = get_db()
    project = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    method = request.form.get("paid_method", "").strip()
    if method not in PAYMENT_METHODS:
        conn.close()
        flash("Pick how it was paid.", "danger")
        return redirect(request.referrer or url_for("shop_billing"))
    conn.execute("""UPDATE projects SET payment_status = 'paid', paid_at = ?, paid_by = ?, paid_method = ?
                    WHERE id = ?""", (now_iso(), session.get("user_name"), method, project_id))
    conn.commit()
    conn.close()
    flash("Marked paid.", "success")
    return redirect(request.referrer or url_for("shop_billing"))


@app.route("/shop/billing/<int:project_id>/pay-card", methods=["POST"])
@shop_role_required('admin')
def project_pay_card(project_id):
    """Idea "Credit card": a simulated Stripe-style card charge so Frank can
    see how a "Pay with Card" flow would feel - no real Stripe account, no
    network call, no real charge. Only the card's last 4 digits are kept,
    alongside a fake charge id, next to the usual Mark Paid fields."""
    conn = get_db()
    project = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    card_number = re.sub(r"\D", "", request.form.get("card_number", ""))
    if len(card_number) < 4:
        conn.close()
        flash("Enter a card number to simulate the charge.", "danger")
        return redirect(request.referrer or url_for("shop_billing"))
    last4 = card_number[-4:]
    charge_id = "sim_ch_" + secrets.token_hex(8)
    conn.execute("""UPDATE projects SET payment_status = 'paid', paid_at = ?, paid_by = ?, paid_method = 'Card',
                    card_last4 = ?, card_charge_id = ? WHERE id = ?""",
                 (now_iso(), session.get("user_name"), last4, charge_id, project_id))
    conn.commit()
    conn.close()
    flash(f"Card charged (simulated) - ending in {last4}, receipt {charge_id}.", "success")
    return redirect(request.referrer or url_for("shop_billing"))


# ---------------------------------------------------------------------------
# Wave invoicing (see wave_billing.py): a real invoice in Wave for a job,
# emailed by Wave with its own Pay now link, and a payment check that
# flips the job to Paid once the customer pays in Wave.
# ---------------------------------------------------------------------------

def _project_customer(conn, project_id):
    """(name, email) to bill for a job: the first My Aircraft customer
    linked to its plane, else the plane's Owner text with no email."""
    row = conn.execute("""SELECT c.name, c.email FROM projects pr
                          JOIN customer_assets ca ON ca.asset_id = pr.asset_id
                          JOIN customers c ON c.id = ca.customer_id
                          WHERE pr.id = ? AND c.active = 1 ORDER BY c.id LIMIT 1""", (project_id,)).fetchone()
    if row:
        return row["name"], row["email"]
    row = conn.execute("""SELECT a.owner FROM projects pr JOIN assets a ON a.id = pr.asset_id
                          WHERE pr.id = ?""", (project_id,)).fetchone()
    return ((row["owner"] or "") if row else ""), ""


@app.route("/shop/billing/<int:project_id>/wave-invoice", methods=["POST"])
@shop_role_required('admin')
def project_wave_invoice(project_id):
    """Makes the job's invoice in Wave (whole job, all dates - same lines
    as the Invoice CSV), optionally has Wave email it, and marks the job
    Invoiced here."""
    back = request.referrer or url_for("shop_billing")
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    existing = wave_billing.latest_for(conn, "project", [project_id]).get(project_id)
    if existing:
        conn.close()
        flash(f"This job already has Wave invoice #{existing['invoice_number'] or ''}.", "warning")
        return redirect(back)
    name = request.form.get("customer_name", "").strip()
    email = request.form.get("customer_email", "").strip()
    send = request.form.get("send") == "1"
    if not name:
        conn.close()
        flash("Enter who the invoice is for.", "danger")
        return redirect(back)
    if send and not email:
        conn.close()
        flash("Enter the customer's email so Wave can send it (or untick Email it now).", "danger")
        return redirect(back)
    try:
        cfg = wave_billing.choose_account(conn, "shop", request.form.get("wave_account"))
        lines = wave_billing.project_lines(conn, cfg, project_id)
        memo = f"{project['code']} - {project['name']}"
        inv = wave_billing.create_invoice(conn, cfg, name, email, lines, memo=memo, po_number=project["code"] or "")
    except wave_billing.WaveError as e:
        conn.close()
        flash(str(e), "danger")
        return redirect(back)
    sent_msg, sent = "", False
    if send:
        try:
            wave_billing.send_invoice(cfg, inv["id"], email)
            sent, sent_msg = True, f" and emailed to {email}"
        except wave_billing.WaveError as e:
            sent_msg = f", but it wasn't emailed ({e}) - send it from Wave"
    wave_billing.record_invoice(conn, "project", project_id, inv, name, email, session.get("user_name"), cfg, sent=sent)
    if (project["payment_status"] or "not_invoiced") == "not_invoiced":
        conn.execute("UPDATE projects SET payment_status = 'invoiced', invoiced_at = ?, invoiced_by = ? WHERE id = ?",
                     (now_iso(), session.get("user_name"), project_id))
    conn.commit()
    conn.close()
    flash(f"Wave invoice #{inv.get('invoiceNumber') or ''} created in {cfg['name']} for "
          f"${wave_billing.money(inv.get('total')):.2f}{sent_msg}.",
          "success" if sent or not send else "warning")
    return redirect(back)


@app.route("/shop/billing/wave-sync", methods=["POST"])
@shop_role_required('admin')
def shop_wave_sync():
    """Check Wave for payments now (it's also checked in the background
    every half hour - see _start_session_alert_loop)."""
    return _wave_sync_and_report(request.referrer or url_for("shop_billing"))


def _wave_sync_and_report(back):
    conn = get_db()
    try:
        checked, paid, errors = wave_billing.sync_open_invoices(conn, wave_billing.apply_paid, program="shop")
    except wave_billing.WaveError as e:
        conn.close()
        flash(str(e), "danger")
        return redirect(back)
    conn.close()
    msg = f"Checked {checked} open Wave invoice{'s' if checked != 1 else ''}"
    msg += f" - {paid} newly paid." if paid else " - no new payments."
    flash(msg, "success")
    for e in errors[:3]:
        flash("Wave " + e, "warning")
    return redirect(back)


WAVE_SYNC_EVERY_SECONDS = 30 * 60
_wave_last_sync = [0.0]


def run_wave_payment_check():
    """Background: every half hour, ask Wave about unpaid invoices and mark
    newly paid jobs/flights paid. Quietly does nothing until Wave is set up
    or when nothing is waiting on a payment."""
    if time.time() - _wave_last_sync[0] < WAVE_SYNC_EVERY_SECONDS:
        return
    _wave_last_sync[0] = time.time()
    conn = get_db()
    try:
        if not wave_billing.has_any_connected(conn):
            return
        if not conn.execute("SELECT 1 FROM wave_invoices WHERE paid_applied_at IS NULL LIMIT 1").fetchone():
            return
        wave_billing.sync_open_invoices(conn, wave_billing.apply_paid)
    finally:
        conn.close()


@app.route("/shop/stats")
@shop_role_required('admin')
def shop_stats():
    """Stats: labor hours/cost, parts used (billed value, cost and margin),
    projects worked, plus hours by laborer and aircraft and the most-used
    parts, for the chosen period."""
    start, end, period, label = _shop_period("this_month")
    conn = get_db()
    labor = conn.execute("""SELECT COUNT(*) n, COALESCE(SUM(hours), 0) h, COALESCE(SUM(cost), 0) c,
                                   COUNT(DISTINCT project_id) projects, COUNT(DISTINCT laborer_id) people
                            FROM labor_sessions WHERE ended_at IS NOT NULL AND date(started_at) BETWEEN ? AND ?""",
                         (start, end)).fetchone()
    usage = _shop_parts_usage(conn, start, end)
    parts_sale = sum(u["qty"] * u["sell_price"] for u in usage)
    parts_cost = sum(u["qty"] * u["unit_cost"] for u in usage)
    top = {}
    for u in usage:
        t = top.setdefault(u["part_id"], {"name": u["name"], "unit": u["unit"], "qty": 0.0, "value": 0.0})
        t["qty"] += u["qty"]
        t["value"] += u["qty"] * u["sell_price"]
    top_parts = sorted(top.values(), key=lambda t: -t["qty"])[:10]
    by_laborer = conn.execute("""SELECT l.name, SUM(ls.hours) h, SUM(ls.cost) c FROM labor_sessions ls
                                 JOIN laborers l ON l.id = ls.laborer_id
                                 WHERE ls.ended_at IS NOT NULL AND date(ls.started_at) BETWEEN ? AND ?
                                 GROUP BY l.id ORDER BY h DESC""", (start, end)).fetchall()
    by_aircraft = conn.execute("""SELECT COALESCE(a.tag, 'No aircraft') tag, SUM(ls.hours) h, COUNT(DISTINCT pr.id) projects
                                  FROM labor_sessions ls LEFT JOIN projects pr ON pr.id = ls.project_id
                                  LEFT JOIN assets a ON a.id = pr.asset_id
                                  WHERE ls.ended_at IS NOT NULL AND date(ls.started_at) BETWEEN ? AND ?
                                  GROUP BY a.id ORDER BY h DESC""", (start, end)).fetchall()
    projects_worked = len({u["project_id"] for u in usage} | {r["project_id"] for r in conn.execute(
        "SELECT DISTINCT project_id FROM labor_sessions WHERE date(started_at) BETWEEN ? AND ?", (start, end)).fetchall()})
    conn.close()
    max_h = max([r["h"] or 0 for r in by_laborer] + [r["h"] or 0 for r in by_aircraft] + [0.0001])
    return render_template("shop_stats.html", labor=labor, parts_sale=parts_sale, parts_cost=parts_cost,
                           top_parts=top_parts, by_laborer=by_laborer, by_aircraft=by_aircraft,
                           projects_worked=projects_worked, max_h=max_h,
                           periods=SHOP_PERIODS, period=period, period_label=label, start=start, end=end)


# ---------------------------------------------------------------------------
# Calibrated Tools (QA feat-tool-calibration): torque wrenches, pressure
# gauges and testers that need periodic recalibration, tracked with a due
# date, calibration history and certificate. A tool with no interval set
# ("not required") never shows a due date or a dashboard reminder.
# ---------------------------------------------------------------------------

ALLOWED_TOOL_CERT_EXT = {"pdf", "jpg", "jpeg", "png", "webp"}


def _allowed_tool_cert(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_TOOL_CERT_EXT


def _tool_status(next_due_date):
    """(urgency, label) for a tool's next_due_date - None if calibration
    isn't required or it's never been calibrated yet."""
    if not next_due_date:
        return None
    due = datetime.strptime(next_due_date[:10], "%Y-%m-%d").date()
    delta = (due - date.today()).days
    if delta < 0:
        return {"urgency": "overdue", "label": f"Overdue {abs(delta)} day{'s' if abs(delta) != 1 else ''}"}
    if delta <= 30:
        return {"urgency": "due_soon", "label": "Due today" if delta == 0 else f"Due in {delta} day{'s' if delta != 1 else ''}"}
    return {"urgency": "ok", "label": "OK"}


def _tools_due_reminders(conn):
    """Tools overdue or due within 30 days, for the Maintenance dashboard -
    same "quiet unless something needs attention" idea as
    _fleet_maintenance_reminders, just for tools instead of aircraft."""
    tools = conn.execute("""SELECT * FROM shop_tools WHERE deleted_at IS NULL AND active = 1
                            AND calibration_interval_days IS NOT NULL AND next_due_date IS NOT NULL
                            ORDER BY next_due_date""").fetchall()
    out = []
    for t in tools:
        st = _tool_status(t["next_due_date"])
        if st and st["urgency"] in ("overdue", "due_soon"):
            out.append({"tool": t, "status": st})
    return out


@app.route("/shop/tools")
@shop_role_required('admin', 'tech', 'inspector')
def tools_list():
    conn = get_db()
    tools = conn.execute("SELECT * FROM shop_tools WHERE deleted_at IS NULL ORDER BY name").fetchall()
    conn.close()
    rows = []
    overdue_count = due_soon_count = 0
    for t in tools:
        st = _tool_status(t["next_due_date"])
        if st and st["urgency"] == "overdue":
            overdue_count += 1
        elif st and st["urgency"] == "due_soon":
            due_soon_count += 1
        rows.append({"tool": t, "status": st})
    return render_template("shop_tools.html", rows=rows, overdue_count=overdue_count, due_soon_count=due_soon_count)


@app.route("/shop/tools/new", methods=["GET", "POST"])
@shop_role_required('admin')
def tool_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "danger")
            return redirect(url_for("tool_new"))
        serial = request.form.get("serial", "").strip() or None
        location = request.form.get("location", "").strip() or None
        interval_raw = request.form.get("calibration_interval_days", "").strip()
        interval = int(interval_raw) if interval_raw.isdigit() and int(interval_raw) > 0 else None
        conn = get_db()
        conn.execute("""INSERT INTO shop_tools (name, serial, location, calibration_interval_days, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)""", (name, serial, location, interval, now_iso(), now_iso()))
        conn.commit()
        conn.close()
        flash(f"{name} added.", "success")
        return redirect(url_for("tools_list"))
    return render_template("tool_form.html", tool=None)


@app.route("/shop/tools/<int:tool_id>/edit", methods=["GET", "POST"])
@shop_role_required('admin')
def tool_edit(tool_id):
    conn = get_db()
    tool = conn.execute("SELECT * FROM shop_tools WHERE id = ? AND deleted_at IS NULL", (tool_id,)).fetchone()
    if not tool:
        conn.close()
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            conn.close()
            flash("Name is required.", "danger")
            return redirect(url_for("tool_edit", tool_id=tool_id))
        serial = request.form.get("serial", "").strip() or None
        location = request.form.get("location", "").strip() or None
        interval_raw = request.form.get("calibration_interval_days", "").strip()
        interval = int(interval_raw) if interval_raw.isdigit() and int(interval_raw) > 0 else None
        # Changing the interval (or clearing it) re-figures Next Due off the
        # same Last Calibrated date, so it doesn't need a fresh calibration
        # just because the interval was corrected.
        next_due = None
        if interval and tool["last_calibrated_date"]:
            next_due = (datetime.strptime(tool["last_calibrated_date"][:10], "%Y-%m-%d").date()
                        + timedelta(days=interval)).isoformat()
        conn.execute("""UPDATE shop_tools SET name = ?, serial = ?, location = ?, calibration_interval_days = ?,
                        next_due_date = ?, updated_at = ? WHERE id = ?""",
                     (name, serial, location, interval, next_due, now_iso(), tool_id))
        conn.commit()
        conn.close()
        flash("Saved.", "success")
        return redirect(url_for("tools_list"))
    conn.close()
    return render_template("tool_form.html", tool=tool)


@app.route("/shop/tools/<int:tool_id>/delete", methods=["POST"])
@shop_role_required('admin')
def tool_delete(tool_id):
    conn = get_db()
    tool = conn.execute("SELECT name FROM shop_tools WHERE id = ?", (tool_id,)).fetchone()
    if not tool:
        conn.close()
        abort(404)
    conn.execute("UPDATE shop_tools SET deleted_at = ? WHERE id = ?", (now_iso(), tool_id))
    conn.commit()
    conn.close()
    flash(f"{tool['name']} removed.", "success")
    return redirect(url_for("tools_list"))


@app.route("/shop/tools/<int:tool_id>/calibrate", methods=["POST"])
@shop_role_required('admin', 'tech', 'inspector')
def tool_mark_calibrated(tool_id):
    conn = get_db()
    tool = conn.execute("SELECT * FROM shop_tools WHERE id = ? AND deleted_at IS NULL", (tool_id,)).fetchone()
    if not tool:
        conn.close()
        abort(404)
    if not tool["calibration_interval_days"]:
        conn.close()
        flash("This tool isn't set up to require calibration - edit it to add an interval first.", "danger")
        return redirect(url_for("tools_list"))
    calibrated_at = request.form.get("calibrated_at", "").strip() or date.today().isoformat()
    try:
        datetime.strptime(calibrated_at, "%Y-%m-%d")
    except ValueError:
        calibrated_at = date.today().isoformat()
    cert_file = None
    up = request.files.get("cert")
    if up and up.filename:
        if not _allowed_tool_cert(up.filename):
            conn.close()
            flash("Certificate must be a PDF, JPG or PNG.", "danger")
            return redirect(url_for("tools_list"))
        cert_file = save_upload(up)
    next_due = (datetime.strptime(calibrated_at, "%Y-%m-%d").date()
                + timedelta(days=tool["calibration_interval_days"])).isoformat()
    conn.execute("""INSERT INTO tool_calibrations (tool_id, calibrated_at, cert_file, performed_by, note, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                 (tool_id, calibrated_at, cert_file, session.get("user_name"),
                  request.form.get("note", "").strip() or None, now_iso()))
    conn.execute("""UPDATE shop_tools SET last_calibrated_date = ?, next_due_date = ?,
                    cert_file = COALESCE(?, cert_file), updated_at = ? WHERE id = ?""",
                 (calibrated_at, next_due, cert_file, now_iso(), tool_id))
    conn.commit()
    conn.close()
    flash(f"{tool['name']} marked calibrated.", "success")
    return redirect(url_for("tools_list"))


@app.route("/api/labor/task_lookup")
@login_required
def api_labor_task_lookup():
    """Resolves a scanned TASK- QR code (see project_labor_codes.html) to the
    project + section it identifies, without starting anything."""
    code = request.args.get("code", "").strip()
    # Prefix checked case-blind: Caps Lock on a USB scanner sends "task-".
    if not code.upper().startswith("TASK-"):
        return jsonify({"found": False})
    rest = code[len("TASK-"):]
    if "::" in rest:
        project_code, section = rest.split("::", 1)
    else:
        project_code, section = rest, ""
    project_code = project_code.strip()
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE code = ? COLLATE NOCASE AND deleted_at IS NULL",
                           (project_code,)).fetchone()
    if project and section:
        # Same Caps Lock case: "bRAKES" should still land on the "Brakes" area.
        known = conn.execute("SELECT name FROM project_sections WHERE project_id = ? AND name = ? COLLATE NOCASE",
                             (project["id"], section)).fetchone()
        if known:
            section = known["name"]
    conn.close()
    if not project:
        return jsonify({"found": False})
    return jsonify({"found": True, "project_id": project["id"], "project_code": project["code"],
                     "project_name": project["name"], "section": section})


# Jobs that can't take new labor (reopen them first) - see api_labor_scan.
_LABOR_CLOSED_STATUSES = ("completed", "archived")


@app.route("/api/labor/scan", methods=["POST"])
@shop_role_required('admin', 'tech', 'apprentice', 'inspector')
def api_labor_scan():
    """One scan does double duty: if this laborer has no open timer, this
    starts one against the given task; if they already have one running
    (on any task), this same scan ends it and records the hours/cost.
    Changes a worker's hours, pay and a job's labor bill, so it needs a Shop
    role (like /api/scan) - login alone let flight-only accounts clock people."""
    # A garbled body (not a JSON object, or fields of the wrong type) gets a
    # plain 400, same as /api/scan - never a crash in the error log.
    data = request.get_json(force=True, silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "bad_request"}), 400
    for field in ("code", "section", "note"):
        if data.get(field) is not None and not isinstance(data[field], str):
            return jsonify({"ok": False, "error": "bad_request"}), 400
    project_id = data.get("project_id")
    if project_id in ("", None):
        project_id = None
    elif isinstance(project_id, bool) or not isinstance(project_id, (int, str)):
        return jsonify({"ok": False, "error": "bad_request"}), 400
    else:
        try:
            project_id = int(project_id)
        except ValueError:
            project_id = -1  # text that isn't a job id: matches no job -> "unknown_project" below
    code = (data.get("code") or "").strip()
    section = (data.get("section") or "").strip() or None
    general = data.get("general") is True
    note = (data.get("note") or "").strip() or None
    # Badge codes are always upper case (db.gen_labor_code); a USB scanner
    # with Caps Lock on sends them lower case, so match case-blind.
    if not code.upper().startswith("LABOR-"):
        return jsonify({"ok": False, "error": "not_a_laborer_code"}), 400

    conn = get_db()
    laborer = conn.execute("SELECT * FROM laborers WHERE code = ?", (code,)).fetchone() or \
        conn.execute("SELECT * FROM laborers WHERE code = ? COLLATE NOCASE", (code,)).fetchone()
    if not laborer:
        conn.close()
        return jsonify({"ok": False, "error": "unknown_laborer", "code": code}), 404

    open_session = conn.execute(
        "SELECT * FROM labor_sessions WHERE laborer_id = ? AND ended_at IS NULL", (laborer["id"],)
    ).fetchone()

    # An inactive worker can't START a timer, but their badge still clocks
    # them OUT of one that was already running when they were deactivated -
    # otherwise their hours and pay keep growing until someone notices.
    if not laborer["active"] and not open_session:
        conn.close()
        return jsonify({"ok": False, "error": "inactive_laborer", "name": laborer["name"]}), 400

    # Switching tasks mid-day: a worker who's already clocked in (e.g. on
    # General Shop at the start of the day) scans a DIFFERENT project/task
    # code and then their badge. Instead of clocking them out, the time so
    # far is closed off on what they were doing and a new stretch starts on
    # the task they just scanned - the day keeps running. The Scan page only
    # asks for this (switch=true) right after a task/project/General code
    # was scanned, so a plain badge scan still clocks out as before.
    if open_session and data.get("switch") is True:
        target_project_id = None
        target_section = None
        if not general:
            try:
                target_project_id = int(project_id) if project_id not in (None, "") else None
            except (TypeError, ValueError):
                target_project_id = None
            target_section = section
        same_target = (open_session["project_id"] == target_project_id
                       and (open_session["section"] or None) == (target_section or None))
        # (An inactive worker's badge just clocks them out - see above.)
        if (general or target_project_id) and not same_target and laborer["active"]:
            target = None
            if target_project_id:
                target = conn.execute("SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL",
                                      (target_project_id,)).fetchone()
                if not target:
                    conn.close()
                    return jsonify({"ok": False, "error": "unknown_project"}), 404
                if target["status"] in _LABOR_CLOSED_STATUSES:
                    conn.close()
                    return jsonify({"ok": False, "error": "project_closed", "project_code": target["code"],
                                    "status": target["status"]}), 400
            switch_at = now_iso()
            started = datetime.strptime(open_session["started_at"], "%Y-%m-%d %H:%M:%S")
            ended = datetime.strptime(switch_at, "%Y-%m-%d %H:%M:%S")
            hours = max((ended - started).total_seconds() / 3600.0, 0)
            cost = hours * (open_session["rate"] or 0)
            # Only end it if it's still open - a double scan can't split twice.
            closed = conn.execute(
                "UPDATE labor_sessions SET ended_at = ?, hours = ?, cost = ?, note = COALESCE(?, note) "
                "WHERE id = ? AND ended_at IS NULL",
                (switch_at, hours, cost, note, open_session["id"])).rowcount
            if not closed:
                conn.close()
                return jsonify({"ok": False, "error": "already_switched"}), 409
            conn.execute("""INSERT INTO labor_sessions (laborer_id, project_id, section, started_at, rate, created_at)
                             VALUES (?, ?, ?, ?, ?, ?)""",
                         (laborer["id"], target_project_id, target_section, switch_at, laborer["rate"], now_iso()))
            conn.commit()
            prev = conn.execute("SELECT code FROM projects WHERE id = ?", (open_session["project_id"],)).fetchone()
            conn.close()
            return jsonify({"ok": True, "action": "switch", "laborer": laborer["name"],
                            "hours": round(hours, 2), "cost": round(cost, 2),
                            "from_project_code": prev["code"] if prev else None,
                            "from_general": open_session["project_id"] is None,
                            "from_section": open_session["section"] or "General",
                            "project_code": target["code"] if target else None,
                            "general": target is None,
                            "section": (target_section or "General") if target else "General Shop"})

    if open_session:
        # Clocking out of General Shop time is the one case that needs a
        # word from the person before it can complete - a quick "what did
        # you work on" note, since there's no project/task to tell that from
        # automatically. The scanner just holds - the timer keeps running -
        # until the note comes back with the same scan resubmitted.
        if open_session["project_id"] is None and not note:
            conn.close()
            return jsonify({"ok": False, "error": "note_required", "code": code,
                             "laborer": laborer["name"]}), 400
        started = datetime.strptime(open_session["started_at"], "%Y-%m-%d %H:%M:%S")
        hours = max((datetime.now() - started).total_seconds() / 3600.0, 0)
        cost = hours * (open_session["rate"] or 0)
        conn.execute("UPDATE labor_sessions SET ended_at = ?, hours = ?, cost = ?, note = ? WHERE id = ?",
                     (now_iso(), hours, cost, note, open_session["id"]))
        conn.commit()
        project = conn.execute("SELECT code, name FROM projects WHERE id = ?",
                                (open_session["project_id"],)).fetchone()
        conn.close()
        return jsonify({"ok": True, "action": "clock_out", "laborer": laborer["name"], "hours": round(hours, 2),
                         "cost": round(cost, 2), "project_code": project["code"] if project else None,
                         "general": project is None,
                         "section": open_session["section"] or "General"})

    if not project_id and not general:
        conn.close()
        return jsonify({"ok": False, "error": "no_task_selected", "code": code, "name": laborer["name"]}), 400

    if general:
        try:
            conn.execute("""INSERT INTO labor_sessions (laborer_id, project_id, section, started_at, rate, created_at)
                             VALUES (?, NULL, NULL, ?, ?, ?)""",
                         (laborer["id"], now_iso(), laborer["rate"], now_iso()))
            conn.commit()
        except sqlite3.IntegrityError:
            # Two clock-in scans arrived at the same instant (a double tap,
            # the scanner sending the code twice, two phones at once) - the
            # other one already started the timer a moment ago; this one
            # just reports that instead of starting a second timer too (see
            # idx_labor_sessions_one_open in schema.sql, QA fix
            # qa-labor-double-clock-in).
            conn.rollback()
            conn.close()
            return jsonify({"ok": True, "action": "already_clocked_in", "laborer": laborer["name"],
                             "project_code": None, "general": True, "section": "General Shop"})
        conn.close()
        return jsonify({"ok": True, "action": "clock_in", "laborer": laborer["name"],
                         "project_code": None, "general": True, "section": "General Shop"})

    project = conn.execute("SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)).fetchone()
    if not project:
        conn.close()
        return jsonify({"ok": False, "error": "unknown_project"}), 404
    if project["status"] in _LABOR_CLOSED_STATUSES:
        # Same rule as scanning parts out: a finished (and likely billed)
        # job takes no more labor until it's reopened.
        conn.close()
        return jsonify({"ok": False, "error": "project_closed", "project_code": project["code"],
                        "status": project["status"]}), 400
    try:
        conn.execute("""INSERT INTO labor_sessions (laborer_id, project_id, section, started_at, rate, created_at)
                         VALUES (?, ?, ?, ?, ?, ?)""",
                     (laborer["id"], project_id, section, now_iso(), laborer["rate"], now_iso()))
        conn.commit()
    except sqlite3.IntegrityError:
        # Same double-scan race as above, on a task/project code this time.
        conn.rollback()
        conn.close()
        return jsonify({"ok": True, "action": "already_clocked_in", "laborer": laborer["name"],
                         "project_code": project["code"], "general": False, "section": section or "General"})
    conn.close()
    return jsonify({"ok": True, "action": "clock_in", "laborer": laborer["name"],
                     "project_code": project["code"], "general": False, "section": section or "General"})


@app.route("/api/labor/clocked-in")
@login_required
def api_labor_clocked_in():
    """Everyone clocked in right now, for the floating "who's working"
    timer on every shop/admin page (_labor_timer_widget.html). Shop roles
    and master admins only - flight-only accounts get an empty list rather
    than an error so the widget just stays hidden. elapsed_seconds is
    worked out here (not from started_at in the browser) so the phone's
    clock/time zone can't skew it."""
    if not (session.get("is_master_admin") or session.get("shop_role")):
        return jsonify({"ok": True, "workers": []})
    conn = get_db()
    rows = conn.execute("""
        SELECT ls.id, ls.started_at, ls.section, ls.project_id, l.name AS laborer_name,
               p.code AS project_code, p.name AS project_name
        FROM labor_sessions ls
        JOIN laborers l ON l.id = ls.laborer_id
        LEFT JOIN projects p ON p.id = ls.project_id
        WHERE ls.ended_at IS NULL
        ORDER BY l.name COLLATE NOCASE
    """).fetchall()
    conn.close()
    now = datetime.now()
    workers = []
    for r in rows:
        try:
            started = datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S")
            elapsed = max(int((now - started).total_seconds()), 0)
        except (TypeError, ValueError):
            elapsed = 0
        workers.append({
            "id": r["id"],
            "name": r["laborer_name"],
            "project_id": r["project_id"],
            "project_code": r["project_code"],
            "project_name": r["project_name"],
            "general": r["project_id"] is None,
            "section": r["section"] or "",
            "elapsed_seconds": elapsed,
            "url": url_for("project_detail", project_id=r["project_id"]) if r["project_id"] else None,
        })
    return jsonify({"ok": True, "workers": workers})


@app.route("/api/labor/stop/<int:session_id>", methods=["POST"])
@shop_role_required('admin', 'tech', 'apprentice', 'inspector')
def api_labor_stop(session_id):
    """Manual fallback next to the open-timers list, for when re-scanning
    isn't handy. Shop roles only, same as the badge scan."""
    conn = get_db()
    session_row = conn.execute(
        "SELECT * FROM labor_sessions WHERE id = ? AND ended_at IS NULL", (session_id,)
    ).fetchone()
    if not session_row:
        conn.close()
        return jsonify({"ok": False, "error": "not_found_or_already_stopped"}), 404
    started = datetime.strptime(session_row["started_at"], "%Y-%m-%d %H:%M:%S")
    hours = max((datetime.now() - started).total_seconds() / 3600.0, 0)
    cost = hours * (session_row["rate"] or 0)
    conn.execute("UPDATE labor_sessions SET ended_at = ?, hours = ?, cost = ? WHERE id = ?",
                 (now_iso(), hours, cost, session_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "hours": round(hours, 2), "cost": round(cost, 2)})


# ---------------------------------------------------------------------------
# Master account management (Shop Inventory + Flight School roles together)
# ---------------------------------------------------------------------------

from werkzeug.security import generate_password_hash
from db import ensure_flight_profile


@app.route("/admin")
@master_admin_required
def admin_home():
    """A single, neutrally-themed admin landing page that every 'Admin' link
    in both the Maintenance and Flight School navbars now points to, instead
    of dropping straight into a shop-styled or flight-styled admin screen.
    The dropdowns themselves are unchanged - this is just where they land."""
    conn = get_db()
    user_count = conn.execute("SELECT COUNT(*) c FROM users WHERE active = 1").fetchone()["c"]
    error_log_count = 0
    if os.path.isdir(ERROR_LOG_DIR):
        error_log_count = len([n for n in os.listdir(ERROR_LOG_DIR) if n.endswith(".log")])
    conn.close()
    return render_template("admin_home.html", user_count=user_count, error_log_count=error_log_count)


@app.route("/view-as/<program>/<level>", methods=["POST"])
def view_as_start(program, level):
    """Switches to another role from the "View as" chips in the header -
    any Shop or Flight School level for a master admin, or one of their own
    other roles for an account that holds several (Admin + Inspector, CFI +
    Student, ...). It isn't read-only: everything works as it would for that
    role. Clicking your normal role's chip (Admin for a master admin) goes
    straight back to your normal view. A master admin can also view My
    Aircraft as one specific real owner, picked on Admin > Customers (see
    auth.start_view_as for how each is backed)."""
    if program not in ("shop", "flight", "owner"):
        abort(404)
    if program != "owner" and level == home_view_as_level(program):
        return view_as_exit()
    conn = get_db()
    ok, error = start_view_as(conn, program, level)
    conn.close()
    if not ok:
        flash(error or "Can't view as that.", "danger")
        return redirect(request.referrer or url_for("dashboard"))
    if program == "owner":
        flash(f"Viewing as {session.get('customer_name')} (owner). Nothing you do here affects real data any differently than it would for that owner.", "info")
        return redirect(url_for("customer.customer_dashboard"))
    levels = SHOP_VIEW_AS_LEVELS if program == "shop" else FLIGHT_VIEW_AS_LEVELS
    flash(f"Viewing as {levels[level]} - you can use everything a{'n' if levels[level][0] in 'AEIOU' else ''} {levels[level]} can.", "info")
    return redirect(url_for("dashboard") if program == "shop" else url_for("flight.dashboard"))


@app.route("/view-as/people")
def view_as_people():
    """Admins only: everyone who can log in, to see the app exactly as that
    person does (view only) - opened from "View as someone" in the account
    menu. Your own row is first: it's your normal view."""
    if not can_view_as_person():
        flash("That's for admins only.", "danger")
        return redirect(url_for("home_launcher"))
    conn = get_db()
    users = conn.execute("SELECT * FROM users WHERE active = 1 ORDER BY name COLLATE NOCASE").fetchall()
    user_emails = {str(u["username"] or "").lower() for u in users}
    owners = [c for c in conn.execute("SELECT id, name, email FROM customers WHERE active = 1 ORDER BY name COLLATE NOCASE").fetchall()
              if str(c["email"] or "").lower() not in user_emails]
    conn.close()
    me = (session.get("_person_view_real") or session).get("user_id")
    people = []
    for u in users:
        roles = ["Admin"] if u["is_master_admin"] else []
        roles += [SHOP_VIEW_AS_LEVELS.get(r, r.title()) for r in user_shop_roles(u)]
        roles += [FLIGHT_VIEW_AS_LEVELS.get(r, r.title()) for r in user_flight_roles(u)]
        if u["academy_access"] and not u["is_master_admin"]:
            roles.append("Academy")
        people.append(dict(id=u["id"], name=u["name"], username=u["username"], roles=list(dict.fromkeys(roles)),
                           me=u["id"] == me))
    people.sort(key=lambda p: not p["me"])
    return render_template("view_as_people.html", people=people, owners=owners,
                           current_person=person_view_name())


@app.route("/view-as/person/<int:user_id>", methods=["POST"])
def view_as_person_start(user_id):
    conn = get_db()
    ok, error = start_view_as_person(conn, user_id)
    conn.close()
    if not ok:
        flash(error or "Can't view as that person.", "danger")
        return redirect(request.referrer or url_for("home_launcher"))
    if person_view_active():
        flash(f"Viewing {person_view_name()}'s app exactly as they see it. It's view only: nothing can be changed until you go back to your own view.", "info")
    else:
        flash("Back to your own view.", "info")
    return redirect(url_for("home_launcher"))


@app.route("/view-as/person/exit", methods=["POST"])
def view_as_person_exit():
    if stop_view_as_person():
        flash("Back to your own view.", "info")
    return redirect(url_for("home_launcher"))


@app.route("/view-as/exit", methods=["POST"])
def view_as_exit():
    was_owner = view_as_active_program() == "owner"
    if exit_view_as():
        flash("Back to your normal view.", "info")
    # Exiting an owner preview clears session['customer_id'], so the portal
    # page we were just on (referrer) would immediately bounce to the
    # customer login screen - send an admin back to Admin > Customers
    # instead, same place the preview was started from.
    if was_owner:
        return redirect(url_for("customers_list"))
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/admin/users")
@master_admin_required
def admin_users_list():
    conn = get_db()
    users = conn.execute("SELECT * FROM users ORDER BY active DESC, name").fetchall()
    owner_id = owner_user_id(conn)
    conn.close()
    return render_template("admin_users.html", users=users, owner_id=owner_id,
                           viewer_is_owner=real_user_id() == owner_id)


def _pay_link_context(conn, user_row=None):
    """What the Pay section of Admin > Accounts shows: this account's shop
    worker badge(s) and CFI profile, plus the unlinked badges that could be
    linked to it (e.g. a shop badge made as "Kate" before her login existed).
    Also gathers the CFI/student profile links shown in the Profiles section
    (same two lookups the Pay section already needs, plus the student row)."""
    linked_laborers, cfi, student = [], None, None
    if user_row:
        linked_laborers = conn.execute("SELECT * FROM laborers WHERE user_id = ? ORDER BY name",
                                       (user_row["id"],)).fetchall()
        cfi = conn.execute("SELECT id, name, pay_rate_per_hour FROM cfis WHERE user_id = ?",
                           (user_row["id"],)).fetchone()
        student = conn.execute("SELECT id, name FROM students WHERE user_id = ?",
                               (user_row["id"],)).fetchone()
    unlinked_laborers = conn.execute(
        "SELECT id, name, rate, active FROM laborers WHERE user_id IS NULL ORDER BY active DESC, name").fetchall()
    return dict(linked_laborers=linked_laborers, cfi_profile=cfi, student_profile=student,
                unlinked_laborers=unlinked_laborers)


def _apply_pay_links(conn, user_row, form):
    """Saves the Pay section of the account form. Returns an error message
    (nothing saved) or None.
      badge = "" (leave as is) | "new" (make a new worker badge) | "<laborer id>" (link that badge)
      badge_rate = hourly rate for a new badge
      unlink_badge = laborer id to unlink from this account
      cfi_pay_rate = CFI pay per instructed hour (only when they have a CFI profile)"""
    badge = (form.get("badge") or "").strip()
    new_rate = None
    if badge == "new":
        new_rate = _parse_rate(form.get("badge_rate"))
        if new_rate is None:
            return "Shop hourly rate must be a number, 0 or more."
    cfi_rate_raw = (form.get("cfi_pay_rate") or "").strip()
    cfi_rate = None
    if cfi_rate_raw:
        cfi_rate = _parse_rate(cfi_rate_raw)
        if cfi_rate is None:
            return "CFI pay rate must be a number, 0 or more."
    if badge == "new":
        conn.execute("INSERT INTO laborers (name, code, rate, active, user_id, created_at, updated_at) "
                     "VALUES (?, ?, ?, 1, ?, ?, ?)",
                     (user_row["name"], gen_labor_code(conn), new_rate, user_row["id"], now_iso(), now_iso()))
    elif badge.isdigit():
        conn.execute("UPDATE laborers SET user_id = ?, updated_at = ? WHERE id = ? AND user_id IS NULL",
                     (user_row["id"], now_iso(), int(badge)))
    unlink = (form.get("unlink_badge") or "").strip()
    if unlink.isdigit():
        conn.execute("UPDATE laborers SET user_id = NULL, updated_at = ? WHERE id = ? AND user_id = ?",
                     (now_iso(), int(unlink), user_row["id"]))
    if cfi_rate is not None:
        conn.execute("UPDATE cfis SET pay_rate_per_hour = ? WHERE user_id = ?", (cfi_rate, user_row["id"]))
    conn.commit()
    return None


def _form_roles(form):
    """The ticked Maintenance / Flight School role checkboxes (plus the old
    single shop_role/flight_role field, still sent by a couple of links) ->
    (shop_roles, shop_role, flight_roles, flight_role) to save - see
    db.clean_roles."""
    shop_roles, shop_role = clean_roles(form.getlist("shop_roles") + form.getlist("shop_role"), SHOP_ROLE_ORDER)
    flight_roles, flight_role = clean_roles(form.getlist("flight_roles") + form.getlist("flight_role"), FLIGHT_ROLE_ORDER)
    return shop_roles, shop_role, flight_roles, flight_role


def _role_preset(shop_roles, flight_roles, **extra):
    return dict(shop_roles=(shop_roles or "").split(","), flight_roles=(flight_roles or "").split(","), **extra)


@app.route("/admin/users/new", methods=["GET", "POST"])
@master_admin_required
def admin_user_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        shop_roles, shop_role, flight_roles, flight_role = _form_roles(request.form)
        is_master_admin = 1 if request.form.get("is_master_admin") else 0
        can_bill = 1 if request.form.get("can_bill") else 0
        email = request.form.get("email", "").strip() or None
        phone = request.form.get("phone", "").strip() or None
        notify_email = 1 if request.form.get("notify_email") else 0
        notify_sms = 1 if request.form.get("notify_sms") else 0
        notify_low_stock = 1 if request.form.get("notify_low_stock") else 0
        notify_maintenance = 1 if request.form.get("notify_maintenance") else 0
        notify_flight_reminders = 1 if request.form.get("notify_flight_reminders") else 0
        academy_access = 1 if request.form.get("academy_access") else 0
        groundschool_access = 1 if request.form.get("groundschool_access") else 0
        conn = get_db()
        pay_ctx = _pay_link_context(conn)
        preset = _role_preset(shop_roles, flight_roles, badge=request.form.get("badge", ""),
                              badge_rate=request.form.get("badge_rate", ""), cfi_pay_rate=request.form.get("cfi_pay_rate", ""))
        if not name or not username or not password:
            conn.close()
            flash("Name, username, and password are all required.", "danger")
            return render_template("admin_user_form.html", user=None, name=name, username=username, preset=preset, **pay_ctx)
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            conn.close()
            flash(f"Username '{username}' is already taken.", "danger")
            return render_template("admin_user_form.html", user=None, name=name, username="", preset=preset, **pay_ctx)
        badge = (request.form.get("badge") or "").strip()
        if (badge == "new" and _parse_rate(request.form.get("badge_rate")) is None) or \
                ((request.form.get("cfi_pay_rate") or "").strip() and _parse_rate(request.form.get("cfi_pay_rate")) is None):
            conn.close()
            flash("Pay rates must be a number, 0 or more.", "danger")
            return render_template("admin_user_form.html", user=None, name=name, username=username, preset=preset, **pay_ctx)
        cur = conn.execute(
            "INSERT INTO users (name, username, password_hash, password_plain, is_master_admin, shop_role, flight_role, shop_roles, flight_roles, can_bill, active, "
            "email, phone, notify_email, notify_sms, notify_low_stock, notify_maintenance, notify_flight_reminders, academy_access, groundschool_access, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, username, generate_password_hash(password, method="pbkdf2:sha256"), password, is_master_admin, shop_role, flight_role,
             shop_roles, flight_roles, can_bill,
             email, phone, notify_email, notify_sms, notify_low_stock, notify_maintenance, notify_flight_reminders, academy_access, groundschool_access, now_iso()))
        conn.commit()
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        ensure_flight_profile(conn, user_row)
        _apply_pay_links(conn, user_row, request.form)
        new_badge = conn.execute("SELECT id FROM laborers WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                                 (user_row["id"],)).fetchone()
        conn.close()
        flash(f"Account created for {name}.", "success")
        if badge == "new" and new_badge:
            flash("Their shop worker badge is ready - print it so they can scan in and out.", "info")
            return redirect(url_for("laborer_label", laborer_id=new_badge["id"]))
        return redirect(url_for("admin_users_list"))
    # "Add CFI" / "New Laborer" elsewhere in the app land here with the
    # role picked already (?flight_role=cfi, ?shop_role=tech&badge=new).
    shop_roles, _, flight_roles, _ = _form_roles(request.args)
    preset = _role_preset(shop_roles, flight_roles, badge=request.args.get("badge", ""), badge_rate="", cfi_pay_rate="")
    conn = get_db()
    pay_ctx = _pay_link_context(conn)
    conn.close()
    return render_template("admin_user_form.html", user=None, preset=preset, **pay_ctx)


@app.route("/admin/users/<int:user_id>/edit", methods=["GET", "POST"])
@master_admin_required
def admin_user_edit(user_id):
    conn = get_db()
    user_row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user_row:
        conn.close()
        flash("Account not found.", "danger")
        return redirect(url_for("admin_users_list"))
    if owner_locked(user_id, conn):
        # The owner's account can only be changed by the owner - another
        # master admin can't demote, deactivate, rename or re-password it.
        conn.close()
        flash(f"{user_row['name']}'s account is the owner account - only {user_row['name']} can change it.", "warning")
        return redirect(url_for("admin_users_list"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        shop_roles, shop_role, flight_roles, flight_role = _form_roles(request.form)
        is_master_admin = 1 if request.form.get("is_master_admin") else 0
        can_bill = 1 if request.form.get("can_bill") else 0
        active = 1 if request.form.get("active") else 0
        new_password = request.form.get("password", "")
        email = request.form.get("email", "").strip() or None
        phone = request.form.get("phone", "").strip() or None
        notify_email = 1 if request.form.get("notify_email") else 0
        notify_sms = 1 if request.form.get("notify_sms") else 0
        notify_low_stock = 1 if request.form.get("notify_low_stock") else 0
        notify_maintenance = 1 if request.form.get("notify_maintenance") else 0
        notify_flight_reminders = 1 if request.form.get("notify_flight_reminders") else 0
        academy_access = 1 if request.form.get("academy_access") else 0
        groundschool_access = 1 if request.form.get("groundschool_access") else 0
        if not name:
            flash("Name is required.", "danger")
            conn.close()
            return render_template("admin_user_form.html", user=user_row)
        if user_id == session.get("user_id") and not is_master_admin:
            flash("You can't remove your own admin access.", "danger")
            conn.close()
            return render_template("admin_user_form.html", user=user_row)
        if user_id == session.get("user_id") and not active:
            flash("You can't deactivate your own account.", "danger")
            conn.close()
            return render_template("admin_user_form.html", user=user_row)
        if (request.form.get("badge") == "new" and _parse_rate(request.form.get("badge_rate")) is None) or \
                ((request.form.get("cfi_pay_rate") or "").strip() and _parse_rate(request.form.get("cfi_pay_rate")) is None):
            flash("Pay rates must be a number, 0 or more.", "danger")
            pay_ctx = _pay_link_context(conn, user_row)
            conn.close()
            return render_template("admin_user_form.html", user=user_row, **pay_ctx)
        if new_password:
            conn.execute(
                "UPDATE users SET name=?, shop_role=?, flight_role=?, shop_roles=?, flight_roles=?, is_master_admin=?, can_bill=?, active=?, password_hash=?, password_plain=?, "
                "email=?, phone=?, notify_email=?, notify_sms=?, notify_low_stock=?, notify_maintenance=?, notify_flight_reminders=?, academy_access=?, groundschool_access=? WHERE id=?",
                (name, shop_role, flight_role, shop_roles, flight_roles, is_master_admin, can_bill, active,
                 generate_password_hash(new_password, method="pbkdf2:sha256"), new_password,
                 email, phone, notify_email, notify_sms, notify_low_stock, notify_maintenance, notify_flight_reminders, academy_access, groundschool_access, user_id))
        else:
            conn.execute(
                "UPDATE users SET name=?, shop_role=?, flight_role=?, shop_roles=?, flight_roles=?, is_master_admin=?, can_bill=?, active=?, "
                "email=?, phone=?, notify_email=?, notify_sms=?, notify_low_stock=?, notify_maintenance=?, notify_flight_reminders=?, academy_access=?, groundschool_access=? WHERE id=?",
                (name, shop_role, flight_role, shop_roles, flight_roles, is_master_admin, can_bill, active,
                 email, phone, notify_email, notify_sms, notify_low_stock, notify_maintenance, notify_flight_reminders, academy_access, groundschool_access, user_id))
        conn.commit()
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        ensure_flight_profile(conn, user_row)
        # Keep the linked CFI/student profile's active flag and password in step,
        # since a couple of Flight School views still read those directly.
        if new_password:
            conn.execute("UPDATE cfis SET password_hash=?, active=? WHERE user_id=?",
                         (generate_password_hash(new_password, method="pbkdf2:sha256"), active, user_id))
            conn.execute("UPDATE students SET password_hash=?, active=? WHERE user_id=?",
                         (generate_password_hash(new_password, method="pbkdf2:sha256"), active, user_id))
        else:
            conn.execute("UPDATE cfis SET active=? WHERE user_id=?", (active, user_id))
            conn.execute("UPDATE students SET active=? WHERE user_id=?", (active, user_id))
        conn.commit()
        _apply_pay_links(conn, user_row, request.form)
        conn.close()
        flash("Account updated.", "success")
        return redirect(url_for("admin_users_list"))
    pay_ctx = _pay_link_context(conn, user_row)
    conn.close()
    return render_template("admin_user_form.html", user=user_row, **pay_ctx)


# Where "My Account" was opened from, so the centralized account page can
# offer a way straight back to that program (instead of always landing on
# the Maintenance side). Each program's navbar links with ?from=<key>.
ACCOUNT_FROM_PROGRAMS = {
    "shop": ("Maintenance", "bi-tools", "dashboard"),
    "flight": ("Flight School", "bi-airplane-engines", "flight.dashboard"),
    "academy": ("Flight Academy", "bi-mortarboard", "academy_page"),
    "admin": ("Admin", "bi-shield-lock-fill", "admin_home"),
}


def _account_programs(user_row):
    """Every program this account can open, with the role it has there -
    shown on the centralized My Account page. Read-only: access is still
    changed by an admin in Admin > Accounts."""
    is_admin = bool(user_row["is_master_admin"])
    shop_labels = {"admin": "Admin", "tech": "Tech", "inspector": "Inspector", "apprentice": "Apprentice"}
    flight_labels = {"cfi": "Instructor (CFI)", "student": "Student"}
    progs = []
    if is_admin or user_row["shop_role"]:
        progs.append(dict(key="shop", name="Winds Aloft - Maintenance", icon="bi-tools",
                          url=url_for("dashboard"),
                          role="Master admin" if is_admin else " + ".join(shop_labels.get(r, r.title()) for r in user_shop_roles(user_row))))
    if is_admin or user_row["flight_role"]:
        progs.append(dict(key="flight", name="Fly with Kate! - Flight School", icon="bi-airplane-engines",
                          url=url_for("flight.dashboard"),
                          role="Master admin" if is_admin else " + ".join(flight_labels.get(r, r.title()) for r in user_flight_roles(user_row))))
    if is_admin or user_row["academy_access"]:
        progs.append(dict(key="academy", name="Flight Academy", icon="bi-mortarboard",
                          url=url_for("academy_page"), role="Master admin" if is_admin else "Access granted"))
    if is_admin:
        progs.append(dict(key="admin", name="Admin", icon="bi-shield-lock-fill",
                          url=url_for("admin_home"), role="Owner" if user_row["id"] == owner_user_id() else "Master admin"))
    return progs


def _account_came_from():
    """Which program My Account was opened from: ?from= wins, else the page
    the person was just on (a /flight page means Flight School, and so on),
    else - when they're coming back to My Account from itself, e.g. after
    Save - the program remembered last time. None = the program picker."""
    key = request.args.get("from", "").strip()
    if key not in ACCOUNT_FROM_PROGRAMS:
        key = None
        ref = urlparse(request.referrer or "")
        if ref.netloc == request.host:
            path = ref.path or "/"
            if path.startswith("/account"):
                key = session.get("account_from")
            elif path.startswith("/flight"):
                key = "flight"
            elif path.startswith("/academy"):
                key = "academy"
            elif path.startswith("/admin"):
                key = "admin"
            elif path != "/":
                key = "shop"
    session["account_from"] = key
    return key


def _render_account(user_row, came_from):
    back = None
    if came_from and came_from in ACCOUNT_FROM_PROGRAMS:
        label, icon, endpoint = ACCOUNT_FROM_PROGRAMS[came_from]
        back = dict(key=came_from, label=label, icon=icon, url=url_for(endpoint))
    return render_template("account.html", user=user_row, programs=_account_programs(user_row),
                           came_from=back)


@app.route("/account", methods=["GET", "POST"])
@login_required
def account_page():
    """Self-service 'My Account' - one centralized page for the person's
    account across every OpsHub program (Maintenance, Flight School,
    Academy, Admin). Every logged-in user (not just admins) gets here from
    the dropdown under their name, top right, in any program, and it opens
    in its own neutral OpsHub layout (account_base.html) with a button back
    to the program they came from. It shows which programs they can use and
    their role in each, and lets them update their own contact info,
    password, and notification preferences for each program they have.
    Role/access fields (shop role, flight role, master admin, academy
    access, active) aren't editable here - those stay admin-only, via
    Admin > Accounts."""
    conn = get_db()
    user_row = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not user_row:
        conn.close()
        flash("Account not found.", "danger")
        return redirect(url_for("home_launcher"))
    came_from = _account_came_from() if request.method == "GET" else session.get("account_from")
    if request.method == "POST":
        has_shop = bool(user_row["is_master_admin"] or user_row["shop_role"])
        has_flight = bool(user_row["is_master_admin"] or user_row["flight_role"])
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip() or None
        phone = request.form.get("phone", "").strip() or None
        notify_email = 1 if request.form.get("notify_email") else 0
        notify_sms = 1 if request.form.get("notify_sms") else 0
        # Each program's section only shows for people who can use that
        # program - a hidden section keeps its saved settings instead of
        # being switched off by the save.
        if has_shop:
            notify_low_stock = 1 if request.form.get("notify_low_stock") else 0
            notify_maintenance = 1 if request.form.get("notify_maintenance") else 0
        else:
            notify_low_stock = user_row["notify_low_stock"] or 0
            notify_maintenance = user_row["notify_maintenance"] or 0
        if has_flight:
            notify_flight_reminders = 1 if request.form.get("notify_flight_reminders") else 0
            notify_push_30min = 1 if request.form.get("notify_push_30min") else 0
            notify_push_timeup = 1 if request.form.get("notify_push_timeup") else 0
            notify_push_late = 1 if request.form.get("notify_push_late") else 0
        else:
            notify_flight_reminders = user_row["notify_flight_reminders"] or 0
            notify_push_30min = user_row["notify_push_30min"] or 0
            notify_push_timeup = user_row["notify_push_timeup"] or 0
            notify_push_late = user_row["notify_push_late"] or 0
        notify_push_session_alerts = 1 if (notify_push_30min or notify_push_timeup or notify_push_late) else 0
        new_password = request.form.get("password", "")
        confirm_password = request.form.get("password_confirm", "")
        if not name:
            flash("Name is required.", "danger")
            conn.close()
            return _render_account(user_row, came_from)
        if new_password and new_password != confirm_password:
            flash("The two new password fields don't match.", "danger")
            conn.close()
            return _render_account(user_row, came_from)
        if new_password:
            pw_hash = generate_password_hash(new_password, method="pbkdf2:sha256")
            conn.execute(
                "UPDATE users SET name=?, email=?, phone=?, notify_email=?, notify_sms=?, notify_low_stock=?, "
                "notify_maintenance=?, notify_flight_reminders=?, notify_push_session_alerts=?, "
                "notify_push_30min=?, notify_push_timeup=?, notify_push_late=?, "
                "password_hash=?, password_plain=? WHERE id=?",
                (name, email, phone, notify_email, notify_sms, notify_low_stock, notify_maintenance,
                 notify_flight_reminders, notify_push_session_alerts,
                 notify_push_30min, notify_push_timeup, notify_push_late, pw_hash, new_password, session["user_id"]))
            conn.execute("UPDATE cfis SET password_hash=? WHERE user_id=?", (pw_hash, session["user_id"]))
            conn.execute("UPDATE students SET password_hash=? WHERE user_id=?", (pw_hash, session["user_id"]))
        else:
            conn.execute(
                "UPDATE users SET name=?, email=?, phone=?, notify_email=?, notify_sms=?, notify_low_stock=?, "
                "notify_maintenance=?, notify_flight_reminders=?, notify_push_session_alerts=?, "
                "notify_push_30min=?, notify_push_timeup=?, notify_push_late=? WHERE id=?",
                (name, email, phone, notify_email, notify_sms, notify_low_stock, notify_maintenance,
                 notify_flight_reminders, notify_push_session_alerts,
                 notify_push_30min, notify_push_timeup, notify_push_late, session["user_id"]))
        conn.commit()
        session["user_name"] = name
        conn.close()
        flash("Your account has been updated.", "success")
        return redirect(url_for("account_page", **({"from": came_from} if came_from else {})))
    conn.close()
    return _render_account(user_row, came_from)


# ---------------------------------------------------------------------------
# Master admin: notification (email/text reminder) settings
# ---------------------------------------------------------------------------

@app.route("/account/push_test", methods=["POST"])
@login_required
def account_push_test():
    """"Send me a test alert" on My Account - pushes to every device this
    person has turned phone alerts on for, and says how many it reached."""
    conn = get_db()
    uid = session["user_id"]
    results = push.queue_and_push(conn, uid, "Fly with Kate! test alert",
                                  "If you can see this, phone alerts work on this device.",
                                  tag=f"push-test-{uid}", url="/account")
    if not any(ok for ok, _s, _e in results):
        conn.execute("DELETE FROM push_pending WHERE user_id = ? AND tag = ?", (uid, f"push-test-{uid}"))
        conn.commit()
    conn.close()
    if not results:
        flash("No devices have phone alerts turned on for your account yet - tap Enable on this device first.", "warning")
    else:
        sent = sum(1 for ok, _s, _e in results if ok)
        failed = [f"{status or ''} {err or ''}".strip() for ok, status, err in results if not ok]
        msg = f"Test alert sent to {sent} device{'s' if sent != 1 else ''}."
        if failed:
            msg += f" {len(failed)} failed ({'; '.join(failed)[:120]})."
        flash(msg, "success" if sent else "danger")
    came_from = session.get("account_from")
    return redirect(url_for("account_page", **({"from": came_from} if came_from else {})) + "#push-status")


@app.route("/push-sw.js")
def push_service_worker():
    """The phone-alert service worker, served from the site root so its
    scope is the whole app ("/"). Served from /static/ its scope was only
    /static/, so navigator.serviceWorker.ready never resolved on real pages
    like My Account - the Enable button would ask for permission and then
    never finish, and the status never showed ON."""
    path = os.path.join(app.static_folder, "push-sw.js")
    with open(path, "rb") as fh:
        resp = Response(fh.read(), mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------------------
# Customer portal accounts (aircraft owners) - who can log in, and which
# aircraft they're linked to. See customer.py for the portal itself.
# ---------------------------------------------------------------------------

@app.route("/customers")
@shop_role_required('admin')
def customers_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM customers ORDER BY active DESC, name").fetchall()
    customers = []
    for c in rows:
        planes = conn.execute("""
            SELECT a.tag FROM customer_assets ca JOIN assets a ON a.id = ca.asset_id
            WHERE ca.customer_id = ? ORDER BY a.tag
        """, (c["id"],)).fetchall()
        customers.append({"row": c, "planes": [p["tag"] for p in planes]})
    conn.close()
    return render_template("customers.html", customers=customers)


@app.route("/customers/new", methods=["GET", "POST"])
@shop_role_required('admin')
def customer_new():
    conn = get_db()
    assets = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_simulator = 0 AND is_owner_placeholder = 0 ORDER BY tag").fetchall()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        asset_ids = [int(x) for x in request.form.getlist("asset_ids") if x.isdigit()]
        if not name or not email or not password:
            flash("Name, email, and a starting password are all required.", "danger")
            conn.close()
            return render_template("customer_form.html", customer=None, assets=assets, linked_ids=set(),
                                    name=name, email=email, phone=phone)
        existing = conn.execute("SELECT id FROM customers WHERE lower(email) = ?", (email.lower(),)).fetchone()
        if existing:
            conn.close()
            flash(f"'{email}' already has a customer account.", "danger")
            return render_template("customer_form.html", customer=None, assets=assets, linked_ids=set(),
                                    name=name, email=email, phone=phone)
        cur = conn.execute(
            "INSERT INTO customers (name, email, password_hash, password_plain, phone, active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (name, email, generate_password_hash(password, method="pbkdf2:sha256"), password, phone, now_iso()))
        customer_id = cur.lastrowid
        for aid in asset_ids:
            conn.execute("INSERT OR IGNORE INTO customer_assets (customer_id, asset_id) VALUES (?, ?)",
                         (customer_id, aid))
        conn.commit()
        conn.close()
        flash(f"Customer account created for {name}.", "success")
        return redirect(url_for("customers_list"))
    conn.close()
    return render_template("customer_form.html", customer=None, assets=assets, linked_ids=set(),
                            name="", email="", phone="")


@app.route("/customers/<int:customer_id>")
@shop_role_required('admin')
def customer_detail(customer_id):
    """Read-only admin view of one customer's account: their linked
    aircraft, each one's maintenance reminders, appointments (with the
    same confirm/reschedule status the customer sees), and job costs -
    the same information the customer's own /portal shows, from the admin
    side. This is deliberately scoped to just that - not a way into the
    full parts/inventory/projects side of Winds Aloft; use the normal
    asset/project pages (linked below) to actually change something."""
    conn = get_db()
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not customer:
        conn.close()
        abort(404)
    asset_ids = _owned_asset_ids(conn, customer_id)
    planes = []
    if asset_ids:
        ph = ",".join("?" * len(asset_ids))
        assets = conn.execute(
            f"SELECT * FROM assets WHERE id IN ({ph}) AND deleted_at IS NULL ORDER BY tag", asset_ids).fetchall()
        for a in assets:
            items = conn.execute(
                "SELECT * FROM maintenance_items WHERE asset_id = ? AND active = 1 ORDER BY name", (a["id"],)).fetchall()
            reminders = [{"item": m, "status": maintenance_status(m, asset_meter(a, m["hour_type"]))} for m in items]
            reminders.sort(key=lambda r: {"overdue": 0, "due_soon": 1, "ok": 2, "unknown": 3}.get(r["status"]["urgency"], 4))
            appointments = conn.execute("""
                SELECT * FROM projects WHERE asset_id = ? AND deleted_at IS NULL AND scheduled_date IS NOT NULL
                      AND status NOT IN ('completed', 'archived')
                ORDER BY scheduled_date
            """, (a["id"],)).fetchall()
            jobs = conn.execute("""
                SELECT * FROM projects WHERE asset_id = ? AND deleted_at IS NULL
                ORDER BY (status = 'active') DESC, created_at DESC LIMIT 10
            """, (a["id"],)).fetchall()
            bills = []
            for j in jobs:
                grouped, labor, total = _project_bill(conn, j["id"])
                if total > 0 or grouped or labor:
                    bills.append({"project": j, "total": total})
            planes.append({"asset": a, "reminders": reminders, "appointments": appointments, "bills": bills})
    conn.close()
    return render_template("customer_detail.html", customer=customer, planes=planes)


@app.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@shop_role_required('admin')
def customer_edit(customer_id):
    conn = get_db()
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not customer:
        conn.close()
        abort(404)
    assets = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL AND is_simulator = 0 AND is_owner_placeholder = 0 ORDER BY tag").fetchall()
    linked_ids = {r["asset_id"] for r in conn.execute(
        "SELECT asset_id FROM customer_assets WHERE customer_id = ?", (customer_id,)).fetchall()}
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        new_password = request.form.get("password", "").strip()
        active = 1 if request.form.get("active") else 0
        asset_ids = {int(x) for x in request.form.getlist("asset_ids") if x.isdigit()}
        if not name or not email:
            flash("Name and email are required.", "danger")
            conn.close()
            return render_template("customer_form.html", customer=customer, assets=assets, linked_ids=linked_ids,
                                    name=name, email=email, phone=phone)
        dupe = conn.execute("SELECT id FROM customers WHERE lower(email) = ? AND id != ?",
                             (email.lower(), customer_id)).fetchone()
        if dupe:
            flash(f"'{email}' already belongs to another customer account.", "danger")
            conn.close()
            return render_template("customer_form.html", customer=customer, assets=assets, linked_ids=linked_ids,
                                    name=name, email=email, phone=phone)
        if new_password:
            conn.execute("UPDATE customers SET name=?, email=?, phone=?, active=?, password_hash=?, password_plain=? WHERE id=?",
                         (name, email, phone, active, generate_password_hash(new_password, method="pbkdf2:sha256"),
                          new_password, customer_id))
        else:
            conn.execute("UPDATE customers SET name=?, email=?, phone=?, active=? WHERE id=?",
                         (name, email, phone, active, customer_id))
        conn.execute("DELETE FROM customer_assets WHERE customer_id = ?", (customer_id,))
        for aid in asset_ids:
            conn.execute("INSERT OR IGNORE INTO customer_assets (customer_id, asset_id) VALUES (?, ?)",
                         (customer_id, aid))
        conn.commit()
        conn.close()
        flash("Customer account updated.", "success")
        return redirect(url_for("customers_list"))
    conn.close()
    return render_template("customer_form.html", customer=customer, assets=assets, linked_ids=linked_ids,
                            name=customer["name"], email=customer["email"], phone=customer["phone"] or "")


@app.route("/customers/<int:customer_id>/delete", methods=["POST"])
@shop_role_required('admin')
def customer_delete(customer_id):
    conn = get_db()
    conn.execute("DELETE FROM customer_assets WHERE customer_id = ?", (customer_id,))
    conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    conn.commit()
    conn.close()
    flash("Customer account removed.", "success")
    return redirect(url_for("customers_list"))


@app.route("/projects/<int:project_id>/reschedule/dismiss", methods=["POST"])
@shop_role_required('admin', 'tech')
def project_reschedule_dismiss(project_id):
    """Admin has seen and handled a customer's reschedule request (e.g.
    already called them, or already changed scheduled_date) - clears the
    flag so it drops off the dashboard. Doesn't touch scheduled_date itself;
    use the normal Edit Project form for that."""
    conn = get_db()
    conn.execute("UPDATE projects SET customer_reschedule_requested_at = NULL, customer_reschedule_note = NULL WHERE id = ?",
                 (project_id,))
    conn.commit()
    conn.close()
    flash("Dismissed.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/admin/notifications", methods=["GET", "POST"])
@master_admin_required
def admin_notifications():
    conn = get_db()
    if request.method == "POST":
        notify.save_settings(conn, {
            "smtp_host": request.form.get("smtp_host", ""),
            "smtp_port": request.form.get("smtp_port", ""),
            "smtp_username": request.form.get("smtp_username", ""),
            "smtp_password": request.form.get("smtp_password", ""),
            "smtp_from": request.form.get("smtp_from", ""),
            "twilio_account_sid": request.form.get("twilio_account_sid", ""),
            "twilio_auth_token": request.form.get("twilio_auth_token", ""),
            "twilio_from_number": request.form.get("twilio_from_number", ""),
        })
        flash("Notification settings saved.", "success")
        conn.close()
        return redirect(url_for("admin_notifications"))
    settings = notify.get_settings(conn)
    # Everyone who could get flight phone alerts, with how many devices each
    # has turned on - for the "Test phone alerts" picker.
    push_people = conn.execute("""
        SELECT u.id, u.name, COUNT(ps.id) AS device_count
        FROM users u LEFT JOIN push_subscriptions ps ON ps.user_id = u.id
        WHERE u.active = 1 AND (u.is_master_admin = 1 OR u.flight_role IS NOT NULL AND u.flight_role != '')
        GROUP BY u.id ORDER BY device_count = 0, u.name
    """).fetchall()
    # Every active account with an email on file - lets "Send a test email
    # to..." offer a filterable pick-list instead of having to remember or
    # look up an address, while still taking any typed-in email too (see
    # the <datalist> in admin_notifications.html, same pattern as the part
    # form's Category field).
    email_people = conn.execute(
        "SELECT name, email FROM users WHERE active = 1 AND email IS NOT NULL AND email != '' ORDER BY name"
    ).fetchall()
    conn.close()
    return render_template("admin_notifications.html", settings=settings,
                           email_configured=notify.email_configured(settings),
                           sms_configured=notify.sms_configured(settings),
                           push_people=push_people, email_people=email_people)


@app.route("/admin/wave", methods=["GET", "POST"])
@master_admin_required
def admin_wave():
    """Admin > Wave: up to three Wave accounts (each one business with its
    own products, its own login or sharing another account's), which
    programs each is used for, and each program's default account - the
    one Invoice in Wave preselects (any of the program's accounts can
    still be picked there for a single invoice)."""
    conn = get_db()
    if request.method == "POST":
        current = wave_billing.get_settings(conn)
        values = {}
        for n in wave_billing.ACCOUNTS:
            k = lambda f: wave_billing.account_key(n, f)  # noqa: E731
            # A blank token box keeps the saved token (it's never shown).
            values[k("token")] = request.form.get(k("token"), "").strip() or current[k("token")]
            values[k("token_from")] = request.form.get(k("token_from"), "")
            if values[k("token_from")]:
                values[k("token")] = ""  # sharing another account's login
            values[k("name")] = request.form.get(k("name"), "")
            # Business select sends "id|name" so the name can be shown
            # without asking Wave again.
            biz_id, _, biz_name = request.form.get(k("business_id"), "").partition("|")
            values[k("business_id")] = biz_id
            values[k("business_name")] = biz_name if biz_id else ""
            if biz_id == current[k("business_id")] and not biz_name:
                values[k("business_name")] = current[k("business_name")]
            for f in ("labor_product_id", "parts_product_id", "flight_product_id"):
                values[k(f)] = "" if biz_id != current[k("business_id")] else request.form.get(k(f), "")
        for program in wave_billing.PROGRAMS:
            picked = [str(n) for n in wave_billing.ACCOUNTS if request.form.get(f"use_{program}_{n}") == "1"]
            values[wave_billing.program_key(program, "accounts")] = ",".join(picked)
            default = request.form.get(wave_billing.program_key(program, "default"), "")
            values[wave_billing.program_key(program, "default")] = default if default in picked else (picked[0] if picked else "")
        wave_billing.save_settings(conn, values)
        conn.close()
        flash("Wave settings saved.", "success")
        return redirect(url_for("admin_wave"))
    raw = wave_billing.get_settings(conn)
    accounts = []
    for n in wave_billing.ACCOUNTS:
        cfg = wave_billing.account_config(raw, n)
        open_count = conn.execute("SELECT COUNT(*) c FROM wave_invoices WHERE paid_applied_at IS NULL AND COALESCE(account, 1) = ?",
                                  (n,)).fetchone()["c"]
        info = {"cfg": cfg, "connected": wave_billing.is_connected(cfg), "open_count": open_count,
                "businesses": [], "products": [], "error": None,
                "programs": {p: str(n) in raw[wave_billing.program_key(p, "accounts")].split(",") for p in wave_billing.PROGRAMS}}
        if cfg["token"]:
            try:
                info["businesses"] = wave_billing.list_businesses(cfg["token"])
                if cfg["business_id"]:
                    info["products"] = wave_billing.list_products(cfg["token"], cfg["business_id"])
            except wave_billing.WaveError as e:
                info["error"] = str(e)
        accounts.append(info)
    programs = []
    for p, spec in wave_billing.PROGRAMS.items():
        connected, default = wave_billing.program_accounts(conn, p)
        programs.append({"key": p, "label": spec["label"], "connected": connected, "default": default,
                         "products": spec["products"]})
    conn.close()
    return render_template("admin_wave.html", accounts=accounts, programs=programs,
                           enabled=wave_billing.is_enabled(raw),
                           product_labels=wave_billing.PRODUCT_LABELS,
                           program_key=wave_billing.program_key, account_key=wave_billing.account_key)


@app.route("/admin/wave/toggle", methods=["POST"])
@master_admin_required
def admin_wave_toggle():
    """The on/off switch: off hides Invoice in Wave everywhere and pauses
    the payment check, keeping every setting and invoice for later."""
    on = request.form.get("enabled") == "1"
    conn = get_db()
    wave_billing.save_settings(conn, {wave_billing.ENABLED_KEY: "1" if on else "0"})
    conn.close()
    flash("Wave invoicing turned on." if on else
          "Wave invoicing turned off. Settings and invoices are kept - switch it back on any time.", "success")
    return redirect(url_for("admin_wave"))


@app.route("/admin/wave/test/<int:n>", methods=["POST"])
@master_admin_required
def admin_wave_test(n):
    """Test connection: read-only checks on one account as saved."""
    if n not in wave_billing.ACCOUNTS:
        abort(404)
    conn = get_db()
    results = wave_billing.test_account(conn, n)
    name = wave_billing.account_config(wave_billing.get_settings(conn), n)["name"]
    conn.close()
    ok = all(r[0] for r in results)
    flash(f"{name} test: " + ("everything checks out." if ok else "something needs fixing."), "success" if ok else "warning")
    for good, msg in results:
        flash(("\u2713 " if good else "\u2717 ") + msg, "success" if good else "danger")
    return redirect(url_for("admin_wave"))


@app.route("/admin/wave/test/<int:n>/invoice", methods=["POST"])
@master_admin_required
def admin_wave_test_invoice(n):
    """Make a test invoice: a $1 draft in Wave, emailed to no one."""
    if n not in wave_billing.ACCOUNTS:
        abort(404)
    conn = get_db()
    try:
        inv = wave_billing.create_test_invoice(conn, n)
    except wave_billing.WaveError as e:
        conn.close()
        flash(str(e), "danger")
        return redirect(url_for("admin_wave"))
    conn.close()
    flash(f"Test invoice #{inv.get('invoiceNumber') or ''} made in Wave as a draft for $1.00 to \"OpsHub test\" - "
          "nothing was sent to anyone. Find it under Sales & Payments > Invoices in Wave and delete it there.", "success")
    return redirect(url_for("admin_wave"))


@app.route("/admin/wave/disconnect/<int:n>", methods=["POST"])
@master_admin_required
def admin_wave_disconnect(n):
    """Clears one Wave account and takes it off both programs. Invoices
    already made in it stay in Wave; OpsHub just can't check them for
    payment any more until it's connected again."""
    if n not in wave_billing.ACCOUNTS:
        abort(404)
    conn = get_db()
    raw = wave_billing.get_settings(conn)
    cfg = wave_billing.account_config(raw, n)
    values = {wave_billing.account_key(n, f): "" for f in wave_billing.ACCOUNT_FIELDS}
    for m in wave_billing.ACCOUNTS:  # accounts that shared this one's login lose it too
        if raw[wave_billing.account_key(m, "token_from")] == str(n):
            values[wave_billing.account_key(m, "token_from")] = ""
    for p in wave_billing.PROGRAMS:
        kept = [x for x in raw[wave_billing.program_key(p, "accounts")].split(",") if x and x != str(n)]
        values[wave_billing.program_key(p, "accounts")] = ",".join(kept)
        if raw[wave_billing.program_key(p, "default")] == str(n):
            values[wave_billing.program_key(p, "default")] = kept[0] if kept else ""
    wave_billing.save_settings(conn, values)
    if cfg["business_id"]:
        wave_billing.forget_business_customers(conn, cfg["business_id"])
    conn.commit()
    conn.close()
    flash(f"{cfg['name']} disconnected.", "success")
    return redirect(url_for("admin_wave"))


@app.route("/admin/notifications/test_push", methods=["POST"])
@master_admin_required
def admin_notifications_test_push():
    """Sends a test phone alert to the picked people (or everyone with a
    device turned on) and reports, per person, how many devices it reached."""
    conn = get_db()
    if request.form.get("send_to") == "all":
        user_ids = [r["user_id"] for r in conn.execute("SELECT DISTINCT user_id FROM push_subscriptions").fetchall()]
    else:
        user_ids = [int(x) for x in request.form.getlist("user_ids") if x.isdigit()]
    if not user_ids:
        conn.close()
        flash("Pick at least one person (or choose Everyone) to send the test to.", "danger")
        return redirect(url_for("admin_notifications") + "#push-test")
    message = request.form.get("test_message", "").strip() or "Test alert - if you can see this, phone alerts are working on this device."
    sender = session.get("user_name") or "OpsHub"
    lines = []
    any_ok = False
    for uid in user_ids:
        user = conn.execute("SELECT id, name FROM users WHERE id = ?", (uid,)).fetchone()
        if not user:
            continue
        results = push.queue_and_push(conn, uid, "Fly with Kate! test alert", f"{message} (sent by {sender})",
                                      tag=f"push-test-{uid}", url="/account")
        if not any(ok for ok, _s, _e in results):
            # Nothing was delivered - drop the queued copy so it doesn't pop
            # up later out of the blue once their phone starts working.
            conn.execute("DELETE FROM push_pending WHERE user_id = ? AND tag = ?", (uid, f"push-test-{uid}"))
            conn.commit()
        if not results:
            lines.append(f"{user['name']}: no devices turned on (they need to tap Enable in My Account on their phone)")
            continue
        sent = sum(1 for ok, _s, _e in results if ok)
        failed = [f"{status or ''} {err or ''}".strip() for ok, status, err in results if not ok]
        any_ok = any_ok or sent > 0
        line = f"{user['name']}: sent to {sent} device{'s' if sent != 1 else ''}"
        if failed:
            line += f", {len(failed)} failed ({'; '.join(failed)[:120]})"
        lines.append(line)
    conn.close()
    flash("Test phone alert - " + " | ".join(lines), "success" if any_ok else "warning")
    return redirect(url_for("admin_notifications") + "#push-test")


@app.route("/admin/notifications/test_email", methods=["POST"])
@master_admin_required
def admin_notifications_test_email():
    to_addr = request.form.get("test_email", "").strip()
    if not to_addr:
        flash("Enter an email address to send the test to.", "danger")
        return redirect(url_for("admin_notifications"))
    conn = get_db()
    settings = notify.get_settings(conn)
    conn.close()
    ok, err = notify.send_email(settings, to_addr, "Fly with Kate! test reminder",
                                 "This is a test message from Fly with Kate! - if you got this, email reminders are working.")
    if ok:
        flash(f"Test email sent to {to_addr}.", "success")
    else:
        flash(f"Test email failed: {err}", "danger")
    return redirect(url_for("admin_notifications"))


@app.route("/admin/notifications/test_sms", methods=["POST"])
@master_admin_required
def admin_notifications_test_sms():
    to_number = request.form.get("test_phone", "").strip()
    if not to_number:
        flash("Enter a phone number to send the test to.", "danger")
        return redirect(url_for("admin_notifications"))
    conn = get_db()
    settings = notify.get_settings(conn)
    conn.close()
    ok, err = notify.send_sms(settings, to_number, "Fly with Kate! test reminder - if you got this, text reminders are working.")
    if ok:
        flash(f"Test text sent to {to_number}.", "success")
    else:
        flash(f"Test text failed: {err}", "danger")
    return redirect(url_for("admin_notifications"))


# ---------------------------------------------------------------------------
# Master admin: reset/wipe tools
# ---------------------------------------------------------------------------

def _removable_cfi_ids(conn):
    """Instructor profiles the Reset Instructors button may delete - every
    CFI except one linked to a master admin login, so the owner's own
    instructor profile (and their login) always survives a reset."""
    return [r["id"] for r in conn.execute("""
        SELECT c.id FROM cfis c LEFT JOIN users u ON u.id = c.user_id
        WHERE COALESCE(u.is_master_admin, 0) = 0""").fetchall()]


def _reset_counts():
    """Row counts shown on the Reset Data page so a master admin can see
    what each button is about to wipe before they click it."""
    conn = get_db()
    counts = {
        "activity_log": conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"],
        "orders": conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"],
        "projects": conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"],
        "flights": conn.execute("SELECT COUNT(*) c FROM flights").fetchone()["c"],
        "planes": conn.execute("SELECT COUNT(*) c FROM assets WHERE is_flight_asset = 1").fetchone()["c"],
        "squawks": (conn.execute("SELECT COUNT(*) c FROM flights WHERE squawk = 1").fetchone()["c"]
                    + conn.execute("SELECT COUNT(*) c FROM plane_squawks").fetchone()["c"]),
        "schedule": conn.execute("SELECT COUNT(*) c FROM scheduled_flights").fetchone()["c"],
        "students": conn.execute("SELECT COUNT(*) c FROM students WHERE is_station = 0").fetchone()["c"],
        "ledger": conn.execute("SELECT COUNT(*) c FROM student_ledger").fetchone()["c"],
        "instructors": len(_removable_cfi_ids(conn)),
    }
    counts["flight_school_total"] = (counts["flights"] + counts["schedule"] + counts["students"]
                                     + counts["ledger"] + counts["instructors"] + counts["planes"])
    conn.close()
    return counts


@app.route("/admin/reset")
@master_admin_required
def admin_reset():
    return render_template("admin_reset.html", counts=_reset_counts())


# ---------------------------------------------------------------------------
# Master admin: remote restart/reboot/shutdown (a "backdoor" for when SSH is down but
# this app itself is still reachable - see admin_system.html for the pitch).
# Both run the actual command a couple seconds after responding, in a
# background thread, so the browser gets its response/flash message before
# the connection drops out from under it. Requires the Pi's OS user this app
# runs as to have passwordless sudo for exactly these commands (see the
# README for the one-time `visudo` line) - nothing else is granted.
# ---------------------------------------------------------------------------

def _read_pi_temp_c():
    """Current SoC temperature via vcgencmd, or None off-Pi/if unavailable.
    Same reading pi_health.py's cron job alerts on - see that file."""
    try:
        out = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True,
                              timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"temp=([\d.]+)", out)
    return float(m.group(1)) if m else None


@app.route("/admin/login-attempts", methods=["GET", "POST"])
@master_admin_required
def admin_login_attempts():
    """Recent failed logins and lockouts, with an Unlock button per locked account."""
    if request.method == "POST":
        unlock_account(request.form.get("account", ""))
        flash("Unlocked.", "success")
        return redirect(url_for("admin_login_attempts"))
    conn = get_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    locked = conn.execute("SELECT account, locked_until FROM login_lockouts WHERE locked_until > ? "
                          "ORDER BY locked_until DESC", (now,)).fetchall()
    attempts = conn.execute("SELECT * FROM login_attempts ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return render_template("admin_login_attempts.html", locked=locked, attempts=attempts)


@app.route("/admin/system")
@master_admin_required
def admin_system():
    return render_template("admin_system.html", pi_temp_c=_read_pi_temp_c())


def _run_delayed_command(args, delay=2.0):
    def _go():
        time.sleep(delay)
        subprocess.run(args, check=False)
    threading.Thread(target=_go, daemon=True).start()


@app.route("/admin/system/restart_app", methods=["POST"])
@master_admin_required
def admin_system_restart_app():
    flash("Restarting the app now - this page will be unreachable for a few seconds.", "success")
    _run_delayed_command(["sudo", "systemctl", "restart", "opshub"])
    return redirect(url_for("admin_system"))


@app.route("/admin/system/reboot", methods=["POST"])
@master_admin_required
def admin_system_reboot():
    flash("Rebooting the Pi now - everything will be unreachable for 30-60 seconds.", "success")
    _run_delayed_command(["sudo", "reboot"])
    return redirect(url_for("admin_system"))


@app.route("/admin/system/shutdown", methods=["POST"])
@master_admin_required
def admin_system_shutdown():
    # A clean power-off before unplugging the Pi (so the SD card and the
    # database aren't mid-write when the power goes). Nothing remote can turn
    # it back on afterwards - see admin_system.html for how to power it up.
    flash("Shutting the Pi down now. Wait until its green light stops blinking (about 20-30 seconds) "
          "before unplugging it. To turn it back on, plug the power back in.", "success")
    _run_delayed_command(["sudo", "shutdown", "-h", "now"])
    return redirect(url_for("admin_system"))


@app.route("/admin/system/log")
@master_admin_required
def admin_system_log():
    """Lists the individual error files, newest first - every unhandled
    exception, with its full traceback, is saved as its own timestamped
    file in instance/error_logs/ regardless of debug mode (see the
    got_request_exception hookup and _PerFileErrorHandler near the top of
    this file). The point is to make an error visible and copyable from a
    browser - on a phone, from off-site over Tailscale, wherever - without
    needing to SSH into the Pi and go digging through journalctl just to
    see what broke."""
    entries = []
    total_count = 0
    error = None
    try:
        if os.path.isdir(ERROR_LOG_DIR):
            names = sorted((n for n in os.listdir(ERROR_LOG_DIR) if n.endswith(".log")), reverse=True)
            total_count = len(names)
            for name in names[:100]:  # most recent 100 files is plenty without the page getting unwieldy
                try:
                    with open(os.path.join(ERROR_LOG_DIR, name), "r", errors="replace") as f:
                        text = f.read()
                except OSError:
                    continue
                entries.append(_parse_error_log_entry(name, text))
    except OSError as e:
        error = str(e)
    return render_template("admin_system_log.html", entries=entries, error=error,
                           truncated=total_count > len(entries), total_count=total_count)


@app.route("/admin/system/log/clear", methods=["POST"])
@master_admin_required
def admin_system_log_clear():
    try:
        if os.path.isdir(ERROR_LOG_DIR):
            for name in os.listdir(ERROR_LOG_DIR):
                if name.endswith(".log"):
                    os.remove(os.path.join(ERROR_LOG_DIR, name))
        flash("Error log cleared.", "success")
    except OSError as e:
        flash(f"Couldn't clear the log: {e}", "danger")
    return redirect(url_for("admin_system_log"))


@app.route("/system_alerts/<int:alert_id>/acknowledge", methods=["POST"])
@shop_role_required('admin')
def system_alert_acknowledge(alert_id):
    """Dismisses a Pi health banner (see dashboard()) - written by
    pi_health.py's cron job, not by this app."""
    conn = get_db()
    conn.execute("UPDATE system_alerts SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL",
                 (now_iso(), alert_id))
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))


def _delete_photo_files(conn, where_clause, params):
    """Removes the uploaded image files for a set of photo rows before the
    rows themselves are deleted, so nothing orphaned is left in static/uploads."""
    rows = conn.execute(f"SELECT filename FROM photos WHERE {where_clause}", params).fetchall()
    for r in rows:
        file_path = os.path.join(UPLOAD_DIR, r["filename"])
        if os.path.exists(file_path):
            os.remove(file_path)


@app.route("/admin/reset/activity_log", methods=["POST"])
@master_admin_required
def admin_reset_activity_log():
    conn = get_db()
    conn.execute("DELETE FROM transactions")
    conn.commit()
    conn.close()
    flash("Activity log cleared.", "success")
    return redirect(url_for("admin_reset"))


@app.route("/admin/reset/orders", methods=["POST"])
@master_admin_required
def admin_reset_orders():
    conn = get_db()
    conn.execute("DELETE FROM orders")
    conn.commit()
    conn.close()
    flash("Orders cleared.", "success")
    return redirect(url_for("admin_reset"))


@app.route("/admin/reset/squawks", methods=["POST"])
@master_admin_required
def admin_reset_squawks():
    """Clears squawk flags only - keeps the flight log entries themselves.
    Quick squawks (not tied to a flight) have no entry to keep, so those
    rows are deleted outright."""
    conn = get_db()
    conn.execute("""UPDATE flights SET squawk = 0, squawk_acknowledged_at = NULL, squawk_acknowledged_by = NULL,
                     squawk_repaired_at = NULL, squawk_repaired_by = NULL WHERE squawk = 1""")
    conn.execute("DELETE FROM plane_squawks")
    conn.commit()
    conn.close()
    flash("Squawks cleared.", "success")
    return redirect(url_for("admin_reset"))


@app.route("/admin/reset/flights", methods=["POST"])
@master_admin_required
def admin_reset_flights():
    return _run_reset(_wipe_flight_log, "Flight log cleared.")


@app.route("/admin/reset/projects", methods=["POST"])
@master_admin_required
def admin_reset_projects():
    """Wipes every project and everything that hangs off one. Since new
    project codes (e.g. 26-001) are generated from the highest existing
    code, deleting all projects also resets the project numbering back
    to 001 for the current year. Transactions and orders that were tied
    to a project are kept (part/order history), just un-linked from the
    project that no longer exists."""
    conn = get_db()
    _delete_photo_files(conn, "project_id IS NOT NULL", ())
    conn.execute("DELETE FROM photos WHERE project_id IS NOT NULL")
    conn.execute("DELETE FROM labor_sessions")
    conn.execute("DELETE FROM project_sections")
    conn.execute("UPDATE maintenance_log SET project_id = NULL WHERE project_id IS NOT NULL")
    conn.execute("UPDATE transactions SET project_id = NULL WHERE project_id IS NOT NULL")
    conn.execute("UPDATE orders SET project_id = NULL WHERE project_id IS NOT NULL")
    conn.execute("DELETE FROM projects")
    conn.commit()
    conn.close()
    flash("Projects cleared and project numbering reset.", "success")
    return redirect(url_for("admin_reset"))


@app.route("/admin/reset/planes", methods=["POST"])
@master_admin_required
def admin_reset_planes():
    """Wipes every Flight School plane (assets with is_flight_asset=1) and
    everything that hangs off one - see _wipe_planes()."""
    return _run_reset(_wipe_planes, "Flight School planes cleared.")


# ---- Flight School resets -------------------------------------------------
# Each helper wipes one slice of the Fly with Kate! side and takes care of
# the rows pointing at it first (foreign keys are ON), so the buttons can be
# used one at a time or all together by "Reset All Flight School Data".

def _ids_placeholders(ids):
    return ",".join("?" for _ in ids)


def _remove_flight_logins(conn, user_ids, role):
    """After deleting student/CFI profiles, deal with their master logins:
    a login that only existed for that flight role is deleted (with its
    phone-alert devices), so the same username can be reused later. A login
    that also has shop access, another flight profile, or is a master
    admin is kept and just loses the flight role."""
    for uid in {u for u in user_ids if u}:
        user = conn.execute("SELECT id, is_master_admin, shop_role, flight_role FROM users WHERE id = ?",
                            (uid,)).fetchone()
        if not user:
            continue
        still_linked = conn.execute("""SELECT (SELECT COUNT(*) FROM cfis WHERE user_id = ?)
                                             + (SELECT COUNT(*) FROM students WHERE user_id = ?) AS n""",
                                    (uid, uid)).fetchone()["n"]
        if not user["is_master_admin"] and not (user["shop_role"] or "").strip() and not still_linked:
            conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM push_pending WHERE user_id = ?", (uid,))
            conn.execute("UPDATE cfis SET user_id = NULL WHERE user_id = ?", (uid,))
            conn.execute("UPDATE students SET user_id = NULL WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        elif user["flight_role"] == role and not still_linked:
            conn.execute("UPDATE users SET flight_role = NULL WHERE id = ?", (uid,))


def _wipe_flight_log(conn, where="1=1", params=()):
    """Deletes logged flights. Billing entries and each flight's pilot
    logbook page (Flight Academy - pilot_logbook.flight_id, a hard foreign
    key) are kept (so balances and a student's logged hours don't change)
    but un-linked from the deleted flight."""
    ids = [r["id"] for r in conn.execute(f"SELECT id FROM flights WHERE {where}", params).fetchall()]
    if ids:
        ph = _ids_placeholders(ids)
        conn.execute(f"UPDATE student_ledger SET flight_id = NULL WHERE flight_id IN ({ph})", ids)
        conn.execute(f"UPDATE pilot_logbook SET flight_id = NULL WHERE flight_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM flights WHERE id IN ({ph})", ids)
    return len(ids)


def _wipe_schedule(conn, where="1=1", params=()):
    """Deletes bookings (scheduled, pending approval, denied, cancelled).
    Logged flights that were started from a booking, and waitlist offers
    made when a booking was cancelled (waitlist_offers.cancelled_flight_id,
    a hard foreign key), are kept but un-linked from the deleted booking."""
    ids = [r["id"] for r in conn.execute(f"SELECT id FROM scheduled_flights WHERE {where}", params).fetchall()]
    if ids:
        ph = _ids_placeholders(ids)
        conn.execute(f"UPDATE flights SET scheduled_flight_id = NULL WHERE scheduled_flight_id IN ({ph})", ids)
        conn.execute(f"UPDATE waitlist_offers SET cancelled_flight_id = NULL WHERE cancelled_flight_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM notification_log WHERE category = 'flight_reminder' AND ref_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM flight_alerts WHERE scheduled_flight_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM scheduled_flights WHERE id IN ({ph})", ids)
    return len(ids)


def _wipe_billing(conn):
    """Clears every student's account history and sets all balances to $0."""
    conn.execute("DELETE FROM student_ledger")
    conn.execute("UPDATE students SET balance = 0")


def _wipe_students(conn):
    """Deletes every student (not station accounts like "Shop") with their
    flights, bookings and account history, plus logins that were only for
    being a student."""
    rows = conn.execute("SELECT id, user_id FROM students WHERE is_station = 0").fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        ph = _ids_placeholders(ids)
        conn.execute(f"DELETE FROM student_ledger WHERE student_id IN ({ph})", ids)
        _wipe_flight_log(conn, f"student_id IN ({ph})", ids)
        _wipe_schedule(conn, f"student_id IN ({ph})", ids)
        # Cancellation waitlist rows are the student's own requests/offers -
        # hard foreign keys to students, so they go with the student.
        conn.execute(f"DELETE FROM waitlist_offers WHERE student_id IN ({ph})", ids)
        conn.execute(f"UPDATE waitlist_offers SET waitlist_id = NULL WHERE waitlist_id IN "
                     f"(SELECT id FROM flight_waitlist WHERE student_id IN ({ph}))", ids)
        conn.execute(f"DELETE FROM flight_waitlist WHERE student_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM field_change_log WHERE entity_type = 'student' AND entity_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM students WHERE id IN ({ph})", ids)
        _remove_flight_logins(conn, [r["user_id"] for r in rows], "student")
    return len(ids)


def _wipe_instructors(conn):
    """Deletes every instructor profile except master admins' own. Their
    logged flights are kept with no instructor listed; their upcoming
    bookings are kept and flagged "needs review" so a new instructor can
    be picked."""
    ids = _removable_cfi_ids(conn)
    if ids:
        ph = _ids_placeholders(ids)
        user_ids = [r["user_id"] for r in conn.execute(
            f"SELECT user_id FROM cfis WHERE id IN ({ph})", ids).fetchall()]
        conn.execute(f"UPDATE flights SET cfi_id = NULL WHERE cfi_id IN ({ph})", ids)
        conn.execute(f"""UPDATE scheduled_flights SET needs_review = 1,
                           review_reason = 'Instructor was removed by an admin reset - pick a new one'
                         WHERE cfi_id IN ({ph}) AND solo = 0 AND status IN ('scheduled', 'pending_approval')""", ids)
        conn.execute(f"UPDATE scheduled_flights SET cfi_id = NULL WHERE cfi_id IN ({ph})", ids)
        conn.execute(f"UPDATE students SET created_by_cfi_id = NULL WHERE created_by_cfi_id IN ({ph})", ids)
        conn.execute(f"UPDATE flight_waitlist SET cfi_id = NULL WHERE cfi_id IN ({ph})", ids)
        conn.execute(f"UPDATE waitlist_offers SET cfi_id = NULL WHERE cfi_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM field_change_log WHERE entity_type = 'cfi' AND entity_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM cfis WHERE id IN ({ph})", ids)
        _remove_flight_logins(conn, user_ids, "cfi")
    return len(ids)


def _wipe_planes(conn):
    """Deletes every Flight School plane and everything hanging off it -
    flights, bookings, maintenance items/log, to-dos, compression checks and
    plane photos. Shop projects that referenced a plane are kept, un-linked."""
    plane_ids = [r["id"] for r in conn.execute("SELECT id FROM assets WHERE is_flight_asset = 1").fetchall()]
    if plane_ids:
        ph = _ids_placeholders(plane_ids)
        _wipe_flight_log(conn, f"asset_id IN ({ph})", plane_ids)
        _wipe_schedule(conn, f"asset_id IN ({ph})", plane_ids)
        # Waitlist offers are for a slot on that plane (asset_id NOT NULL) so
        # they go; a waitlist request just loses its plane preference.
        conn.execute(f"DELETE FROM waitlist_offers WHERE asset_id IN ({ph})", plane_ids)
        conn.execute(f"UPDATE flight_waitlist SET asset_id = NULL WHERE asset_id IN ({ph})", plane_ids)
        item_ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM maintenance_items WHERE asset_id IN ({ph})", plane_ids).fetchall()]
        if item_ids:
            conn.execute(f"DELETE FROM maintenance_log WHERE item_id IN ({_ids_placeholders(item_ids)})", item_ids)
        conn.execute(f"DELETE FROM maintenance_items WHERE asset_id IN ({ph})", plane_ids)
        conn.execute(f"DELETE FROM plane_todos WHERE asset_id IN ({ph})", plane_ids)
        conn.execute(f"DELETE FROM compression_checks WHERE asset_id IN ({ph})", plane_ids)
        _delete_photo_files(conn, f"asset_id IN ({ph}) AND project_id IS NULL AND part_id IS NULL", plane_ids)
        conn.execute(f"DELETE FROM photos WHERE asset_id IN ({ph}) AND project_id IS NULL AND part_id IS NULL", plane_ids)
        conn.execute(f"UPDATE photos SET asset_id = NULL WHERE asset_id IN ({ph})", plane_ids)
        conn.execute(f"DELETE FROM field_change_log WHERE entity_type = 'plane' AND entity_id IN ({ph})", plane_ids)
        conn.execute(f"UPDATE projects SET asset_id = NULL WHERE asset_id IN ({ph})", plane_ids)
        conn.execute(f"DELETE FROM assets WHERE id IN ({ph})", plane_ids)
    return len(plane_ids)


def _run_reset(fn, message):
    """Runs one reset helper in a single transaction - all or nothing - and
    reports back on the Reset Data page."""
    conn = get_db()
    try:
        fn(conn)
        conn.commit()
        flash(message, "success")
    except Exception as e:  # keep the page usable and say what went wrong
        conn.rollback()
        app.logger.exception("Reset failed")
        flash(f"Reset didn't run - nothing was deleted. ({e})", "danger")
    finally:
        conn.close()
    return redirect(url_for("admin_reset") + "#flight-school")


@app.route("/admin/reset/schedule", methods=["POST"])
@master_admin_required
def admin_reset_schedule():
    return _run_reset(_wipe_schedule, "Schedule cleared - all bookings and requests deleted.")


@app.route("/admin/reset/billing", methods=["POST"])
@master_admin_required
def admin_reset_billing():
    return _run_reset(_wipe_billing, "Student account history cleared and every balance set to $0.")


@app.route("/admin/reset/students", methods=["POST"])
@master_admin_required
def admin_reset_students():
    return _run_reset(_wipe_students, "Students cleared, with their flights, bookings and account history.")


@app.route("/admin/reset/instructors", methods=["POST"])
@master_admin_required
def admin_reset_instructors():
    return _run_reset(_wipe_instructors, "Instructors cleared (master admins' own instructor profiles were kept).")


@app.route("/admin/reset/flight_school", methods=["POST"])
@master_admin_required
def admin_reset_flight_school():
    """The big one: every Flight School slice above in one go. Needs RESET
    typed into the box as well as the pop-up, since it can't be undone."""
    if request.form.get("confirm_text", "").strip().upper() != "RESET":
        flash('Type RESET in the box to confirm resetting all Flight School data.', "danger")
        return redirect(url_for("admin_reset") + "#flight-school")

    def _everything(conn):
        _wipe_schedule(conn)
        _wipe_flight_log(conn)
        _wipe_billing(conn)
        _wipe_students(conn)
        _wipe_instructors(conn)
        _wipe_planes(conn)
        conn.execute("DELETE FROM push_pending")
        conn.execute("DELETE FROM adsb_track_points")
        conn.execute("DELETE FROM notification_log WHERE category = 'flight_reminder'")
    return _run_reset(_everything, "All Flight School data reset. Shop data and master admin logins were not touched.")


def _start_session_alert_loop(debug_mode):
    """Runs flight.check_session_alerts() roughly once a minute for as long
    as the app is up - the source of the "30 min left" / "time's up" /
    "running late" phone push alerts (see flight.py / push.py).

    debug_mode must be the same value about to be passed to app.run(debug=...)
    below - this runs before app.run() itself sets app.debug, so app.debug
    isn't reliable yet and the caller has to hand it in explicitly.

    Only matters when debug_mode is True: Flask's debug auto-reloader
    re-execs this whole script in a child process, so without this guard
    the loop would start once in the reloader's parent/monitor process and
    again in the actual serving child, double-sending every alert.
    WERKZEUG_RUN_MAIN is only ever "true" in the real serving process,
    unset in the parent/monitor one. In production (debug_mode False,
    reloader off) there's only ever one process, so the loop always
    starts - checking WERKZEUG_RUN_MAIN alone, without also checking
    debug_mode, would wrongly skip starting it there too."""
    if debug_mode and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    def _loop():
        while True:
            try:
                check_session_alerts()
            except Exception:
                app.logger.exception("Session alert check failed")
            try:
                run_balance_hold_release_check()
            except Exception:
                app.logger.exception("Balance hold release check failed")
            try:
                run_wave_payment_check()
            except Exception:
                app.logger.exception("Wave payment check failed")
            time.sleep(60)

    threading.Thread(target=_loop, daemon=True).start()


if __name__ == "__main__":
    if not os.path.exists(os.path.join("instance", "shopinv.db")):
        init_db()
    else:
        init_db()  # safe: uses IF NOT EXISTS

    # Off by default - debug mode's interactive in-browser console lets
    # anyone who triggers an unhandled error on the running app get a
    # Python shell on the Pi, which is too dangerous to leave on for the
    # always-on production service. Errors are still fully captured either
    # way (see got_request_exception / instance/error_logs/ above, and
    # Admin -> System), so nothing about diagnosing a problem depends on
    # this being on. Set OPSHUB_DEBUG=1 in the environment for local
    # development only, never on the Pi.
    debug_mode = os.environ.get("OPSHUB_DEBUG") == "1"

    _start_session_alert_loop(debug_mode)

    cert_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cert.pem")
    key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        print("Starting with HTTPS (self-signed cert) so phone cameras can scan.")
        app.run(host="0.0.0.0", port=5050, debug=debug_mode, ssl_context=(cert_path, key_path), threaded=True)
    else:
        app.run(host="0.0.0.0", port=5050, debug=debug_mode, threaded=True)
