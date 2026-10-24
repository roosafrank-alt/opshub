"""QA finding feat-lesson-still-on: the day-before reminder links to the
dashboard, whose Next Lesson card asks a student "Still on for tomorrow?"
with I'll be there (flight.schedule_still_on) and Can't make it (the
existing student cancel form)."""
from datetime import date, timedelta

import db
import notify
from harness import OpsHubTestCase, seed_row


class StillOnTomorrowTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.asset = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
            "VALUES (?,?,1,?,?,?,?)", ("N81PA", "Piper Archer", 500.0, 480.0, db.now_iso(), db.now_iso()))

    def _book(self, days, **kw):
        conn = db.get_db()
        sid = seed_row(conn, "scheduled_flights", asset_id=self.asset, student_id=self.student,
                       scheduled_date=(date.today() + timedelta(days=days)).strftime("%Y-%m-%d"),
                       scheduled_time="09:00", status="scheduled", created_at=db.now_iso(), **kw)
        conn.commit()
        conn.close()
        return sid

    def test_card_asks_for_tomorrows_lesson(self):
        sid = self._book(1)
        html = self.login("flight_student").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Still on for tomorrow?", html)
        self.assertIn(f"/flight/schedule/{sid}/still_on", html)
        self.assertIn("Can't make it", html)

    def test_card_does_not_ask_about_a_lesson_days_away(self):
        self._book(5)
        html = self.login("flight_student").get("/flight/dashboard").get_data(as_text=True)
        self.assertNotIn("Still on for", html)

    def test_ill_be_there_records_it_and_card_shows_confirmed(self):
        sid = self._book(1)
        c = self.login("flight_student")
        r = c.post(f"/flight/schedule/{sid}/still_on")
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT still_on_at, confirmed_at, status FROM scheduled_flights WHERE id = ?", (sid,))
        self.assertTrue(row["still_on_at"])
        self.assertTrue(row["confirmed_at"])
        self.assertEqual(row["status"], "scheduled")
        html = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("You're confirmed for tomorrow", html)
        self.assertNotIn("Still on for tomorrow?", html)

    def test_only_the_booked_student_can_answer(self):
        sid = self._book(1)
        r = self.login("cfi").post(f"/flight/schedule/{sid}/still_on")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(self.q1("SELECT still_on_at FROM scheduled_flights WHERE id = ?", (sid,))["still_on_at"])

    def test_cant_make_it_uses_the_existing_cancel_form(self):
        sid = self._book(1)
        c = self.login("flight_student")
        html = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn(f"/flight/schedule/{sid}/student_cancel", html)
        c.post(f"/flight/schedule/{sid}/student_cancel", data={"reason": "sick", "ack_fee": "on"})
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id = ?", (sid,))["status"], "cancelled")

    def test_reminder_text_links_to_the_dashboard(self):
        self.assertTrue(notify.FLIGHT_DASHBOARD_URL.endswith("/flight/dashboard"))
