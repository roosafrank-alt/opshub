"""Email/text reminders: low parts stock, maintenance due, and upcoming
scheduled flights.

Run daily via cron (see README/runbook) as:
    PYTHONPATH=/home/frank/shopinv/vendor python3 /home/frank/shopinv/notify.py

Delivery settings (SMTP for email, Twilio for text) are stored in the
app_settings table and edited from the admin Notification Settings page in
the app - nothing here is hardcoded. If a channel isn't configured yet,
sends for that channel are silently skipped (not an error) so the daily
check can still run before both channels are set up.

Only the standard library is used (smtplib, urllib) so no extra packages
need to be vendored onto the Pi.
"""

import base64
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText

import db


# ---------------------------------------------------------------------------
# Settings (app_settings key/value table)
# ---------------------------------------------------------------------------

SETTINGS_KEYS = [
    "smtp_host", "smtp_port", "smtp_username", "smtp_password", "smtp_from",
    "twilio_account_sid", "twilio_auth_token", "twilio_from_number",
]


def get_settings(conn):
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    return {k: settings.get(k) or "" for k in SETTINGS_KEYS}


def save_settings(conn, values):
    """values: dict of key -> new value (only SETTINGS_KEYS are saved)."""
    for key in SETTINGS_KEYS:
        if key not in values:
            continue
        val = values[key].strip() if values[key] else ""
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, val),
        )
    conn.commit()


def email_configured(settings):
    return bool(settings["smtp_host"] and settings["smtp_username"] and settings["smtp_password"])


def sms_configured(settings):
    return bool(settings["twilio_account_sid"] and settings["twilio_auth_token"] and settings["twilio_from_number"])


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

# Reminders cover both programs, and each should look like it came from the
# brand the recipient actually knows - the shop side is Winds Aloft, the
# flight school side is Fly with Kate!. Callers pass which one applies;
# defaults to Fly with Kate! for anything that doesn't specify (e.g. the
# admin's own test-send button, which isn't tied to either category).
DEFAULT_BRAND = "Fly with Kate!"


def _automated_footer(brand):
    return (f"\n\n---\nThis is an automated message from {brand} - please don't reply to it. "
            "Questions? Contact us directly.")


def send_email(settings, to_addr, subject, body, brand=DEFAULT_BRAND):
    """Returns (ok: bool, error: str|None)."""
    if not email_configured(settings) or not to_addr:
        return False, "not configured"
    try:
        port = int(settings["smtp_port"] or 587)
    except ValueError:
        port = 587
    from_addr = settings["smtp_from"] or settings["smtp_username"]
    body = body + _automated_footer(brand)
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        # A display name (not just the bare address) makes it obvious at a
        # glance, before even opening the email, that this isn't a person.
        msg["From"] = f"{brand} <{from_addr}>"
        msg["To"] = to_addr
        with smtplib.SMTP(settings["smtp_host"], port, timeout=20) as server:
            server.starttls()
            server.login(settings["smtp_username"], settings["smtp_password"])
            server.sendmail(from_addr, [to_addr], msg.as_string())
        return True, None
    except Exception as e:
        return False, str(e)


