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


def _owner_preview_blocked():
    """True while a master admin is previewing 'My Aircraft' as a real
    owner (start_view_as(conn, 'owner', ...)). That preview exists to show
    exactly what a real customer portal login would show, so it must never
    reach a staff page - not even ones only gated on "is someone logged
    in?" - regardless of whatever shop_role/flight_role/cfi_id/student_id
    the previewing admin's own real account happens to carry (those aren't
    reset for an owner preview the way they are for a shop/flight one,
    since My Aircraft has no role of its own to swap in). Every staff-facing
    decorator below checks this first and fails closed, rather than relying
    on each of those fields separately staying "off" (QA "view": a stale
    shop_role/flight_role/cfi_id let an owner preview reach other tabs and
    programs it was never supposed to)."""
    return session.get("_view_as_program") == "owner" and bool(session.get("_view_as_real"))


def _owner_preview_redirect():
    flash("Exit the owner preview to use staff pages.", "info")
    return redirect(url_for("customer.customer_dashboard"))


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

    Only touches the CURRENTLY ACTIVE session's mirrored fields (whatever
    `session.get(...)` returns right now) - during "View as a person"
    (_PERSON_VIEW_KEY) that's the person being viewed, which is exactly
    right: it's their access that must never go stale. It deliberately
    does NOT touch the live role fields while a role/owner preview
    (_view_as_real, start_view_as) is active, since those are
    intentionally showing a DIFFERENT role than the account's real one -
    overwriting them here would silently cancel the preview on every
    request. Instead it refreshes the _view_as_real snapshot itself, so
    exiting the preview lands on up-to-date real roles instead of ones
    frozen from whenever the preview started."""
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
        if _owner_preview_blocked():
            return _owner_preview_redirect()
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
        if _owner_preview_blocked():
            return _owner_preview_redirect()
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
            if _owner_preview_blocked():
                return _owner_preview_redirect()
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
    """A master admin (any level, any program, or a specific owner), or an
    account with several roles in a program (just its own roles) - either
    one already previewing can switch straight to a different preview
    without exiting first (idea "view change"). program is 'shop', 'flight'
    or 'owner'; level is one of that program's own *_VIEW_AS_LEVELS keys
    (or, for 'owner', a customer id). Returns (ok, error_message_or_None)."""
    if program == "shop" or program == "flight":
        if level not in allowed_view_as_levels(conn, program):
            return False, None
    if program == "flight":
        person_id, person_name = _pick_view_as_flight_profile(conn, level)
        if not person_id:
            return False, f"There's no active {FLIGHT_VIEW_AS_LEVELS[level].lower()} on file yet to preview as."
    elif program == "owner":
        if not real_is_master_admin():
            return False, None
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
    elif program != "shop":
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
    if person_view_active() and not session.get("_view_as_real"):
        return f"{person_view_name()} (view only)"
    if not session.get("_view_as_real"):
        return None
    program = session.get("_view_as_program")
    if program == "owner":
        return f"Owner ({session.get('customer_name')})"
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
    """'shop' | 'flight' | 'owner' | None - which program's preview is
    active right now."""
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
    """True only for the REAL signed-in account (see real_user_id) - a
    master admin "viewing as" the owner is not treated as the owner."""
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
    shared with "view as a person" (start_view_as_person), which builds the
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
        return redirect(url_for("customer.customer_login"))
    return None


def customer_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("customer_id"):
            flash("Log in to see your aircraft.", "danger")
            return redirect(url_for("customer.customer_login"))
        expired = _refresh_or_expire_customer_session()
        if expired:
            return expired
        return f(*args, **kwargs)
    return wrapper



# ---------------------------------------------------------------------------
# "View as a person" (idea: an admin sees anybody's exact app without
# logging out and typing their password). Picked from the account menu.
# Unlike the role chips above - which keep the admin's own account and just
# swap a role in - this builds the session a real login by that person
# would get (_fill_session, the same code log_in_combined uses), so every
# page shows their own data: their dashboard, their students, their
# account. The admin's whole session is kept under _person_view_real and
# put back on exit.
#
# It's VIEW ONLY: while active, app.block_writes_in_person_view refuses
# every request that would change something (anything but GET/HEAD/OPTIONS),
# apart from going back to your own view or picking someone else. Otherwise
# an admin could change someone's password, or have payroll, charges and
# sign-offs recorded under that person's name.
# ---------------------------------------------------------------------------

_PERSON_VIEW_KEY = "_person_view_real"


def person_view_active():
    return bool(session.get(_PERSON_VIEW_KEY))


def person_view_name():
    return session.get("_person_view_name") if person_view_active() else None


def can_view_as_person():
    """Only a real master admin - checked against their own saved session
    while a person view is active (the session then belongs to the person
    being viewed) and ignoring any role preview (_view_as_real)."""
    real = session.get(_PERSON_VIEW_KEY)
    if real:
        base = real.get("_view_as_real") or real
        return bool(base.get("is_master_admin"))
    return real_is_master_admin()


def real_user_id():
    """The real signed-in account's own users.id - even mid preview, whether
    that's a role preview (_view_as_real, session['user_id'] itself is never
    part of that snapshot so this only matters if that ever changes), viewing
    as another person (_PERSON_VIEW_KEY swaps session['user_id'] to the
    viewed person's own id), or both at once. Needed anywhere that must know
    who is really at the keyboard, such as the owner-account lock in
    Admin > Accounts - see owner_user_id/is_owner below."""
    base = session.get(_PERSON_VIEW_KEY) or session
    real = base.get("_view_as_real")
    if real and "user_id" in real:
        return real.get("user_id")
    return base.get("user_id")


def _admin_session():
    """The admin's own session to return to: the saved one while a person
    view is active, else the current one - without any role preview."""
    base = dict(session.get(_PERSON_VIEW_KEY) or session)
    for k in ("_flashes", _PERSON_VIEW_KEY, "_person_view_name"):
        base.pop(k, None)
    real = base.pop("_view_as_real", None)
    base.pop("_view_as_program", None)
    base.pop("_view_as_person_name", None)
    if real:
        base.update(real)
    return base


def start_view_as_person(conn, user_id):
    """Returns (ok, error_message_or_None). Viewing yourself just goes back
    to your own view."""
    if not can_view_as_person():
        return False, None
    admin = _admin_session()
    if user_id == admin.get("user_id"):
        stop_view_as_person()
        return True, None
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or not user["active"]:
        return False, "That account isn't active."
    # Same match log_in_combined makes at the login form: a customer
    # account under the same email/username is that person's My Aircraft.
    customer = conn.execute("SELECT * FROM customers WHERE lower(email) = lower(?) AND active = 1",
                            (user["username"],)).fetchone()
    flashes = session.get("_flashes")
    permanent = session.permanent
    session.clear()
    session.permanent = permanent
    if flashes:
        session["_flashes"] = flashes
    _fill_session(user, customer)
    session[_PERSON_VIEW_KEY] = admin
    session["_person_view_name"] = user["name"]
    return True, None


def stop_view_as_person():
    """Back to the admin's own view (with no role preview). Returns False
    if no person view was active."""
    if not person_view_active():
        return False
    admin = _admin_session()
    flashes = session.get("_flashes")
    permanent = session.permanent
    session.clear()
    session.permanent = permanent
    session.update(admin)
    if flashes:
        session["_flashes"] = flashes
    return True
