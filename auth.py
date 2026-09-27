"""Master login shared by Shop Inventory and Flight School.

One account (one username/password) works for both programs. What a person
can see and do in each is driven by `users.shop_role` ('admin'/'tech'/
'student'/None) and `users.flight_role` ('cfi'/'student'/None), plus
`users.is_master_admin` which grants everything everywhere.

Logging in here populates a handful of session keys used across both
app.py and flight.py:
  session['user_id'], ['user_name'], ['is_master_admin'],
  ['shop_role'], ['flight_role']
and, for backward compatibility with the existing (well-tested) Flight
School routes/templates, also session['cfi_id'] / ['student_id'] when the
account has a linked CFI or student profile - those two keep working
exactly as before, just populated from the master login instead of a
separate one.
"""
from functools import wraps

from flask import session, redirect, url_for, flash
from werkzeug.security import check_password_hash

from db import get_db, ensure_flight_profile


def authenticate(username, password):
    """Returns the users row for a valid username/password, or None."""
    username = (username or "").strip()
    if not username or not password:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if not row or not row["active"]:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return row


def log_in_user(user_row, remember=True):
    """Populates the session for an already-authenticated user row.
    remember=True (the default - covers the kiosk display and anyone who
    doesn't see/use the "Remember me" checkbox) uses PERMANENT_SESSION_LIFETIME
    (180 days, app.py) instead of expiring when the browser closes.
    remember=False makes it a plain session cookie that clears when the
    browser/app is fully closed - for a shared or public device."""
    session.clear()
    session.permanent = bool(remember)
    session["user_id"] = user_row["id"]
    session["user_name"] = user_row["name"]
    session["is_master_admin"] = bool(user_row["is_master_admin"])
    session["shop_role"] = user_row["shop_role"]
    session["flight_role"] = user_row["flight_role"]
    session["can_bill"] = bool(user_row["can_bill"])
    session["academy_access"] = bool(user_row["academy_access"])
    session["tour_seen_shop"] = bool(user_row["tour_seen_shop"])
    session["tour_seen_flight"] = bool(user_row["tour_seen_flight"])

    conn = get_db()
    ensure_flight_profile(conn, user_row)
    if user_row["flight_role"] == "cfi":
        cfi = conn.execute("SELECT id FROM cfis WHERE user_id = ?", (user_row["id"],)).fetchone()
        if cfi:
            session["cfi_id"] = cfi["id"]
    elif user_row["flight_role"] == "student":
        student = conn.execute("SELECT id FROM students WHERE user_id = ?", (user_row["id"],)).fetchone()
        if student:
            session["student_id"] = student["id"]
    conn.close()


def log_out_user():
    session.clear()


def current_user(conn):
    uid = session.get("user_id")
    if not uid:
        return None
    return conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def _no_access_redirect():
    flash("Log in to do that.", "danger")
    return redirect(url_for("home_launcher"))


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return _no_access_redirect()
        return f(*args, **kwargs)
    return wrapper


def master_admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return _no_access_redirect()
        if not session.get("is_master_admin"):
            flash("That's for admins only.", "danger")
            return redirect(url_for("home_launcher"))
        return f(*args, **kwargs)
    return wrapper


def shop_role_required(*roles):
    """Allows master admins plus anyone whose shop_role is in `roles`.
    e.g. @shop_role_required('admin', 'tech')

    A flight-only account (no shop_role at all - a CFI or a flight student)
    has no shop page it CAN land on, including the Shop dashboard itself -
    sending it there bounced it straight back here and back again, an
    infinite redirect loop the browser reported as "too many redirects"
    (QA finding ux-shop-link-redirect-loop). That account goes to its own
    Flight School dashboard instead, with a plain "no shop access" message.
    A shop account that's just missing ONE page's role (e.g. a Student on
    an Admin-only page) still has the Shop dashboard to land on, so that
    case is unchanged."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                return _no_access_redirect()
            if session.get("is_master_admin") or session.get("shop_role") in roles:
                return f(*args, **kwargs)
            if not session.get("shop_role"):
                flash("You don't have access to the shop side of OpsHub.", "danger")
                return redirect(url_for("flight.dashboard"))
            flash("You don't have access to that part of Shop Inventory.", "danger")
            return redirect(url_for("dashboard"))
        return wrapper
    return decorator


def can_see_shop_costs():
    """Tech and Student roles don't see prices/costs/inventory value - only
    Admins (and master admins) do."""
    return bool(session.get("is_master_admin") or session.get("shop_role") == "admin")


def can_manage_billing():
    """Flight School: $ totals, outstanding balances, and the Billing page
    are only for CFIs an admin has specifically granted that to (plus
    master admins) - a regular instructor logs flights but doesn't see
    what's owed or been collected."""
    return bool(session.get("is_master_admin") or session.get("can_bill"))


