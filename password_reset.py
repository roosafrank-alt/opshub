"""PW-3: "Forgot password?" - an emailed, single-use link that lets someone choose a new password.

How it works (kept deliberately small):
  * /forgot-password asks for a username or email. The answer on screen is always the same
    ("if that matches an account with an email on file, we sent a link"), so nobody can use
    the page to find out who has an account.
  * A link is good for one hour and one use. Only a hash of it is stored.
  * Setting the new password follows the same 8-character rule as My Account, clears any login
    lockout on the account, and wipes the readable copy of the password (PW-2).
  * Someone who has both a staff login and an aircraft-owner login with the same email and the
    same password (HUB-28) gets both changed together, so the one login keeps working.
"""
import hashlib
import secrets
from datetime import timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

import notify
from auth import _acct, _iso, _utc, login_source
from db import get_db

pwreset_bp = Blueprint("pwreset", __name__)

LINK_MINUTES = 60
MAX_LINKS_PER_ACCOUNT_PER_HOUR = 3
MAX_REQUESTS_PER_SOURCE = 10          # per 10 minutes, per address (the Funnel proxy itself is never limited)
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
SENT_MSG = ("If that matches an account with an email address on file, we've emailed a link to choose a new "
            "password. It works for 1 hour. Nothing arrived? Check spam, or ask an admin to set a new password for you.")


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _matches(conn, who):
    """Active accounts this username/email points to: [(kind, row)]."""
    out = []
    for u in conn.execute("SELECT * FROM users WHERE active = 1 AND (lower(username) = ? OR lower(email) = ?)",
                          (who, who)).fetchall():
        out.append(("user", u))
    for c in conn.execute("SELECT * FROM customers WHERE active = 1 AND lower(email) = ?", (who,)).fetchall():
        out.append(("customer", c))
    return out


def _too_many_from_source(conn, source):
    if not source or source in _LOOPBACK:
        return False
    since = _iso(_utc() - timedelta(minutes=10))
    n = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE source = ? AND kind = 'reset' AND at >= ?",
                     (source, since)).fetchone()[0]
    return n >= MAX_REQUESTS_PER_SOURCE


def _send_link(conn, kind, row):
    """Makes and emails one link. Quietly does nothing without an email on file or past the hourly limit."""
    email = (row["email"] or "").strip()
    if not email:
        return
    since = _iso(_utc() - timedelta(hours=1))
    recent = conn.execute("SELECT COUNT(*) FROM password_resets WHERE kind = ? AND account_id = ? AND created_at >= ?",
                          (kind, row["id"], since)).fetchone()[0]
    if recent >= MAX_LINKS_PER_ACCOUNT_PER_HOUR:
        return
    token = secrets.token_urlsafe(32)
    now = _utc()
    conn.execute("INSERT INTO password_resets (kind, account_id, token_hash, created_at, expires_at) VALUES (?,?,?,?,?)",
                 (kind, row["id"], _hash(token), _iso(now), _iso(now + timedelta(minutes=LINK_MINUTES))))
    conn.commit()
    link = request.url_root.rstrip("/") + url_for("pwreset.reset_password", token=token)
    login_name = row["username"] if kind == "user" else email
    body = (f"Hi {row['name']},\n\nSomeone asked to choose a new password for {login_name}.\n\n"
            f"Open this link to choose one (it works once, for {LINK_MINUTES} minutes):\n{link}\n\n"
            "If that wasn't you, ignore this email - your password stays as it is.")
    notify.send_email(notify.get_settings(conn), email, "Choose a new password", body, brand="OpsHub")


@pwreset_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        who = (request.form.get("who") or "").strip().lower()[:200]
        conn = get_db()
        try:
            source = login_source()
            if who and not _too_many_from_source(conn, source):
                conn.execute("INSERT INTO login_attempts (at, username, source, kind) VALUES (?,?,?,?)",
                             (_iso(_utc()), who, source, "reset"))
                conn.commit()
                for kind, row in _matches(conn, who):
                    _send_link(conn, kind, row)
        finally:
            conn.close()
        flash(SENT_MSG, "success")
        return redirect(url_for("home_launcher"))
    return render_template("forgot_password.html")


def _live_link(conn, token):
    row = conn.execute("SELECT * FROM password_resets WHERE token_hash = ?", (_hash(token),)).fetchone()
    if not row or row["used_at"] or row["expires_at"] < _iso(_utc()):
        return None
    return row


def _account(conn, link):
    table = "users" if link["kind"] == "user" else "customers"
    acct = conn.execute(f"SELECT * FROM {table} WHERE id = ? AND active = 1", (link["account_id"],)).fetchone()
    return acct


@pwreset_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    conn = get_db()
    link = _live_link(conn, token)
    acct = _account(conn, link) if link else None
    if not acct:
        conn.close()
        return render_template("reset_password.html", expired=True), 410
    if request.method == "POST":
        from app import password_problem  # late import: app registers this blueprint
        new_password = request.form.get("password", "")
        problem = password_problem(new_password, request.form.get("password_confirm", ""),
                                   request.form.get("password_shown") == "1")
        if not new_password:
            problem = "Type the new password you want."
        if problem:
            conn.close()
            flash(problem, "danger")
            return render_template("reset_password.html", expired=False, who=acct["name"])
        new_hash = generate_password_hash(new_password, method="pbkdf2:sha256")
        old_hash = acct["password_hash"]
        email = (acct["email"] or "").strip().lower()
        if link["kind"] == "user":
            conn.execute("UPDATE users SET password_hash = ?, password_plain = NULL WHERE id = ?", (new_hash, acct["id"]))
            conn.execute("UPDATE cfis SET password_hash = ? WHERE user_id = ?", (new_hash, acct["id"]))
            conn.execute("UPDATE students SET password_hash = ? WHERE user_id = ?", (new_hash, acct["id"]))
            conn.execute("DELETE FROM login_lockouts WHERE account = ?", (_acct(acct["username"]),))   # same connection: a second one would hit 'database is locked'
            sibling_sql, sibling_table = "SELECT id FROM customers WHERE lower(email) = ? AND password_hash = ?", "customers"
        else:
            conn.execute("UPDATE customers SET password_hash = ?, password_plain = NULL WHERE id = ?", (new_hash, acct["id"]))
            sibling_sql, sibling_table = "SELECT id FROM users WHERE lower(email) = ? AND password_hash = ?", "users"
        if email:
            conn.execute("DELETE FROM login_lockouts WHERE account = ?", (_acct(email),))
            # the same person's other login (same email AND same password until now) moves with it
            for sib in conn.execute(sibling_sql, (email, old_hash)).fetchall():
                extra = ", password_plain = NULL"
                conn.execute(f"UPDATE {sibling_table} SET password_hash = ?{extra} WHERE id = ?", (new_hash, sib["id"]))
                if sibling_table == "users":
                    conn.execute("UPDATE cfis SET password_hash = ? WHERE user_id = ?", (new_hash, sib["id"]))
                    conn.execute("UPDATE students SET password_hash = ? WHERE user_id = ?", (new_hash, sib["id"]))
        conn.execute("UPDATE password_resets SET used_at = ? WHERE kind = ? AND account_id = ? AND used_at IS NULL",
                     (_iso(_utc()), link["kind"], link["account_id"]))
        conn.commit()
        conn.close()
        flash("Your password is changed. Log in with the new one.", "success")
        return redirect(url_for("home_launcher"))
    conn.close()
    return render_template("reset_password.html", expired=False, who=acct["name"])
