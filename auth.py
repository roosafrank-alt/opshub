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

from datetime import datetime, timedelta, timezone

from flask import session, redirect, url_for, flash, request
from werkzeug.security import check_password_hash

from db import get_db, ensure_flight_profile, user_shop_roles, user_flight_roles


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
    session["groundschool_access"] = bool(user_row["groundschool_access"] or user_row["academy_access"])
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
    elif user_row["shop_role"] == "admin" or user_row["is_master_admin"]:
        cfi = conn.execute("SELECT id FROM cfis WHERE user_id = ?", (user_row["id"],)).fetchone()
        if cfi:
            session["cfi_id"] = cfi["id"]
    conn.close()


def log_out_user():
    session.clear()


def current_user(conn):
    uid = session.get("user_id")
    if not uid:
        return None
    return conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


LOGIN_NEEDED_MSG = "Please log in to continue."


def login_next_url():
    """The page a signed-out person was trying to open, carried along to the
    login page as ?next= so they land back on it after logging in (SEAM-4 /
    SEAM-16). Only a GET to a page is worth coming back to."""
    if request.method != "GET":
        return None
    path = request.full_path.rstrip("?") if request.query_string else request.path
    return path if path and path != "/" else None


def safe_next(target):
    """Only a path inside this app (never another site) may be returned to."""
    target = (target or "").strip()
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def _no_access_redirect():
    flash(LOGIN_NEEDED_MSG, "danger")
    nxt = login_next_url()
    return redirect(url_for("home_launcher", **({"next": nxt} if nxt else {})))


def refresh_or_expire_session():
    """QA fix (qa-session-keeps-old-access): sessions last up to 180 days
    (PERMANENT_SESSION_LIFETIME, app.py), so without this a deactivated
    account, or one whose roles were just changed in Admin > Accounts,
    kept its OLD access until it happened to log out and back in. Called
    right after the existing `session.get("user_id")` check in every
    login-gated decorator (here and in flight.py), before any role check,
    so every protected page re-checks the signed-in account against the
    `users` table. Does nothing (no query) when there's no signed-in
    account. Returns a redirect response - the caller must return it
    immediately - when the account is gone or deactivated; otherwise
    refreshes the session's mirrored role fields from the fresh row and
    returns None ("carry on").

    It deliberately does NOT touch the live role fields while a role switch
    (_view_as_real, start_view_as) is active, since those are intentionally
    showing a DIFFERENT role than the account's real one - overwriting them
    here would silently cancel the switch on every request. Instead it
    refreshes the _view_as_real snapshot itself, so exiting the switch lands
    on up-to-date real roles instead of ones frozen from whenever it
    started."""
    uid = session.get("user_id")
    if not uid:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    if not row or not row["active"]:
        session.clear()
        flash("You've been signed out because this account is no longer active.", "danger")
        return redirect(url_for("home_launcher"))
    fresh = dict(is_master_admin=bool(row["is_master_admin"]), shop_role=row["shop_role"],
                flight_role=row["flight_role"], can_bill=bool(row["can_bill"]))
    preview = session.get("_view_as_real")
    if preview is not None:
        preview.update(fresh)
        session["_view_as_real"] = preview
    else:
        session.update(fresh)
    session["academy_access"] = bool(row["academy_access"])
    session["groundschool_access"] = bool(row["groundschool_access"] or row["academy_access"])
    return None


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return _no_access_redirect()
        expired = refresh_or_expire_session()
        if expired:
            return expired
        return f(*args, **kwargs)
    return wrapper


def master_admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return _no_access_redirect()
        expired = refresh_or_expire_session()
        if expired:
            return expired
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
            expired = refresh_or_expire_session()
            if expired:
                return expired
            if session.get("is_master_admin") or session.get("shop_role") in roles:
                return f(*args, **kwargs)
            if not session.get("shop_role"):
                flash("You don't have access to Winds Aloft.", "danger")
                return redirect(url_for("flight.dashboard"))
            flash("You don't have access to that part of Winds Aloft.", "danger")
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
# "Switch role" (the header chips) - lets a master admin temporarily use a
# program as a lower access level, without logging out of their own account. Which
# levels make sense depends on the program: Shop Inventory's Admin/Tech/
# Student are pure role flags (shop_role_required/can_see_shop_costs), so
# faking one is just a session swap. Flight School's CFI/Student views are
# per-person (a CFI's dashboard shows their own students, a student's shows
# their own logbook), so previewing one also has to point at a real cfis/
# students row - see _pick_view_as_flight_profile - rather than just a role
# label, or pages that expect one would break. Either way the real session
# fields are stashed under _view_as_real to restore on exit - captured once,
# on the very first switch, so switching straight from one role to another
# (e.g. CFI to Student, without exiting back to the real admin view first)
# always restores the true admin session. _view_as_program separately tracks
# which program's switch is active right now, for the chips to show the
# current role and the way back on the right program. This is the ONLY
# look-as-someone feature (HUB-03): the old look-only "View as someone" and
# the owner preview are gone.
# ---------------------------------------------------------------------------