# ---------------------------------------------------------------------------
# "View as" - lets a master admin temporarily browse a program as a lower
# access level sees it, without logging out of their own account. Which
# levels make sense depends on the program: Shop Inventory's Admin/Tech/
# Student are pure role flags (shop_role_required/can_see_shop_costs), so
# faking one is just a session swap. Flight School's CFI/Student views are
# per-person (a CFI's dashboard shows their own students, a student's shows
# their own logbook), so previewing one also has to point at a real cfis/
# students row - see _pick_view_as_flight_profile - rather than just a role
# label, or pages that expect one would break. Either way the real session
# fields are stashed under _view_as_real to restore on exit - captured once,
# on the very first preview, so switching straight from one preview to
# another (e.g. CFI to Student, without exiting back to the real admin
# view first - idea "view change") always restores the true admin session,
# not whichever preview was active just before the switch. _view_as_program
# separately tracks which preview is active right now, for the "View as"
# chips to grey out/show Exit on the right program.
# ---------------------------------------------------------------------------

SHOP_VIEW_AS_LEVELS = {"admin": "Admin", "tech": "Tech", "apprentice": "Apprentice", "inspector": "Inspector"}
FLIGHT_VIEW_AS_LEVELS = {"cfi": "CFI", "student": "Student"}
# Every session field any preview (shop, flight or owner) can touch - the
# full baseline snapshotted on first entry, so exiting always restores
# everything regardless of which programs were previewed along the way.
_VIEW_AS_SNAPSHOT_KEYS = ("is_master_admin", "shop_role", "flight_role", "cfi_id", "student_id",
                          "can_bill", "customer_id", "customer_name")


def _pick_view_as_flight_profile(conn, level):
    """A real cfis/students row to back a Flight School preview: the admin's
    own linked profile at that level if they have one, else the first active
    one alphabetically. Returns (id, name), or (None, None) if the school
    has nobody active at that level yet to preview as."""
    table = "cfis" if level == "cfi" else "students"
    own_user_id = session.get("user_id")
    if own_user_id:
        own = conn.execute(f"SELECT id, name FROM {table} WHERE user_id = ? AND active = 1", (own_user_id,)).fetchone()
        if own:
            return own["id"], own["name"]
    first = conn.execute(f"SELECT id, name FROM {table} WHERE active = 1 ORDER BY name LIMIT 1").fetchone()
    return (first["id"], first["name"]) if first else (None, None)


def start_view_as(conn, program, level):
    """True master admin only - which includes a master admin already
    previewing someone else, who can switch straight to a different
    preview (even a different program) without exiting back to their own
    view first (idea "view change"). program is 'shop', 'flight' or
    'owner'; level is one of that program's own *_VIEW_AS_LEVELS keys (or,
    for 'owner', a customer id). Returns (ok, error_message_or_None)."""
    if not session.get("is_master_admin") and not session.get("_view_as_real"):
        return False, None
    if program == "shop":
        if level not in SHOP_VIEW_AS_LEVELS:
            return False, None
    elif program == "flight":
        if level not in FLIGHT_VIEW_AS_LEVELS:
            return False, None
        person_id, person_name = _pick_view_as_flight_profile(conn, level)
        if not person_id:
            return False, f"There's no active {FLIGHT_VIEW_AS_LEVELS[level].lower()} on file yet to preview as."
    elif program == "owner":
        # level is a customers.id (as a string, from the URL) rather than a
        # role name - My Aircraft has no roles, just "this real owner's
        # view", picked from customers_list rather than a fixed level set.
        try:
            customer_id = int(level)
        except ValueError:
            return False, None
        customer = conn.execute("SELECT id, name FROM customers WHERE id = ? AND active = 1",
                                (customer_id,)).fetchone()
        if not customer:
            return False, "That customer account isn't active."
    else:
        return False, None

    # Baseline snapshot, captured once on the very first preview this
    # session. Every switch after that - same program at a different
    # level, or straight to a different program - re-applies that baseline
    # before layering the new preview on top, so nothing left over from an
    # earlier preview (e.g. a Shop Tech shop_role) lingers once you've
    # switched to previewing Flight School instead.
    if "_view_as_real" not in session:
        session["_view_as_real"] = {k: session.get(k) for k in _VIEW_AS_SNAPSHOT_KEYS}
    for k, v in session["_view_as_real"].items():
        session[k] = v
    session.pop("_view_as_person_name", None)

    session["is_master_admin"] = False
    if program == "shop":
        session["shop_role"] = level
        session["can_bill"] = False
    elif program == "flight":
        session["flight_role"] = level
        session["can_bill"] = False
        session["cfi_id"] = person_id if level == "cfi" else None
        session["student_id"] = person_id if level == "student" else None
        session["_view_as_person_name"] = person_name
    elif program == "owner":
        session["customer_id"] = customer["id"]
        session["customer_name"] = customer["name"]
    session["_view_as_program"] = program
    return True, None


