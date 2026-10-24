"""Idea feat-rebook-after-cancel: after a student cancels their own lesson,
the dashboard offers the next open days for the same plane and instructor and
one tap books one as the same lesson."""
from datetime import datetime, timedelta

from harness import OpsHubTestCase, seed_row
import db


class StudentRebookAfterCancelTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?",
                                  (self.users["flight_student"]["id"],))["id"]
        self.when = datetime.now() + timedelta(days=3)
        conn = db.get_db()
        self.sched = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student_id,
                              scheduled_date=self.when.date().isoformat(), scheduled_time="10:00",
                              duration_hours=1.5, notes="Pattern work", status="scheduled", created_by="Front Desk")
        conn.commit()
        conn.close()

    def _cancel(self):
        c = self.login("flight_student")
        r = c.post(f"/flight/schedule/{self.sched}/student_cancel", data={"reason": "sick"}, follow_redirects=True)
        return c, r.get_data(as_text=True)

    def test_box_offers_three_days_after_cancel(self):
        c, html = self._cancel()
        self.assertIn("Pick a new time?", html)
        self.assertIn("Other times", html)
        self.assertIn("No thanks", html)
        self.assertEqual(html.count('name="date"'), 3)

    def test_one_tap_books_the_same_lesson(self):
        c, html = self._cancel()
        day = (self.when + timedelta(days=1)).date().isoformat()
        r = c.post(f"/flight/schedule/{self.sched}/rebook", data={"date": day}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        new = self.q1("SELECT * FROM scheduled_flights WHERE student_id = ? AND scheduled_date = ? AND status = 'scheduled'",
                      (self.student_id, day))
        self.assertIsNotNone(new)
        self.assertEqual((new["asset_id"], new["scheduled_time"], new["duration_hours"], new["notes"]),
                         (self.plane, "10:00", 1.5, "Pattern work"))

    def test_date_not_offered_is_refused(self):
        c, html = self._cancel()
        far = (self.when + timedelta(days=60)).date().isoformat()
        c.post(f"/flight/schedule/{self.sched}/rebook", data={"date": far}, follow_redirects=True)
        self.assertIsNone(self.q1("SELECT id FROM scheduled_flights WHERE scheduled_date = ? AND status = 'scheduled'", (far,)))

    def test_no_box_without_a_cancel(self):
        html = self.login("flight_student").get(f"/flight/dashboard?rebook={self.sched}").get_data(as_text=True)
        self.assertNotIn("Pick a new time?", html)

    def test_nothing_open_says_so(self):
        conn = db.get_db()
        for i in range(1, 16):
            d = (self.when + timedelta(days=i)).date().isoformat()
            seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student_id,
                     scheduled_date=d, scheduled_time="10:00", duration_hours=1.0, status="scheduled", created_by="x")
        conn.commit()
        conn.close()
        c, html = self._cancel()
        self.assertIn("Nothing is open", html)
        self.assertIn("Other times", html)


if __name__ == "__main__":
    import unittest
    unittest.main()
