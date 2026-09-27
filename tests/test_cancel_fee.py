"""Idea "canceling flight options in settings": a configurable
late-cancellation window/fee, charged automatically (via the student
ledger) when a student cancels their own booking inside it."""
from datetime import date, datetime, timedelta

from harness import OpsHubTestCase, seed_row
import db


class CancelFeeSettingsTest(OpsHubTestCase):
    def test_defaults_to_24h_and_no_fee(self):
        html = self.login("master").get("/flight/settings/cancellation-fee").get_data(as_text=True)
        self.assertIn('value="24"', html)
        self.assertIn('value="0.00"', html)

    def test_admin_can_set_the_policy(self):
        c = self.login("master")
        r = c.post("/flight/settings/cancellation-fee", data={"window_hours": "48", "fee_amount": "75"},
                   follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        html = c.get("/flight/settings/cancellation-fee").get_data(as_text=True)
        self.assertIn('value="48"', html)
        self.assertIn('value="75.00"', html)

    def test_non_admin_cannot_reach_the_settings_page(self):
        r = self.login("cfi").get("/flight/settings/cancellation-fee", follow_redirects=True)
        self.assertNotIn("Late-Cancellation Fee", r.get_data(as_text=True))


class StudentCancelFeeTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?",
                                  (self.users["flight_student"]["id"],))["id"]
        # Turn the policy on: 24h window, $50 fee.
        self.login("master").post("/flight/settings/cancellation-fee",
                                   data={"window_hours": "24", "fee_amount": "50"})

    def _book(self, when):
        conn = db.get_db()
        sched = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student_id,
                         scheduled_date=when.date().isoformat(), scheduled_time=when.strftime("%H:%M"),
                         duration_hours=1.0, status="scheduled", created_by="Front Desk")
        conn.commit()
        conn.close()
        return sched

    def test_late_cancel_charges_the_fee_and_lands_in_the_ledger(self):
        sched = self._book(datetime.now() + timedelta(hours=2))
        c = self.login("flight_student")
        r = c.post(f"/flight/schedule/{sched}/student_cancel",
                   data={"reason": "sick", "ack_fee": "on"}, follow_redirects=True)
        self.assertIn("50.00", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id = ?", (sched,))["status"], "cancelled")
        entry = self.q1("SELECT * FROM student_ledger WHERE student_id = ? AND entry_type = 'cancellation_fee'",
                        (self.student_id,))
        self.assertIsNotNone(entry)
        self.assertEqual(entry["amount"], -50.0)
        self.assertEqual(self.q1("SELECT balance FROM students WHERE id = ?", (self.student_id,))["balance"], -50.0)

    def test_late_cancel_without_ack_is_rejected_and_not_charged(self):
        sched = self._book(datetime.now() + timedelta(hours=2))
        c = self.login("flight_student")
        c.post(f"/flight/schedule/{sched}/student_cancel", data={"reason": "sick"}, follow_redirects=True)
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id = ?", (sched,))["status"], "scheduled")
        self.assertIsNone(self.q1("SELECT * FROM student_ledger WHERE student_id = ?", (self.student_id,)))

    def test_early_cancel_is_free(self):
        sched = self._book(datetime.now() + timedelta(days=5))
        c = self.login("flight_student")
        c.post(f"/flight/schedule/{sched}/student_cancel", data={"reason": "sick"}, follow_redirects=True)
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id = ?", (sched,))["status"], "cancelled")
        self.assertIsNone(self.q1("SELECT * FROM student_ledger WHERE student_id = ?", (self.student_id,)))

    def test_fee_set_to_zero_never_charges_even_when_late(self):
        self.login("master").post("/flight/settings/cancellation-fee",
                                  data={"window_hours": "24", "fee_amount": "0"})
        sched = self._book(datetime.now() + timedelta(hours=2))
        c = self.login("flight_student")
        c.post(f"/flight/schedule/{sched}/student_cancel", data={"reason": "sick"}, follow_redirects=True)
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id = ?", (sched,))["status"], "cancelled")
        self.assertIsNone(self.q1("SELECT * FROM student_ledger WHERE student_id = ?", (self.student_id,)))