SHOP_VIEW_AS_LEVELS = {"admin": "Admin", "tech": "Tech", "apprentice": "Apprentice", "inspector": "Inspector"}
FLIGHT_VIEW_AS_LEVELS = {"cfi": "CFI", "student": "Student"}
# Every session field a role switch (shop or flight) can touch - the full
# baseline snapshotted on first entry, so exiting always restores everything
# regardless of which programs were switched along the way.
_VIEW_AS_SNAPSHOT_KEYS = ("is_master_admin", "shop_role", "flight_role", "cfi_id", "student_id", "can_bill")


# Idea "view as for multi-role accounts": someone who holds more than one
# role in a program (ticked separately in Admin > Accounts - e.g. Admin AND
# Inspector, or CFI AND Student) gets the same chips, limited to their own
# roles, and switching uses the app for real as that role. Clicking the chip
# for your normal role (Admin for a master admin) goes straight back to your
# normal view - no separate "Back to admin" step.


def _real(key):
    """The signed-in account's own session value, even mid-preview."""
    real = session.get("_view_as_real")
    return real.get(key) if real else session.get(key)


def real_is_master_admin():
    return bool(_real("is_master_admin"))


def _own_roles(conn, program):
    uid = session.get("user_id")
    if not uid:
        return []
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        return []
    return user_shop_roles(row) if program == "shop" else user_flight_roles(row)


def home_view_as_level(program):
    """The chip that means "my normal view" in this program: Admin for a
    master admin, otherwise the account's own main role there."""
    if real_is_master_admin():
        return "admin"
    return _real("shop_role") if program == "shop" else _real("flight_role")


def allowed_view_as_levels(conn, program):
    """{level: label} this account may switch to in 'shop' or 'flight' -
    every level for a master admin, just their own roles for anyone else
    (and nothing at all when they only have one role there)."""
    levels = SHOP_VIEW_AS_LEVELS if program == "shop" else FLIGHT_VIEW_AS_LEVELS
    if real_is_master_admin():
        return dict(levels)
    own = [r for r in _own_roles(conn, program) if r in levels]
    if len(own) < 2:
        return {}
    return {r: levels[r] for r in own}


def view_as_chips(conn, program):
    """The header's chip row for one program: a list of dicts with
    level, label and action - 'exit' (back to your normal view), 'current'
    (the view you're in now) or 'start' (switch to it)."""
    levels = allowed_view_as_levels(conn, program)
    if not levels:
        return []
    home = home_view_as_level(program)
    previewing = bool(session.get("_view_as_real"))
    here = session.get("_view_as_program") == program if previewing else True
    current = (session.get("shop_role") if program == "shop" else session.get("flight_role")) if previewing and here else None
    chips = []
    if home not in levels:
        # A master admin on Flight School: CFI/Student are previews, and
        # "Admin" is the way back.
        chips.append(dict(level=home, label="Admin", action="exit" if previewing else "current"))
    for level, label in levels.items():
        if level == home:
            action = "exit" if previewing else "current"
        elif level == current:
            action = "current"
        else:
            action = "start"
        chips.append(dict(level=level, label=label, action=action))
    return chips


def _pick_view_as_flight_profile(conn, level):
    """A real cfis/students row to back a Flight School preview: the
    account's own linked profile at that level if they have one, else (for a
    master admin only) the first active one alphabetically. Returns
    (id, name), or (None, None) if there's nobody to preview as."""
    table = "cfis" if level == "cfi" else "students"
    own_user_id = session.get("user_id")
    if own_user_id:
        own = conn.execute(f"SELECT id, name FROM {table} WHERE user_id = ? AND active = 1", (own_user_id,)).fetchone()
        if own:
            return own["id"], own["name"]
    if not real_is_master_admin():
        return None, None
    first = conn.execute(f"SELECT id, name FROM {table} WHERE active = 1 ORDER BY name LIMIT 1").fetchone()
    return (first["id"], first["name"]) if first else (None, None)


