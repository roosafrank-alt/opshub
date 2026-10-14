"""Keeps the Flight School dashboard's weather and NOTAM strips ready.

Before: when the saved copy was older than 20 minutes (weather) or 30
(NOTAMs), whoever opened the Flight School dashboard waited while the app
asked half a dozen outside services one after another (each allowed up to
8-10 seconds). On a bad day that was long enough for the phone to give up,
which looked like the Fly with Kate! tile "not opening".

Now the dashboard only ever READS the saved copy (instant). A background
thread refreshes it, and the app's once-a-minute loop (app.py) keeps it
fresh. If the saved copy is missing or more than 2 hours old the strip is
simply left out until the refresh lands - never old weather shown as current.
The saved copy lives in the same weather_cache table as before.
"""
import json
import logging
import threading
import time
from datetime import datetime

import db
import notams
import weather

log = logging.getLogger("opshub.dashboard_cache")

WEATHER_REFRESH_AFTER = 15 * 60   # refresh a bit before weather's own 20-minute freshness
NOTAM_REFRESH_AFTER = 25 * 60     # ...and before NOTAMs' 30 minutes
MAX_STALE_SECONDS = 2 * 3600      # older than this is hidden, not shown
MIN_GAP_SECONDS = 60              # never start refreshes closer together than this

BACKGROUND = True                 # tests turn this off so no thread runs
_lock = threading.Lock()
_last_start = [0.0]


def _read(conn, key):
    """(payload, age_in_seconds) of the saved copy, or (None, None)."""
    try:
        row = conn.execute("SELECT payload, fetched_at FROM weather_cache WHERE cache_key = ?", (key,)).fetchone()
        if not row:
            return None, None
        age = (datetime.now() - datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        return json.loads(row["payload"]), age
    except Exception:
        return None, None


def _weather_key():
    return f"dashboard_wx_{weather.ZIP_CODE}"


def _refresh(do_weather, do_notams):
    conn = db.get_db()
    try:
        if do_weather:
            try:
                weather.get_dashboard_weather(conn, force=True)
            except Exception:
                log.exception("Weather refresh failed")
        if do_notams:
            try:
                notams.get_dashboard_notams(conn, force=True)
            except Exception:
                log.exception("NOTAM refresh failed")
    finally:
        conn.close()


def _start_refresh(do_weather=True, do_notams=True):
    """Refreshes in a background thread; does nothing if one is already
    running or one started under a minute ago."""
    if not BACKGROUND:
        return False
    now = time.time()
    if now - _last_start[0] < MIN_GAP_SECONDS or not _lock.acquire(blocking=False):
        return False
    _last_start[0] = now

    def run():
        try:
            _refresh(do_weather, do_notams)
        finally:
            _lock.release()

    threading.Thread(target=run, daemon=True).start()
    return True


def _usable(payload, age):
    return payload if payload is not None and age is not None and 0 <= age <= MAX_STALE_SECONDS else None


def cached_weather(conn):
    """The saved weather payload for the dashboard, never fetching inline."""
    payload, age = _read(conn, _weather_key())
    if payload is None or age is None or age > WEATHER_REFRESH_AFTER:
        _start_refresh(do_weather=True, do_notams=False)
    return _usable(payload, age)


def cached_notams(conn):
    """The saved NOTAM payload for the dashboard, never fetching inline."""
    payload, age = _read(conn, notams.CACHE_KEY)
    if payload is None or age is None or age > NOTAM_REFRESH_AFTER:
        _start_refresh(do_weather=False, do_notams=True)
    return _usable(payload, age)


def refresh_if_stale():
    """Called from the app's once-a-minute loop: starts a refresh for
    whichever saved copy is getting old, so the dashboard never has to."""
    conn = db.get_db()
    try:
        w, w_age = _read(conn, _weather_key())
        n, n_age = _read(conn, notams.CACHE_KEY)
    finally:
        conn.close()
    want_w = w is None or w_age is None or w_age > WEATHER_REFRESH_AFTER
    want_n = n is None or n_age is None or n_age > NOTAM_REFRESH_AFTER
    if want_w or want_n:
        _start_refresh(want_w, want_n)