def exit_view_as():
    real = session.pop("_view_as_real", None)
    session.pop("_view_as_person_name", None)
    session.pop("_view_as_program", None)
    if not real:
        return False
    for key, value in real.items():
        session[key] = value
    return True


def viewing_as_label():
    if not session.get("_view_as_real"):
        return None
    program = session.get("_view_as_program")
    if program == "owner":
        return f"Owner ({session.get('customer_name')})"
    if program == "shop":
        return SHOP_VIEW_AS_LEVELS.get(session.get("shop_role"))
    role_label = FLIGHT_VIEW_AS_LEVELS.get(session.get("flight_role"))
    name = session.get("_view_as_person_name")
    return f"{role_label} ({name})" if name and role_label else role_label


def view_as_active_program():
    """'shop' | 'flight' | 'owner' | None - which program's levels the
    "View as" chips should treat as currently active (to grey out/exclude
    that one and show Exit)."""
    if not session.get("_view_as_real"):
        return None
    return session.get("_view_as_program")


# ---------------------------------------------------------------------------
# Owner account: the first master admin. Other master admins can't change
# it (roles, admin flag, active, password, name) or see its password, but
# the owner can always change theirs.
# ---------------------------------------------------------------------------

def owner_user_id(conn=None):
    """The owner's users.id, remembered in app_settings ('owner_user_id').
    The first time it's asked for, it's set to the oldest master admin
    account, so it never moves just because another admin is added."""
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'owner_user_id'").fetchone()
        if row and str(row["value"] or "").isdigit():
            exists = conn.execute("SELECT id FROM users WHERE id = ?", (int(row["value"]),)).fetchone()
            if exists:
                return int(row["value"])
        first = conn.execute("SELECT id FROM users WHERE is_master_admin = 1 ORDER BY id LIMIT 1").fetchone()
        if not first:
            return None
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('owner_user_id', ?)", (str(first["id"]),))
        conn.commit()
        return first["id"]
    finally:
        if own_conn:
            conn.close()


def is_owner(conn=None):
    uid = session.get("user_id")
    return bool(uid) and uid == owner_user_id(conn)


def owner_locked(user_id, conn=None):
    """True when user_id is the owner's account and the person signed in
    isn't the owner - i.e. they may look but not change it."""
    return bool(user_id) and user_id == owner_user_id(conn) and not is_owner(conn)


# ---------------------------------------------------------------------------
# Customer portal login - a completely separate account system from the
# staff `users` table above (own table, own session keys: session['customer_id']
# / ['customer_name'], never user_id). A customer has no shop_role/flight_role
# and no staff routes recognize their session, so there's no way this login
# reaches anything but the customer portal blueprint's own routes, which in
# turn only ever show the aircraft customer_assets links to that customer_id
# - see customer.py.
# ---------------------------------------------------------------------------

def authenticate_customer(email, password):
    """Returns the customers row for a valid email/password, or None."""
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM customers WHERE lower(email) = ?", (email,)).fetchone()
    conn.close()
    if not row or not row["active"]:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return row


