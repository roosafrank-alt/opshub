"""Shared test harness for OpsHub.

Every test runs the real Flask app against a THROWAWAY SQLite database in a
temp folder - never instance/shopinv.db - and with every outbound side
effect (internet calls, email, SMS, push, label printer, reboot/restart)
replaced by a harmless fake. Safe to run on the Mac, the Pi, or in a
Claude cloud session, even while the live app is running.

Run everything:
    python3 -m unittest discover -s tests -v

Only uses the Python standard library + Flask (already installed for the
app), so there's nothing extra to pip install.
"""
import os
import sys
import shutil
import tempfile
import unittest
import urllib.request
import smtplib
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# ---------------------------------------------------------------------------
# Point the app at a temp database BEFORE app.py is imported anywhere.
# db.get_db() reads db.DB_PATH on every call, so swapping the module global
# is enough to redirect every connection in every blueprint.
# ---------------------------------------------------------------------------
import db  # noqa: E402

_TMP_ROOT = tempfile.mkdtemp(prefix="opshub-tests-")
db.DB_PATH = os.path.join(_TMP_ROOT, "template.db")


# ---------------------------------------------------------------------------
# Kill every outbound side effect. Anything that tries to reach the network,
# send mail, or run a shell command (reboot/restart/lp) records the attempt
# instead so a test can assert on it.
# ---------------------------------------------------------------------------
SIDE_EFFECTS = []


class _Blocked(Exception):
    pass


def _fake_urlopen(req, *a, **k):
    url = getattr(req, "full_url", req)
    SIDE_EFFECTS.append(("urlopen", url))
    raise _Blocked("network disabled in tests: %s" % url)


class _FakeSMTP:
    def __init__(self, *a, **k):
        SIDE_EFFECTS.append(("smtp", a))
        raise _Blocked("smtp disabled in tests")


def _fake_run(args, *a, **k):
    SIDE_EFFECTS.append(("subprocess", args))
    return subprocess.CompletedProcess(args, 0, b"", b"")


class _FakePopen:
    def __init__(self, args, *a, **k):
        SIDE_EFFECTS.append(("subprocess", args))
        self.returncode = 0

    def communicate(self, *a, **k):
        return (b"", b"")

    def wait(self, *a, **k):
        return 0


urllib.request.urlopen = _fake_urlopen
smtplib.SMTP = _FakeSMTP
smtplib.SMTP_SSL = _FakeSMTP
subprocess.run = _fake_run
subprocess.Popen = _FakePopen
subprocess.call = lambda args, *a, **k: (SIDE_EFFECTS.append(("subprocess", args)) or 0)
subprocess.check_call = subprocess.call
subprocess.check_output = lambda args, *a, **k: (SIDE_EFFECTS.append(("subprocess", args)) or b"")

import app as app_module  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import gc  # noqa: E402
from flask.testing import FlaskClient  # noqa: E402

flask_app = app_module.app
flask_app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

# When a route crashes mid-save, its half-finished SQLite connection can keep
# the database write-locked until Python's garbage collector happens to run.
# Collecting after every request stops one crash from stalling every later
# test for 30s. test_db_not_left_locked_after_a_crash turns this off to test
# the app's own cleanup.
GC_AFTER_REQUEST = [True]


class _Client(FlaskClient):
    def open(self, *a, **k):
        try:
            return super().open(*a, **k)
        finally:
            if GC_AFTER_REQUEST[0]:
                gc.collect()


flask_app.test_client_class = _Client


def open_finding(finding_id):
    """Marks a test for a bug that's been REPORTED but not fixed yet. It
    describes the correct behavior, so it fails today, and that's expected.
    finding_id matches the QA findings card on the Idea Queue page. When the
    fix is approved and made, delete this decorator in the same change. A test
    still carrying it after the fix shows up as "unexpected success"."""
    import unittest as _u

    def deco(fn):
        fn.qa_finding = finding_id
        return _u.expectedFailure(fn)
    return deco

# Label printer: never touch real USB hardware.
try:
    import label_printer
    for _name in dir(label_printer):
        if _name.startswith("print_"):
            setattr(label_printer, _name, lambda *a, **k: SIDE_EFFECTS.append(("label", a)))
except Exception:  # pragma: no cover
    pass

# Build the schema + migrations once, then copy that file for each test -
# much faster than re-running init_db()'s migrations every time.
app_module.init_db()
_TEMPLATE_DB = db.DB_PATH

PASSWORD = "test-pass-123"
_FAST_HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")

