"""QA feat-no-show-button: a lesson the student never came to gets a
"No-show" button next to "Log this lesson" - it cancels the booking as
"No-show", charges the school's late-cancel fee and frees the plane."""
import datetime

from harness import OpsHubTestCase
import db


class NoShowButtonTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", ("N2231Q", "Cessna 172", 1, 1000.0, 900.0, db.now_iso(), db.now_iso()))
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE username = 'cfi'")["id"]
        self.student_id = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]
        self.now = datetime.datetime.now()
        self.login("master").post("/flight/settings/school", data={"window_hours": "24", "fee_amount": "50"})

    def _book(self, when):
        return self.exec(
            "INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date, scheduled_time, "
            "duration_hours, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (self.plane, self.cfi_id, self.student_id, when.strftime("%Y-%m-%d"),
             when.strftime("%H:%M"), 1.0, "scheduled", db.now_iso()))

    def _past(self):
        past = self.now - datetime.timedelta(hours=3)
        if past.date() != self.now.date():
            self.skipTest("too close to midnight")
        return past

    def test_dashboard_offers_no_show_with_the_fee(self):
        self._book(self._past())
        body = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Log this lesson", body)
        self.assertIn("No-show", body)
        self.assertIn("charge $50.00", body)

    def test_no_show_cancels_charges_fee_and_frees_the_plane(self):
        sched = self._book(self._past())
        r = self.login("cfi").post(f"/flight/schedule/{sched}/no_show", follow_redirects=True)
        self.assertIn("Marked no-show", r.get_data(as_text=True))
        row = self.q1("SELECT status, cancel_reason FROM scheduled_flights WHERE id = ?", (sched,))
        self.assertEqual((row["status"], row["cancel_reason"]), ("cancelled", "No-show"))
        entry = self.q1("SELECT * FROM student_ledger WHERE student_id = ? AND entry_type = 'cancellation_fee'",
                        (self.student_id,))
        self.assertEqual(entry["amount"], -50.0)
        # The same plane and time can be booked again.
        again = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        self.assertNotIn("Log this lesson", again)

    def test_no_fee_set_still_closes_it_out(self):
        self.login("master").post("/flight/settings/school", data={"window_hours": "24", "fee_amount": "0"})
        sched = self._book(self._past())
        self.login("cfi").post(f"/flight/schedule/{sched}/no_show")
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id = ?", (sched,))["status"], "cancelled")
        self.assertIsNone(self.q1("SELECT id FROM student_ledger WHERE entry_type = 'cancellation_fee'"))

    def test_future_lesson_cannot_be_marked_no_show(self):
        sched = self._book(self.now + datetime.timedelta(hours=3))
        self.login("cfi").post(f"/flight/schedule/{sched}/no_show")
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id = ?", (sched,))["status"], "scheduled")

    def test_student_cannot_mark_no_show_and_it_only_works_once(self):
        sched = self._book(self._past())
        self.login("flight_student").post(f"/flight/schedule/{sched}/no_show")
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id = ?", (sched,))["status"], "scheduled")
        c = self.login("cfi")
        c.post(f"/flight/schedule/{sched}/no_show")
        c.post(f"/flight/schedule/{sched}/no_show")
        n = self.q1("SELECT COUNT(*) n FROM student_ledger WHERE entry_type = 'cancellation_fee'")["n"]
        self.assertEqual(n, 1)
