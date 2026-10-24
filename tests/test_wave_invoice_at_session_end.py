"""Idea "billing": ending a lesson as Unpaid used to just log it owed, with
no way to actually get paid short of a separate trip to Billing > Invoice
in Wave later. End Session now offers "Invoice through Wave now" (shown
once Unpaid is picked, only to whoever can already see money - can_bill),
which makes one real Wave invoice for just that flight right there, emails
it, and shows an "Open Wave invoice" link in the same "Session ended and
logged" confirmation (see flight.log_end / _ended_flight_wave_invoice).
Also checks Wave back every 20s for a few minutes afterward
(flight._quick_wave_check) instead of waiting for the usual 30-minute
background poll, so the flight flips to Paid on its own if the student
pays on the spot. Wave itself is faked - see tests/test_wave_invoicing.py's
FakeWave, reused here."""
from datetime import date
from unittest import mock

import db
import flight
from harness import OpsHubTestCase, seed_row
from test_wave_invoicing import WaveTestBase


class _ImmediateThread:
    """Stands in for threading.Thread in these tests: runs the target right
    away, in the same thread, instead of a real background one. Combined
    with the time.sleep patch below, this makes log_end's "check back every
    20s" watcher (flight._quick_wave_check) run instantly and deterministically
    instead of leaving a real thread sleeping in the background - one that
    would otherwise still be alive, making real (unfaked) calls to Wave's
    real API, once this test's FakeWave patch is torn down after it ends."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


class WaveInvoiceAtSessionEndTest(WaveTestBase):
    def setUp(self):
        super().setUp()
        sleep_patch = mock.patch.object(flight.time, "sleep", lambda s: None)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        thread_patch = mock.patch.object(flight.threading, "Thread", _ImmediateThread)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE users SET email = 'stu@example.com' WHERE id = ?", (self.users["flight_student"]["id"],))
        self.exec("UPDATE students SET plane_rate_override = 150 WHERE id = ?", (self.student,))
        # The assigned CFI on these test flights is cfi_billing (flight_role
        # cfi + can_bill=1, see harness.ROLES) - the real-world "CFI who's
        # also allowed to see/collect money" this feature is for. Plain
        # "cfi" (no can_bill) is used only where a test needs someone who
        # can end a flight but not see the Wave checkbox.
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi_billing"]["id"],))["id"]
        self.exec("UPDATE cfis SET rate_per_hour = 60 WHERE id = ?", (self.cfi,))
        self.plain_cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.exec("UPDATE cfis SET rate_per_hour = 60 WHERE id = ?", (self.plain_cfi,))
        self.asset = self.make_asset("N321CC")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.asset,))
        self.started_at = "2026-09-28 09:00:00"

    def _running_flight(self, **kw):
        kw.setdefault("cfi_id", self.cfi)
        kw.setdefault("student_id", self.student)
        kw.setdefault("asset_id", self.asset)
        kw.setdefault("flight_date", date.today().isoformat())
        kw.setdefault("solo", 0)
        kw.setdefault("started_at", self.started_at)
        # stopped_at (clock stopped, End Session pressed once already) is
        # what makes log_active.html show the Hobbs/paid-unpaid form at all
        # (see f.can_end and f.stopped_at in the template) - without it the
        # card only offers the Stop/Pause buttons.
        kw.setdefault("stopped_at", "2026-09-28 10:00:00")
        kw.setdefault("hobbs_start", 100.0)
        kw.setdefault("tach_start", 90.0)
        conn = db.get_db()
        fid = seed_row(conn, "flights", **kw)
        conn.commit()
        conn.close()
        return fid

    def _end_unpaid(self, c, fid, **form_overrides):
        form = dict(hobbs_end="101.5", tach_end="90.8", paid="0")
        form.update(form_overrides)
        r = c.post(f"/flight/log/{fid}/end", data=form)
        self.assertEqual(r.status_code, 302)
        return c.get(r.headers["Location"]).get_data(as_text=True)

    # ----- the checkbox on End Session --------------------------------------

    def test_checkbox_only_shown_once_wave_connected_and_can_bill(self):
        fid = self._running_flight()
        c = self.login("cfi_billing")
        html = c.get(f"/flight/log/active?flight_id={fid}").get_data(as_text=True)
        self.assertNotIn("wave_invoice_now", html)  # not connected yet
        self.connect_wave()
        html = c.get(f"/flight/log/active?flight_id={fid}").get_data(as_text=True)
        self.assertIn("wave_invoice_now", html)
        self.assertIn("Invoice through Wave now", html)
        # A CFI without billing rights never sees it, connected or not, even
        # on a flight they're the assigned instructor for (own_fid below).
        own_fid = self._running_flight(cfi_id=self.plain_cfi)
        html = self.login("cfi").get(f"/flight/log/active?flight_id={own_fid}").get_data(as_text=True)
        self.assertNotIn("wave_invoice_now", html)

    # ----- ending unpaid with the box ticked --------------------------------

    def test_unpaid_with_wave_checked_creates_and_sends_an_invoice(self):
        self.connect_wave()
        fid = self._running_flight()
        c = self.login("cfi_billing")
        html = self._end_unpaid(c, fid, wave_invoice_now="1")
        self.assertIn("Wave invoice #101 made for $", html)
        self.assertIn("emailed to stu@example.com", html)
        self.assertIn("Open Wave invoice #101", html)
        f = self.q1("SELECT paid, wave_invoice_id FROM flights WHERE id = ?", (fid,))
        self.assertEqual(f["paid"], 0)  # still unpaid until Wave says otherwise
        self.assertIsNotNone(f["wave_invoice_id"])
        w = self.q1("SELECT * FROM wave_invoices WHERE id = ?", (f["wave_invoice_id"],))
        self.assertEqual(w["kind"], "student")
        self.assertEqual(w["ref_id"], self.student)
        self.assertTrue(w["sent_at"])
        items = self.wave.created_items()[0]
        self.assertTrue(all(i["productId"] == "P-FLIGHT" for i in items))

    def test_unchecked_box_logs_unpaid_with_no_wave_call(self):
        self.connect_wave()
        fid = self._running_flight()
        c = self.login("cfi_billing")
        html = self._end_unpaid(c, fid)  # wave_invoice_now omitted
        self.assertIn("Session ended and logged.", html)
        self.assertNotIn("Wave invoice", html)
        self.assertEqual(self.wave.created_items(), [])
        self.assertIsNone(self.q1("SELECT wave_invoice_id FROM flights WHERE id = ?", (fid,))["wave_invoice_id"])

    def test_paid_session_never_invoices_through_wave(self):
        self.connect_wave()
        fid = self._running_flight()
        c = self.login("cfi_billing")
        r = c.post(f"/flight/log/{fid}/end", data={
            "hobbs_end": "101.5", "tach_end": "90.8", "paid": "1",
            "payment_amount": "225.00", "payment_method": "Cash", "wave_invoice_now": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.wave.created_items(), [])

    def test_regular_cfi_without_billing_rights_cannot_trigger_it(self):
        self.connect_wave()
        fid = self._running_flight(cfi_id=self.plain_cfi)
        c = self.login("cfi")
        self._end_unpaid(c, fid, wave_invoice_now="1")
        self.assertEqual(self.wave.created_items(), [])
        self.assertIsNone(self.q1("SELECT wave_invoice_id FROM flights WHERE id = ?", (fid,))["wave_invoice_id"])

    def test_guest_flight_is_left_off_wave_invoicing(self):
        self.connect_wave()
        fid = self._running_flight(guest_name="Intro Guest")
        c = self.login("cfi_billing")
        self._end_unpaid(c, fid, wave_invoice_now="1")
        self.assertEqual(self.wave.created_items(), [])

    def test_wave_error_still_keeps_the_flight_logged_unpaid(self):
        self.connect_wave()
        fid = self._running_flight()

        def boom(*a, **k):
            raise flight.wave_billing.WaveError("Wave refused the access token")
        with mock.patch.object(flight.wave_billing, "create_invoice", boom):
            c = self.login("cfi_billing")
            html = self._end_unpaid(c, fid, wave_invoice_now="1")
        self.assertIn("Session ended and logged.", html)
        self.assertIn("wasn&#39;t made: Wave refused the access token", html)
        f = self.q1("SELECT paid, wave_invoice_id FROM flights WHERE id = ?", (fid,))
        self.assertEqual(f["paid"], 0)
        self.assertIsNone(f["wave_invoice_id"])

    # ----- flips to Paid once Wave says so, without waiting 30 minutes -----

    def test_quick_check_flips_the_flight_paid_once_wave_reports_it(self):
        self.connect_wave()
        fid = self._running_flight()
        c = self.login("cfi_billing")
        self._end_unpaid(c, fid, wave_invoice_now="1")
        local_id = self.q1("SELECT wave_invoice_id FROM flights WHERE id = ?", (fid,))["wave_invoice_id"]
        self.wave.pay("INV1")
        flight._quick_wave_check(local_id)  # simulates the next 20s check finding it paid
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id = ?", (fid,))["paid"], 1)

    def test_quick_check_gives_up_quietly_if_never_paid(self):
        self.connect_wave()
        fid = self._running_flight()
        c = self.login("cfi_billing")
        self._end_unpaid(c, fid, wave_invoice_now="1")
        local_id = self.q1("SELECT wave_invoice_id FROM flights WHERE id = ?", (fid,))["wave_invoice_id"]
        flight._quick_wave_check(local_id)  # never paid - just returns
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id = ?", (fid,))["paid"], 0)