def start_view_as(conn, program, level):
    """A master admin (any level, either program), or an account with
    several roles in a program (just its own roles) - either one already
    switched can switch straight to a different role without exiting first.
    program is 'shop' or 'flight'; level is one of that program's own
    *_VIEW_AS_LEVELS keys. Returns (ok, error_message_or_None)."""
    if program not in ("shop", "flight"):
        return False, None
    if level not in allowed_view_as_levels(conn, program):
        return False, None
    person_id = person_name = None
    if program == "flight":
        person_id, person_name = _pick_view_as_flight_profile(conn, level)
        if not person_id:
            return False, f"There's no active {FLIGHT_VIEW_AS_LEVELS[level].lower()} on file yet to switch to."

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
    if program == "shop":
        return SHOP_VIEW_AS_LEVELS.get(session.get("shop_role"))
    role_label = FLIGHT_VIEW_AS_LEVELS.get(session.get("flight_role"))
    name = session.get("_view_as_person_name")
    # Your own profile needs no name after it - only a master admin
    # previewing someone else's does.
    if name and role_label and name != session.get("user_name"):
        return f"{role_label} ({name})"
    return role_label


def view_as_active_program():
    """'shop' | 'flight' | None - which program's role switch is active
    right now."""
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
    """True only for the REAL signed-in account (see real_user_id)."""
    uid = real_user_id()
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
    _fill_session(user_row, customer_row)


def _fill_session(user_row, customer_row):
    """Everything log_in_combined puts in the session for these accounts -
    (was also used by the old "view as a person" feature, now gone), building the
    exact same session a real login by that person would get."""
    if user_row:
        session["user_id"] = user_row["id"]
        session["user_name"] = user_row["name"]
        session["is_master_admin"] = bool(user_row["is_master_admin"])
        session["shop_role"] = user_row["shop_role"]
        session["flight_role"] = user_row["flight_role"]
        session["can_bill"] = bool(user_row["can_bill"])
        session["academy_access"] = bool(user_row["academy_access"])
        session["groundschool_access"] = bool(user_row["groundschool_access"] or user_row["academy_access"])
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
        elif user_row["shop_role"] == "admin" or user_row["is_master_admin"]:
            # The unbilled, never-bookable cfis row ensure_flight_profile
            # just made sure exists for them (idea "shop admin skip launcher";
            # HUB-01 gives a master admin with no Fly with Kate! role the same).
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


def session_programs():
    """Every program this session can open, in picker order, for the
    header's Programs button (SEAM-2 / SEAM-12) and the picker itself: a
    list of dicts with key, name, icon (bootstrap icon class) and endpoint.
    Mirrors home_launcher.html's own tile conditions, same as
    account_program_count() below."""
    is_master_admin = session.get("is_master_admin")
    is_admin_like = bool(is_master_admin or session.get("shop_role") == "admin")
    progs = []
    if is_master_admin or session.get("shop_role"):
        progs.append(dict(key="shop", name="Winds Aloft", icon="bi-tools", endpoint="dashboard"))
    if is_master_admin or session.get("flight_role") or is_admin_like:
        progs.append(dict(key="flight", name="Fly with Kate!", icon="bi-airplane-engines", endpoint="flight.index"))
    if is_master_admin or session.get("academy_access"):
        progs.append(dict(key="academy", name="Flight Academy", icon="bi-mortarboard", endpoint="academy_page"))
    if is_admin_like:
        progs.append(dict(key="customers", name="Customers", icon="bi-people", endpoint="customers_list"))
    if session.get("customer_id"):
        progs.append(dict(key="owner", name="My Aircraft", icon="bi-airplane", endpoint="customer.customer_dashboard"))
    if is_master_admin:
        progs.append(dict(key="admin", name="Admin", icon="bi-shield-lock-fill", endpoint="admin_home"))
    return progs