# Every kind of account the app knows about. The key is what tests use with
# self.login(...). Keep this list in sync if a new role is ever added.
ROLES = {
    "master":        dict(is_master_admin=1, shop_role="admin", flight_role="cfi", can_bill=1, academy_access=1),
    "shop_admin":    dict(shop_role="admin"),
    "tech":          dict(shop_role="tech"),
    "inspector":     dict(shop_role="inspector"),
    "shop_student":  dict(shop_role="student"),
    "cfi":           dict(flight_role="cfi"),
    "cfi_billing":   dict(flight_role="cfi", can_bill=1),
    "flight_student": dict(flight_role="student"),
    "no_roles":      dict(),
}


class OpsHubTestCase(unittest.TestCase):
    """Base class: fresh database per test, one user per role, helpers for
    logging in and creating common records."""

    def setUp(self):
        SIDE_EFFECTS.clear()
        GC_AFTER_REQUEST[0] = True
        self._dir = tempfile.mkdtemp(dir=_TMP_ROOT)
        db.DB_PATH = os.path.join(self._dir, "shopinv.db")
        shutil.copy(_TEMPLATE_DB, db.DB_PATH)
        self.app = flask_app
        self.client = flask_app.test_client()
        self.users = {}
        conn = db.get_db()
        for key, attrs in ROLES.items():
            cols = dict(name=key.replace("_", " ").title(), username=key, password_hash=_FAST_HASH,
                        active=1, created_at=db.now_iso())
            cols.update(attrs)
            names = ",".join(cols)
            marks = ",".join("?" * len(cols))
            cur = conn.execute(f"INSERT INTO users ({names}) VALUES ({marks})", list(cols.values()))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
            db.ensure_flight_profile(conn, row)
            self.users[key] = row
        conn.execute("INSERT INTO customers (name, email, password_hash, active) VALUES (?,?,?,1)",
                     ("Owner Customer", "owner@example.com", _FAST_HASH))
        self.customer_id = conn.execute("SELECT id FROM customers WHERE email='owner@example.com'").fetchone()["id"]
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    # ----- auth -----------------------------------------------------------
    def login(self, role):
        """Log in as one of ROLES (or 'customer'), the same way the real
        login form does, without paying for a password hash each time."""
        self.client = flask_app.test_client()
        if role is None:
            return self.client
        if role == "customer":
            with self.client.session_transaction() as s:
                s["customer_id"] = self.customer_id
                s["customer_name"] = "Owner Customer"
            return self.client
        u = self.users[role]
        conn = db.get_db()
        cfi = conn.execute("SELECT id FROM cfis WHERE user_id = ?", (u["id"],)).fetchone()
        stu = conn.execute("SELECT id FROM students WHERE user_id = ?", (u["id"],)).fetchone()
        conn.close()
        with self.client.session_transaction() as s:
            s["user_id"] = u["id"]
            s["user_name"] = u["name"]
            s["is_master_admin"] = bool(u["is_master_admin"])
            s["shop_role"] = u["shop_role"]
            s["flight_role"] = u["flight_role"]
            s["can_bill"] = bool(u["can_bill"])
            s["academy_access"] = bool(u["academy_access"])
            s["tour_seen_shop"] = True
            s["tour_seen_flight"] = True
            if u["flight_role"] == "cfi" and cfi:
                s["cfi_id"] = cfi["id"]
            if u["flight_role"] == "student" and stu:
                s["student_id"] = stu["id"]
        return self.client

    # ----- data helpers ---------------------------------------------------
    def q(self, sql, params=()):
        conn = db.get_db()
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows

    def q1(self, sql, params=()):
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def exec(self, sql, params=()):
        conn = db.get_db()
        cur = conn.execute(sql, params)
        conn.commit()
        rid = cur.lastrowid
        conn.close()
        return rid

    def make_part(self, name="Oil Filter", barcode="PART-001", qty=10, unit_cost=12.5, reorder=2, unit="ea"):
        return self.exec(
            "INSERT INTO parts (barcode, name, category, location, unit, qty_on_hand, reorder_point, unit_cost, "
            "sell_price, supplier, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (barcode, name, "Engine", "Shelf A", unit, qty, reorder, unit_cost, unit_cost * 1.3, "Aircraft Spruce",
             db.now_iso(), db.now_iso()))

    def make_asset(self, tag="N12345"):
        return self.exec("INSERT INTO assets (tag, name, created_at, updated_at) VALUES (?,?,?,?)",
                         (tag, "Cessna 172", db.now_iso(), db.now_iso()))

    def make_project(self, name="Annual - N12345", status="active", asset_id=None):
        conn = db.get_db()
        code = db.gen_project_code(conn, db.now_iso()[2:4])
        cur = conn.execute("INSERT INTO projects (name, code, status, asset_id, created_at) VALUES (?,?,?,?,?)",
                           (name, code, status, asset_id, db.now_iso()))
        conn.commit()
        pid = cur.lastrowid
        conn.close()
        return pid

    def qty(self, part_id):
        return self.q1("SELECT qty_on_hand FROM parts WHERE id = ?", (part_id,))["qty_on_hand"]

    def scan(self, barcode, action, qty=1, project_id=None, performed_by="Frank", **extra):
        body = dict(barcode=barcode, action=action, qty=qty, project_id=project_id, performed_by=performed_by)
        body.update(extra)
        return self.client.post("/api/scan", json=body)