def log_in_customer(customer_row):
    session.clear()
    session.permanent = True
    session["customer_id"] = customer_row["id"]
    session["customer_name"] = customer_row["name"]


def log_out_customer():
    session.clear()


def log_in_combined(user_row, customer_row, remember=True):
    """The hub's login form (home_launcher) checks the entered
    username/password against BOTH the staff `users` table and the
    `customers` table, and this logs in with whichever matched. The two
    account systems stay separate (own tables, own session keys, own
    decorators) - this just lets one person hold both a staff/student
    login and a customer login under the same email+password and see
    every tile they have access to at once, without merging the accounts.
    Pass None for whichever didn't match; at least one must be given."""
    session.clear()
    session.permanent = bool(remember)
    if user_row:
        session["user_id"] = user_row["id"]
        session["user_name"] = user_row["name"]
        session["is_master_admin"] = bool(user_row["is_master_admin"])
        session["shop_role"] = user_row["shop_role"]
        session["flight_role"] = user_row["flight_role"]
        session["can_bill"] = bool(user_row["can_bill"])
        session["academy_access"] = bool(user_row["academy_access"])
        session["tour_seen_shop"] = bool(user_row["tour_seen_shop"])
        session["tour_seen_flight"] = bool(user_row["tour_seen_flight"])
        conn = get_db()
        ensure_flight_profile(conn, user_row)
        if user_row["flight_role"] == "cfi":
            cfi = conn.execute("SELECT id FROM cfis WHERE user_id = ?", (user_row["id"],)).fetchone()
            if cfi:
                session["cfi_id"] = cfi["id"]
        elif user_row["flight_role"] == "student":
            student = conn.execute("SELECT id FROM students WHERE user_id = ?", (user_row["id"],)).fetchone()
            if student:
                session["student_id"] = student["id"]
        elif user_row["shop_role"] == "admin" and not user_row["is_master_admin"]:
            # The unbilled, never-bookable cfis row ensure_flight_profile
            # just made sure exists for them (idea "shop admin skip launcher").
            cfi = conn.execute("SELECT id FROM cfis WHERE user_id = ?", (user_row["id"],)).fetchone()
            if cfi:
                session["cfi_id"] = cfi["id"]
        conn.close()
    if customer_row:
        session["customer_id"] = customer_row["id"]
        session["customer_name"] = customer_row["name"]


def current_customer(conn):
    cid = session.get("customer_id")
    if not cid:
        return None
    return conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()


def account_program_count():
    """How many of the program tiles (Fly with Kate!, Winds Aloft, Flight
    Academy, My Aircraft) this session's account can see - mirrors
    home_launcher.html's own tile conditions (QA ux-launcher-single-program).
    A master admin always sees all four, so this is never 1 for them. A
    shop admin also counts Fly with Kate! now (ux-shop-admin-skip-launcher),
    same as the picker's own tile condition."""
    is_master_admin = session.get("is_master_admin")
    is_admin_like = bool(is_master_admin or session.get("shop_role") == "admin")
    count = 0
    if is_master_admin or session.get("flight_role") or is_admin_like:
        count += 1
    if is_master_admin or session.get("shop_role"):
        count += 1
    if is_master_admin or session.get("academy_access"):
        count += 1
    if is_admin_like or session.get("customer_id"):
        count += 1
    return count


def single_program_endpoint():
    """The one endpoint to send an account straight to instead of showing
    'Choose a program', when it has exactly one program tile - None for an
    account with zero (no access yet) or two-plus (a real choice to make)."""
    if account_program_count() != 1:
        return None
    is_master_admin = session.get("is_master_admin")
    is_admin_like = bool(is_master_admin or session.get("shop_role") == "admin")
    if is_master_admin or session.get("flight_role"):
        return "flight.index"
    if is_master_admin or session.get("shop_role"):
        return "dashboard"
    if is_master_admin or session.get("academy_access"):
        return "academy_page"
    if is_admin_like or session.get("customer_id"):
        return "customers_list" if is_admin_like else "customer.customer_dashboard"
    return None


def customer_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("customer_id"):
            flash("Log in to see your aircraft.", "danger")
            return redirect(url_for("customer.customer_login"))
        return f(*args, **kwargs)
    return wrapper

