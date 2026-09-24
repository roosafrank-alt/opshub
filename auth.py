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
    e.g. @shop_role_required('admin', 'tech')"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                return _no_access_redirect()
            if session.get("is_master_admin") or session.get("shop_role") in roles:
                return f(*args, **kwargs)
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


def current_customer(conn):
    cid = session.get("customer_id")
    if not cid:
        return None
    return conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()


def customer_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("customer_id"):
            flash("Log in to see your aircraft.", "danger")
            return redirect(url_for("customer.customer_login"))
        return f(*args, **kwargs)
    return wrapper

