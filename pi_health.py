"""Watches the Raspberry Pi's temperature and throttle state and alerts on
crossings - overheating or throttling that could cause slowdowns or data
corruption.

Run every 5 minutes via cron (see README/runbook) as:
    python3 /home/frank/shopinv/pi_health.py

Deliberately its own process, separate from the Flask app, so it keeps
alerting even if OpsHub itself is stuck, crashed, or mid-restart. Only the
standard library is used (subprocess, urllib) - same as notify.py/push.py -
so no extra packages need to be vendored onto the Pi.

Alerts once per crossing, not every 5 minutes while still hot: an alert
fires when the temperature exceeds ALERT_TEMP_C or vcgencmd reports active
throttling, and stays open (no repeat notification) until the Pi recovers
(drops below RECOVER_TEMP_C with no throttle bits set), at which point the
next crossing can alert again. Every alert is also written to the
system_alerts table so the OpsHub dashboard can show it to shop admins
(see dashboard() / system_alert_acknowledge() in app.py).
"""

import re
import subprocess
import urllib.error
import urllib.request

import db

NTFY_URL = "https://ntfy.sh/opshub-pi-health-k7x9m2qp4w"

ALERT_TEMP_C = 75.0
RECOVER_TEMP_C = 70.0

# vcgencmd get_throttled bitmask (Raspberry Pi firmware docs):
THROTTLE_BIT_CURRENTLY_THROTTLED = 0x4  # bit 2
THROTTLE_BIT_SOFT_TEMP_LIMIT = 0x8      # bit 3


def _run(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def read_temp_c():
    """Current SoC temperature in Celsius, or None if vcgencmd isn't
    available (e.g. running off the Pi, during development)."""
    m = re.search(r"temp=([\d.]+)", _run(["vcgencmd", "measure_temp"]))
    return float(m.group(1)) if m else None


def read_throttled():
    """Raw get_throttled bitmask as an int (0 if unavailable)."""
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", _run(["vcgencmd", "get_throttled"]))
    return int(m.group(1), 16) if m else 0


def notify(message):
    """Best-effort phone push via ntfy.sh - no account/API key needed. The
    system_alerts row is the record of truth if this fails."""
    req = urllib.request.Request(NTFY_URL, data=message.encode("utf-8"), method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
    except (urllib.error.URLError, OSError):
        pass


def main():
    temp = read_temp_c()
    throttled = read_throttled()
    currently_throttled = bool(throttled & THROTTLE_BIT_CURRENTLY_THROTTLED)
    soft_temp_limit = bool(throttled & THROTTLE_BIT_SOFT_TEMP_LIMIT)

    alerting = (temp is not None and temp > ALERT_TEMP_C) or currently_throttled or soft_temp_limit
    recovered = (temp is None or temp < RECOVER_TEMP_C) and not currently_throttled and not soft_temp_limit

    conn = db.get_db()
    open_alert = conn.execute(
        "SELECT id FROM system_alerts WHERE resolved_at IS NULL ORDER BY created_at DESC LIMIT 1"
    ).fetchone()

    if alerting and not open_alert:
        reasons = []
        if temp is not None and temp > ALERT_TEMP_C:
            reasons.append(f"temp {temp:.0f}°C")
        if currently_throttled:
            reasons.append("throttling active")
        if soft_temp_limit:
            reasons.append("soft temp limit active")
        message = "OpsHub Pi: " + ", ".join(reasons)
        level = "critical" if currently_throttled else "warning"
        conn.execute(
            "INSERT INTO system_alerts (message, level, created_at) VALUES (?, ?, datetime('now'))",
            (message, level),
        )
        conn.commit()
        notify(message)
    elif recovered and open_alert:
        conn.execute("UPDATE system_alerts SET resolved_at = datetime('now') WHERE resolved_at IS NULL")
        conn.commit()

    conn.close()


if __name__ == "__main__":
    main()
