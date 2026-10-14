"""Records which pages are slow, so slowness can be measured instead of guessed.

Every request slower than SLOW_MS (server time only - it does NOT include the
phone's network time) adds one line to instance/slow_requests.log:
    time <TAB> milliseconds <TAB> status <TAB> method <TAB> page
"page" is the route pattern (e.g. /parts/<int:part_id>), so no ids, names or
query strings are ever written. Fast requests are not recorded at all.
The log keeps itself small. Admin > System shows a summary.
"""
import os
import threading
import time
from datetime import datetime, timedelta

SLOW_MS = 800
MAX_BYTES = 400 * 1024      # when the file passes this, keep only the newest lines
KEEP_LINES = 1500

_lock = threading.Lock()
_writes = [0]
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "slow_requests.log")


def _trim():
    try:
        if os.path.getsize(LOG_PATH) <= MAX_BYTES:
            return
        with open(LOG_PATH, "r", errors="replace") as f:
            lines = f.readlines()[-KEEP_LINES:]
        with open(LOG_PATH, "w") as f:
            f.writelines(lines)
    except OSError:
        pass


def record(rule, method, status, ms, when=None):
    """Adds one line if the request was slow. Never raises."""
    if ms < SLOW_MS:
        return False
    try:
        stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        page = (rule or "?").replace("\t", " ").replace("\n", " ")[:120]
        with _lock:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            with open(LOG_PATH, "a") as f:
                f.write(f"{stamp}\t{int(ms)}\t{status}\t{method}\t{page}\n")
            _writes[0] += 1
            if _writes[0] % 50 == 0:
                _trim()
        return True
    except OSError:
        return False


def init_app(app):
    from flask import request

    @app.before_request
    def _timing_start():
        request.environ["opshub_t0"] = time.perf_counter()

    @app.after_request
    def _timing_end(resp):
        try:
            t0 = request.environ.get("opshub_t0")
            if t0 is not None and request.endpoint != "static":
                rule = request.url_rule.rule if request.url_rule else request.path
                record(rule, request.method, resp.status_code, (time.perf_counter() - t0) * 1000)
        except Exception:
            pass
        return resp


def summary(days=7, path=None, now=None):
    """{'total': n, 'pages': [...worst first...], 'recent': [...newest first...]}
    for slow requests in the last `days` days."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=days)
    rows = []
    try:
        with open(path or LOG_PATH, "r", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 5:
                    continue
                try:
                    when = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
                    ms = int(parts[1])
                except ValueError:
                    continue
                if when >= cutoff:
                    rows.append(dict(when=parts[0], ms=ms, status=parts[2], method=parts[3], page=parts[4]))
    except OSError:
        pass
    by_page = {}
    for r in rows:
        key = (r["method"], r["page"])
        p = by_page.setdefault(key, dict(method=r["method"], page=r["page"], count=0, total=0, worst=0, last=""))
        p["count"] += 1
        p["total"] += r["ms"]
        p["worst"] = max(p["worst"], r["ms"])
        p["last"] = max(p["last"], r["when"])
    pages = sorted(by_page.values(), key=lambda p: (-p["worst"], -p["count"]))
    for p in pages:
        p["avg"] = int(p["total"] / p["count"])
    return dict(total=len(rows), pages=pages[:10], recent=sorted(rows, key=lambda r: r["when"], reverse=True)[:10],
                slow_ms=SLOW_MS, days=days)
