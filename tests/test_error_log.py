"""The error log (Admin -> System -> Error Log, instance/error_logs/) is how a
crash gets diagnosed without SSH-ing into the Pi, so what lands in it has to
be worth reading. Two things were making it lie about how much was wrong:

  1. Every unhandled exception was saved TWICE - Flask logs it itself, on top
     of the got_request_exception hook app.py connects - so 2 crashes read as
     4 errors, and one of each pair had no method/path in the Affected column.
  2. Running the test suite the documented way (cd ~/shopinv && python3 -m
     unittest discover -s tests) wrote the crashes tests make on purpose into
     the LIVE log on the Pi, mixed in with the real ones.

Frank hit both on 2026-09-27: 11 entries in the log, 4 of them one pair of
CrashCleanupTest's "forced crash for test" saved twice over."""
import os
import tempfile
import unittest

from harness import OpsHubTestCase, GC_AFTER_REQUEST
import app as app_module
import db


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _log_files():
    d = app_module.ERROR_LOG_DIR
    return sorted(n for n in os.listdir(d) if n.endswith(".log")) if os.path.isdir(d) else []


def _clear_the_test_log():
    """Empties the error log between tests - but ONLY once it has proved the
    log is the harness's temp one.

    Without that proof this walked the live instance/error_logs/ and deleted
    Frank's real errors, which is exactly what it did on the Pi on
    2026-09-28: app.py there had the fix, tests/harness.py didn't, so
    ERROR_LOG_DIR was still the live folder and eleven saved tracebacks went
    with it. A checkout where the harness predates the redirect must skip
    these tests, not quietly wipe the log - the canary below is what fails
    in that case, and it can't help if the damage is already done by the
    time it runs."""
    d = os.path.abspath(app_module.ERROR_LOG_DIR)
    tmp = os.path.abspath(tempfile.gettempdir()) + os.sep
    if not d.startswith(tmp):
        raise unittest.SkipTest(
            "refusing to delete anything in %s - it isn't a temp folder, so it may be the "
            "live error log. tests/harness.py needs the ERROR_LOG_DIR redirect." % d)
    for name in _log_files():
        os.remove(os.path.join(d, name))


class ErrorLogStaysOutOfTheRepoTest(OpsHubTestCase):
    def test_tests_never_write_to_the_live_error_log(self):
        """ERROR_LOG_DIR must point somewhere temporary while tests run, or a
        suite run on the Pi fills the real log with its own fake crashes."""
        live = os.path.join(REPO_ROOT, "instance", "error_logs")
        self.assertNotEqual(os.path.abspath(app_module.ERROR_LOG_DIR), os.path.abspath(live))
        self.assertNotIn(os.path.abspath(REPO_ROOT) + os.sep,
                         os.path.abspath(app_module.ERROR_LOG_DIR) + os.sep,
                         "test error log must live in the temp folder, not the checkout")


class OneFilePerCrashTest(OpsHubTestCase):
    """A crashing request writes exactly one error file, not one per logger."""

    def setUp(self):
        super().setUp()
        _clear_the_test_log()

    def _crash_a_request(self):
        # Same shape as CrashCleanupTest: a trigger makes the scan's INSERT
        # blow up inside the route, which is an unhandled 500.
        GC_AFTER_REQUEST[0] = False
        self.make_part(qty=10)
        self.login("tech")
        conn = db.get_db()
        conn.execute("CREATE TRIGGER IF NOT EXISTS boom BEFORE INSERT ON transactions "
                     "BEGIN SELECT RAISE(ABORT, 'forced crash for test'); END")
        conn.commit()
        conn.close()
        self.scan("PART-001", "in", 1)

    def test_one_crash_saves_one_file(self):
        self._crash_a_request()
        files = _log_files()
        self.assertEqual(len(files), 1, "expected 1 error file, got %d: %s" % (len(files), files))

    def test_the_saved_entry_names_the_request(self):
        self._crash_a_request()
        name = _log_files()[0]
        with open(os.path.join(app_module.ERROR_LOG_DIR, name)) as f:
            text = f.read()
        entry = app_module._parse_error_log_entry(name, text)
        self.assertEqual(entry["affected"], "POST /api/scan")
        self.assertIn("forced crash for test", entry["text"])
        self.assertIn("Traceback", entry["text"])


class ErrorLogPageTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        _clear_the_test_log()
        with open(os.path.join(app_module.ERROR_LOG_DIR, "2026-09-27_21-25-47-000.log"), "w") as f:
            f.write("2026-09-27 21:25:47 [ERROR] Unhandled exception on POST /api/scan: boom\n"
                    "Traceback (most recent call last):\n  ...\n")

    def test_page_lists_the_entry(self):
        html = self.login("master").get("/admin/system/log").get_data(as_text=True)
        self.assertIn("21:25:47", html)
        self.assertIn("POST /api/scan", html)

    def test_clear_empties_the_log(self):
        c = self.login("master")
        c.post("/admin/system/log/clear", follow_redirects=True)
        self.assertEqual(_log_files(), [])

    def test_only_a_master_admin_can_see_it(self):
        for role in ("tech", "shop_admin", "cfi", "flight_student"):
            with self.subTest(role=role):
                r = self.login(role).get("/admin/system/log")
                self.assertIn(r.status_code, (302, 403))
