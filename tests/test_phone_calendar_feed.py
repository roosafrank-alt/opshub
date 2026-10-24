"""QA finding feat-phone-calendar: a secret per-person .ics feed plus an
'Add to my phone calendar' button (flight.calendar_sync / calendar_feed)."""
from datetime import date, timedelta

import db
import flight
from harness import OpsHubTestCase, seed_row


class PhoneCalendarFeedTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        conn = db.get_db()
        self.other_cfi = seed_row(conn, "cfis", name="Other CFI", username="other-cfi-pcf", active=1, created_at=db.now_iso())
        self.other_student = seed_row(conn, "students", name="Other Student", username="other-stu-pcf", active=1, created_at=db.now_iso())
        conn.commit()
        conn.close()
        self.asset = self.exec("INSERT INTO assets (tag, name, is_flight_asset, created_at, updated_at) VALUES (?,?,1,?,?)",
                               ("N81PA", "Piper Archer", db.now_iso(), db.now_iso()))
        self.day = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")

    def _book(self, cfi, student, time="09:00", status="scheduled", private="SECRET-NOTE"):
        conn = db.get_db()
        i = seed_row(conn, "scheduled_flights", asset_id=self.asset, cfi_id=cfi, student_id=student,
                     scheduled_date=self.day, scheduled_time=time, duration_hours=2, status=status,
                     private_notes=private, created_at=db.now_iso())
        conn.commit()
        conn.close()
        return i

    def _feed(self, role):
        c = self.login(role)
        html = c.get("/flight/calendar-sync").get_data(as_text=True)
        import re
        m = re.search(r'/flight/calendar-feed/([A-Za-z0-9_-]+)\.ics', html)
        self.assertTrue(m, html[:500])
        return m.group(1), html

    def test_button_on_schedule_and_my_schedule(self):
        self.assertIn("Add to my phone calendar", self.login("cfi").get("/flight/cfis/schedule").get_data(as_text=True))
        self.assertIn("Add to my phone calendar", self.login("flight_student").get("/flight/schedule").get_data(as_text=True))

    def test_feed_has_only_own_lessons_and_no_private_notes(self):
        mine = self._book(self.cfi, self.student)
        other = self._book(self.other_cfi, self.other_student, time="13:00")
        token, html = self._feed("cfi")
        self.assertIn("webcal://", html)
        ics = self.client.get(f"/flight/calendar-feed/{token}.ics")  # no login needed
        self.assertEqual(ics.status_code, 200)
        self.assertIn("text/calendar", ics.headers["Content-Type"])
        body = ics.get_data(as_text=True)
        self.assertIn(f"lesson-{mine}@opshub", body)
        self.assertNotIn(f"lesson-{other}@opshub", body)
        self.assertIn("N81PA", body)
        self.assertNotIn("SECRET-NOTE", body)
        self.assertNotIn("Other Student", body)

    def test_student_feed_and_cancelled_disappears(self):
        a = self._book(self.cfi, self.student)
        token, _ = self._feed("flight_student")
        self.assertIn(f"lesson-{a}@opshub", self.client.get(f"/flight/calendar-feed/{token}.ics").get_data(as_text=True))
        self.exec("UPDATE scheduled_flights SET status='cancelled' WHERE id=?", (a,))
        self.assertNotIn(f"lesson-{a}@opshub", self.client.get(f"/flight/calendar-feed/{token}.ics").get_data(as_text=True))

    def test_token_unguessable_and_regenerate_revokes(self):
        token, _ = self._feed("cfi")
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(self.client.get("/flight/calendar-feed/nope.ics").status_code, 404)
        r = self.client.post("/flight/calendar-sync")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(f"/flight/calendar-feed/{token}.ics").status_code, 404)
        new, _ = self._feed("cfi")
        self.assertNotEqual(new, token)
        self.assertEqual(self.client.get(f"/flight/calendar-feed/{new}.ics").status_code, 200)

    def test_other_roles_get_no_feed(self):
        r = self.login("shop_worker").get("/flight/calendar-sync") if "shop_worker" in self.users else None
        if r is not None:
            self.assertEqual(r.status_code, 302)