# ---------------------------------------------------------------------------
# Generic seeding: insert a row into ANY table, auto-filling required
# columns with plausible values, so tests don't break every time a column
# is added.
# ---------------------------------------------------------------------------
def _filler(col_name, col_type):
    n = col_name.lower()
    t = (col_type or "").upper()
    if n.endswith("_at") or n.endswith("date") or n in ("date",):
        return db.now_iso()[:10] if "date" in n else db.now_iso()
    if "INT" in t:
        return 1
    if "REAL" in t or "NUM" in t:
        return 1.0
    return "test-" + n


def seed_row(conn, table, **values):
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    row = {}
    for c in cols:
        if c["pk"]:
            continue
        if c["name"] in values:
            row[c["name"]] = values[c["name"]]
        elif c["notnull"] and c["dflt_value"] is None:
            row[c["name"]] = _filler(c["name"], c["type"])
    for k, v in values.items():
        row.setdefault(k, v)
    names = ",".join(row)
    marks = ",".join("?" * len(row))
    cur = conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(row.values()))
    return cur.lastrowid


def seed_everything(tc):
    """One of (nearly) everything, all with id 1 where possible, so every
    /<int:x_id>/ page in the crawler has a real record to render."""
    ids = {}
    ids["asset"] = tc.make_asset("N12345")
    ids["part"] = tc.make_part()
    ids["project"] = tc.make_project(asset_id=ids["asset"])
    conn = db.get_db()
    stu = conn.execute("SELECT id FROM students WHERE user_id = ?", (tc.users["flight_student"]["id"],)).fetchone()["id"]
    cfi = conn.execute("SELECT id FROM cfis WHERE user_id = ?", (tc.users["cfi"]["id"],)).fetchone()["id"]
    ids["student"], ids["cfi"] = stu, cfi
    conn.execute("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (tc.customer_id, ids["asset"]))
    ids["section"] = seed_row(conn, "project_sections", project_id=ids["project"], name="Brakes", created_at=db.now_iso())
    ids["order"] = seed_row(conn, "orders", description="Spark plugs", part_id=ids["part"], qty_ordered=4,
                            unit_cost=20, status="ordered")
    ids["laborer"] = seed_row(conn, "laborers", name="Joe Tech", code="L-0001")
    ids["maint"] = seed_row(conn, "maintenance_items", asset_id=ids["asset"], name="Annual inspection")
    ids["squawk"] = seed_row(conn, "plane_squawks", asset_id=ids["asset"], notes="Left brake soft")
    ids["sched"] = seed_row(conn, "scheduled_flights", asset_id=ids["asset"], student_id=stu, cfi_id=cfi,
                            scheduled_date=db.now_iso()[:10])
    ids["flight"] = seed_row(conn, "flights", asset_id=ids["asset"], student_id=stu, cfi_id=cfi,
                             flight_date=db.now_iso()[:10])
    ids["logbook"] = seed_row(conn, "logbook_entries", asset_id=ids["asset"], project_id=ids["project"],
                              entry_date=db.now_iso()[:10], body="Performed annual inspection.")
    ids["pilotlog"] = seed_row(conn, "pilot_logbook", student_id=stu, asset_id=ids["asset"], entry_date=db.now_iso()[:10])
    conn.execute("INSERT INTO transactions (part_id, project_id, type, qty, note, performed_by, section, source, created_at) "
                 "VALUES (?, ?, 'out', 1, 'seed', 'Frank', 'Brakes', 'assigned', ?)",
                 (ids["part"], ids["project"], db.now_iso()))
    conn.execute("UPDATE parts SET qty_on_hand = qty_on_hand - 1 WHERE id = ?", (ids["part"],))
    conn.commit()
    conn.close()
    return ids