def account_program_count():
    """How many of the program tiles (Fly with Kate!, Winds Aloft, Flight
    Academy, Customers, My Aircraft) this session's account can see - mirrors
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
    if is_admin_like:
        count += 1  # Customers (SEAM-9)
    if session.get("customer_id"):
        count += 1  # My Aircraft
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
    if is_admin_like:
        return "customers_list"
    if session.get("customer_id"):
        return "customer.customer_dashboard"
    return None


def _refresh_or_expire_customer_session():
    """Same idea as refresh_or_expire_session, for the separate customer
    portal account system (own table, own session key: customer_id).
    There are no roles to refresh here - just whether the account is still
    active - so it's its own small helper rather than folded into the
    users-table one above."""
    cid = session.get("customer_id")
    if not cid:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    conn.close()
    if not row or not row["active"]:
        session.clear()
        flash("You've been signed out because this account is no longer active.", "danger")
        return redirect(url_for("home_launcher"))
    return None


def customer_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("customer_id"):
            return _no_access_redirect()
        expired = _refresh_or_expire_customer_session()
        if expired:
            return expired
        return f(*args, **kwargs)
    return wrapper



def person_view_active():
    """The look-only "View as someone" feature was removed (HUB-03: the
    Switch role chips are the only way to look at the app as someone else).
    Kept as a stub so the couple of places that still ask (flight.py's
    alerts page and phone-alert pickup) keep working unchanged: it is never
    active now."""
    return False


def real_user_id():
    """The real signed-in account's own users.id - even mid role switch
    (session['user_id'] itself is never part of the _view_as_real snapshot,
    so this only matters if that ever changes). Needed anywhere that must
    know who is really at the keyboard, such as the owner-account lock in
    Admin > Accounts - see owner_user_id/is_owner above."""
    real = session.get("_view_as_real")
    if real and "user_id" in real:
        return real.get("user_id")
    return session.get("user_id")


# ---------------------------------------------------------------------------
# Login lockout. The login form (and the old /flight/login address) call
# login_allowed() before checking a password, login_failed() after a
# wrong one and login_succeeded() after a right one. Never permanent: 5 wrong
# passwords in a row lock that account name for 15 min, then 1 hr, 4 hr, 16 hr,
# capped at 24 hr. Unknown names are tracked the same way, so the behaviour
# doesn't reveal which usernames exist.
# ---------------------------------------------------------------------------
LOCKOUT_FAILS = 5
LOCKOUT_MINUTES = (15, 60, 240, 960, 1440)
SOURCE_WINDOW_MIN = 10
SOURCE_MAX_NAMES = 10   # this many different names failing from one source within the window slows it down
LOGIN_BLOCKED_MSG = "Incorrect username or password, or too many tries. Please try again later."
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _utc():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def login_source():
    """Who is really asking. Behind Tailscale Funnel remote_addr is always the
    local proxy, so X-Forwarded-For (the last entry, the one the proxy itself
    added) is only trusted when the request comes from localhost."""
    addr = request.remote_addr or ""
    if addr in _LOOPBACK:
        xff = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        if xff:
            return xff[-1][:64]
    return addr[:64]


def _acct(username):
    return (username or "").strip().lower()[:200]


def _log_attempt(conn, username, source, kind):
    conn.execute("INSERT INTO login_attempts (at, username, source, kind) VALUES (?,?,?,?)",
                 (_iso(_utc()), (username or "").strip()[:200], source, kind))


def login_allowed(username):
    """False while this account name is locked, or this source has been trying
    too many different names. The proxy's own address is never limited."""
    now = _iso(_utc())
    conn = get_db()
    try:
        row = conn.execute("SELECT locked_until FROM login_lockouts WHERE account = ?",
                           (_acct(username),)).fetchone()
        if row and row["locked_until"] and row["locked_until"] > now:
            return False
        source = login_source()
        if source and source not in _LOOPBACK:
            since = _iso(_utc() - timedelta(minutes=SOURCE_WINDOW_MIN))
            n = conn.execute("SELECT COUNT(DISTINCT lower(username)) FROM login_attempts "
                             "WHERE source = ? AND kind = 'failed' AND at >= ?", (source, since)).fetchone()[0]
            if n >= SOURCE_MAX_NAMES:
                return False
        return True
    finally:
        conn.close()


def login_failed(username):
    """Records a wrong password; locks the account on the 5th in a row."""
    acct = _acct(username)
    source = login_source()
    now = _utc()
    conn = get_db()
    try:
        _log_attempt(conn, username, source, "failed")
        row = conn.execute("SELECT * FROM login_lockouts WHERE account = ?", (acct,)).fetchone()
        fails = (row["fails"] if row else 0) + 1
        lockouts = row["lockouts"] if row else 0
        locked_until = None
        if fails >= LOCKOUT_FAILS:
            mins = LOCKOUT_MINUTES[min(lockouts, len(LOCKOUT_MINUTES) - 1)]
            locked_until = _iso(now + timedelta(minutes=mins))
            lockouts += 1
            fails = 0
            _log_attempt(conn, username, source, "locked")
        conn.execute("INSERT INTO login_lockouts (account, fails, lockouts, locked_until) VALUES (?,?,?,?) "
                     "ON CONFLICT(account) DO UPDATE SET fails=excluded.fails, lockouts=excluded.lockouts, "
                     "locked_until=excluded.locked_until", (acct, fails, lockouts, locked_until))
        conn.commit()
    finally:
        conn.close()


def login_succeeded(username):
    """A correct login wipes the count (and the repeat-lockout history)."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM login_lockouts WHERE account = ?", (_acct(username),))
        conn.commit()
    finally:
        conn.close()


def unlock_account(account):
    conn = get_db()
    try:
        conn.execute("DELETE FROM login_lockouts WHERE account = ?", (_acct(account),))
        conn.commit()
    finally:
        conn.close()