def send_sms(settings, to_number, body):
    """Returns (ok: bool, error: str|None)."""
    if not sms_configured(settings) or not to_number:
        return False, "not configured"
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{settings['twilio_account_sid']}/Messages.json"
        data = urllib.parse.urlencode({
            "From": settings["twilio_from_number"],
            "To": to_number,
            "Body": body,
        }).encode()
        req = urllib.request.Request(url, data=data)
        auth = base64.b64encode(f"{settings['twilio_account_sid']}:{settings['twilio_auth_token']}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status not in (200, 201):
                return False, f"Twilio returned HTTP {resp.status}"
        return True, None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        return False, f"Twilio error {e.code}: {detail}"
    except Exception as e:
        return False, str(e)


def notify_user(settings, user, subject, body, brand=DEFAULT_BRAND):
    """Sends to whichever channels this user opted into and has contact info
    for. Returns True if at least one send succeeded."""
    sent_any = False
    if user["notify_email"] and user["email"]:
        ok, _err = send_email(settings, user["email"], subject, body, brand=brand)
        sent_any = sent_any or ok
    if user["notify_sms"] and user["phone"]:
        ok, _err = send_sms(settings, user["phone"], body)
        sent_any = sent_any or ok
    return sent_any


# ---------------------------------------------------------------------------
# Dedup log
# ---------------------------------------------------------------------------

def already_notified_today(conn, category, ref_id, ref_key=""):
    row = conn.execute(
        "SELECT 1 FROM notification_log WHERE category=? AND ref_id=? AND ref_key=? "
        "AND date(sent_at)=date('now') LIMIT 1",
        (category, ref_id, ref_key),
    ).fetchone()
    return row is not None


def log_notification(conn, category, ref_id, ref_key=""):
    conn.execute(
        "INSERT INTO notification_log (category, ref_id, ref_key) VALUES (?, ?, ?)",
        (category, ref_id, ref_key),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Daily check - one function per category, plus a runner that does all three
# ---------------------------------------------------------------------------

def check_low_stock(conn, settings):
    recipients = conn.execute(
        "SELECT * FROM users WHERE active=1 AND notify_low_stock=1 AND (notify_email=1 OR notify_sms=1)"
    ).fetchall()
    if not recipients:
        return 0
    sent_count = 0
    low_parts = conn.execute(
        "SELECT * FROM parts WHERE qty_on_hand <= reorder_point AND notify_low_stock = 1 ORDER BY name"
    ).fetchall()
    for p in low_parts:
        if already_notified_today(conn, "low_stock", p["id"]):
            continue
        subject = f"Low stock: {p['name']}"
        body = (f"{p['name']} is at {p['qty_on_hand']:g} {p['unit'] or ''} "
                f"(reorder point {p['reorder_point']:g}). Barcode: {p['barcode']}")
        any_sent = False
        for u in recipients:
            if notify_user(settings, u, subject, body, brand="Winds Aloft"):
                any_sent = True
        if any_sent:
            log_notification(conn, "low_stock", p["id"])
            sent_count += 1
    return sent_count


def check_maintenance(conn, settings):
    recipients = conn.execute(
        "SELECT * FROM users WHERE active=1 AND notify_maintenance=1 AND (notify_email=1 OR notify_sms=1)"
    ).fetchall()
    if not recipients:
        return 0
    sent_count = 0
    maint_rows = conn.execute("""
        SELECT mi.*, a.tag as asset_tag, a.name as asset_name,
               a.hobbs_hours as asset_hobbs_hours, a.tach_hours as asset_tach_hours
        FROM maintenance_items mi
        JOIN assets a ON a.id = mi.asset_id
        WHERE mi.active = 1 AND a.deleted_at IS NULL
    """).fetchall()
    for m in maint_rows:
        current = m["asset_tach_hours"] if m["hour_type"] != "hobbs" else m["asset_hobbs_hours"]
        status = db.maintenance_status(m, current)
        if status["urgency"] not in ("overdue", "due_soon"):
            continue
        ref_key = status["urgency"]
        if already_notified_today(conn, "maintenance", m["id"], ref_key):
            continue
        subject = f"Maintenance {status['urgency'].replace('_', ' ')}: {m['name']} ({m['asset_tag']})"
        body = f"{m['asset_tag']} - {m['name']}: {status['label']}"
        any_sent = False
        for u in recipients:
            if notify_user(settings, u, subject, body, brand="Winds Aloft"):
                any_sent = True
        if any_sent:
            log_notification(conn, "maintenance", m["id"], ref_key)
            sent_count += 1
    return sent_count


def check_flight_reminders(conn, settings):
    tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    sched = conn.execute("""
        SELECT sf.*, a.tag as asset_tag, s.user_id as student_user_id, c.user_id as cfi_user_id
        FROM scheduled_flights sf
        JOIN assets a ON a.id = sf.asset_id
        JOIN students s ON s.id = sf.student_id
        LEFT JOIN cfis c ON c.id = sf.cfi_id
        WHERE sf.status = 'scheduled' AND sf.scheduled_date = ?
    """, (tomorrow,)).fetchall()
    sent_count = 0
    for f in sched:
        if already_notified_today(conn, "flight_reminder", f["id"]):
            continue
        time_str = f" at {f['scheduled_time']}" if f["scheduled_time"] else ""
        from app import usdate  # late import: app imports this module (FLY-12 date format)
        subject = f"Flight reminder: {usdate(tomorrow)}"
        body = f"You have a flight scheduled tomorrow ({usdate(tomorrow)}){time_str} in {f['asset_tag']}."
        any_sent = False
        for uid in (f["student_user_id"], f["cfi_user_id"]):
            if not uid:
                continue
            u = conn.execute(
                "SELECT * FROM users WHERE id=? AND active=1 AND notify_flight_reminders=1", (uid,)
            ).fetchone()
            if u and notify_user(settings, u, subject, body, brand="Fly with Kate!"):
                any_sent = True
        if any_sent:
            log_notification(conn, "flight_reminder", f["id"])
            sent_count += 1
    return sent_count


def check_cores_due(conn, settings):
    """Exchange cores owed back to a supplier (see orders.is_exchange): tells
    shop admins once when one is due within 7 days, and once more if it goes
    overdue, until it's marked shipped or credited."""
    recipients = conn.execute(
        "SELECT * FROM users WHERE active=1 AND (is_master_admin=1 OR shop_role='admin') "
        "AND (notify_email=1 OR notify_sms=1)").fetchall()
    if not recipients:
        return 0
    rows = conn.execute("""SELECT o.*, p.name as part_name FROM orders o LEFT JOIN parts p ON p.id = o.part_id
                           WHERE o.is_exchange = 1 AND o.status = 'received' AND o.core_due_date IS NOT NULL
                             AND o.core_shipped_at IS NULL AND o.core_credited_at IS NULL
                             AND o.core_due_date <= date('now', '+7 days')""").fetchall()
    sent = 0
    for o in rows:
        overdue = o["core_due_date"] < datetime.now().strftime("%Y-%m-%d")
        key = "overdue" if overdue else "7day"
        if conn.execute("SELECT 1 FROM notification_log WHERE category='core_due' AND ref_id=? AND ref_key=? LIMIT 1",
                        (o["id"], key)).fetchone():
            continue
        item = o["part_name"] or o["description"]
        from app import usdate  # late import: app imports this module (FLY-12 date format)
        charge = f" or be billed a ${o['core_charge']:,.2f} core charge" if o["core_charge"] else ""
        subject = f"Core {'OVERDUE' if overdue else 'due soon'}: {item}"
        body = (f"The core for {item} (order #{o['id']}, {o['supplier'] or 'supplier'}) "
                f"{'was due' if overdue else 'is due'} back {usdate(o['core_due_date'])}. Ship it{charge}, "
                f"then press Core shipped on the Orders page.")
        if any(notify_user(settings, u, subject, body, brand="Winds Aloft") for u in recipients):
            log_notification(conn, "core_due", o["id"], key)
            sent += 1
    return sent


def run_daily_checks():
    conn = db.get_db()
    settings = get_settings(conn)
    results = {
        "cores_due": check_cores_due(conn, settings),
        "low_stock": check_low_stock(conn, settings),
        "maintenance": check_maintenance(conn, settings),
        "flight_reminder": check_flight_reminders(conn, settings),
    }
    conn.close()
    return results


if __name__ == "__main__":
    counts = run_daily_checks()
    print(f"Reminders sent - low stock: {counts['low_stock']}, "
          f"maintenance: {counts['maintenance']}, flight: {counts['flight_reminder']}")
