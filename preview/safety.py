"""Make a copy of OpsHub unable to touch the outside world.

Called by run_preview.py before the app handles its first request. Reads from
the internet that only fill in display data (weather, NOTAMs, aircraft
positions) are allowed; anything that sends, charges, prints or restarts is
blocked and recorded in OUTBOX so the preview can show what WOULD have happened.
"""
import smtplib
import subprocess
import urllib.request

OUTBOX = []  # (kind, detail) of everything that was blocked

# Hosts whose traffic is always blocked, even for a plain GET.
_BLOCK_HOSTS = ("waveapps.com", "twilio.com", "fcm.googleapis.com", "push.apple.com",
                "push.services.mozilla.com", "notify.windows.com", "stripe.com", "api.pushover.net")


class Blocked(Exception):
    pass


def _record(kind, detail, limit=300):
    OUTBOX.append((kind, str(detail)[:limit]))
    del OUTBOX[:-200]


def neutralize():
    real_urlopen = urllib.request.urlopen

    def safe_urlopen(req, *a, **k):
        url = getattr(req, "full_url", req)
        has_body = getattr(req, "data", None) is not None or k.get("data") is not None or (a and a[0] is not None)
        method = (getattr(req, "get_method", lambda: "GET")() or "GET").upper()
        host = str(url).split("//", 1)[-1].split("/", 1)[0].lower()
        if has_body or method != "GET" or any(h in host for h in _BLOCK_HOSTS):
            _record("network blocked", f"{method} {url}")
            raise Blocked("blocked in preview: " + str(url))
        return real_urlopen(req, *a, **k)

    urllib.request.urlopen = safe_urlopen

    class _NoSMTP:
        def __init__(self, *a, **k):
            _record("email blocked", a)
            raise Blocked("email is switched off in preview")

    smtplib.SMTP = _NoSMTP
    smtplib.SMTP_SSL = _NoSMTP

    try:   # keep the whole message (so a "forgot password" link can be opened from the Blocked sends page)
        import notify

        def blocked_email(settings, to_addr, subject, body, brand=getattr(notify, "DEFAULT_BRAND", "")):
            _record("email blocked", f"To {to_addr} | {subject} | {body}", limit=1500)
            return False, "email is switched off in preview"
        notify.send_email = blocked_email
    except Exception:
        pass

    def _empty(args, kwargs):
        text = bool(kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding"))
        return "" if text else b""

    def fake_run(args, *a, **k):
        _record("command blocked", args)
        out = _empty(args, k)
        return subprocess.CompletedProcess(args, 0, out, out)

    class FakePopen:
        def __init__(self, args, *a, **k):
            _record("command blocked", args)
            self.returncode = 0
            self._out = _empty(args, k)

        def communicate(self, *a, **k):
            return (self._out, self._out)

        def wait(self, *a, **k):
            return 0

    subprocess.run = fake_run
    subprocess.Popen = FakePopen
    subprocess.call = lambda args, *a, **k: (_record("command blocked", args) or 0)
    subprocess.check_call = subprocess.call
    subprocess.check_output = lambda args, *a, **k: (_record("command blocked", args) or _empty(args, k))

    try:
        import label_printer
        for name in dir(label_printer):
            if name.startswith("print_"):
                setattr(label_printer, name, lambda *a, **k: _record("label printer blocked", a))
    except Exception:
        pass
