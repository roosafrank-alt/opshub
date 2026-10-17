"""Idea "on the availability page": a booked slot shows a student an
"Add to waitlist" button that joins the cancellation waitlist for that plane,
weekday and part of the day. Staff don't get the button."""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class AvailabilityBookedWaitlistButtonTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.day = date.today() + timedelta(days=3)
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student,
                 cfi_id=self.cfi, scheduled_date=self.day.isoformat(), scheduled_time="09:00",
                 duration_hours=1.5, status="scheduled", created_by="Front Desk")
        conn.commit()
        conn.close()
        self.url = f"/flight/schedule/availability?view=day&date={self.day.isoformat()}&mode=calendar"

    def test_student_sees_button_and_can_join(self):
        c = self.login("flight_student")
        html = c.get(self.url).get_data(as_text=True)
        self.assertIn("Add to waitlist", html)
        r = c.post("/flight/waitlist/join", data={
            "days": str(self.day.weekday()), "periods": "morning", "asset_id": str(self.plane),
            "until_date": self.day.isoformat()})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM flight_waitlist WHERE student_id = ? AND active = 1", (self.student,))
        self.assertEqual(row["asset_ids"], str(self.plane))
        self.assertEqual(row["until_date"], self.day.isoformat())

    def test_staff_do_not_see_button(self):
        html = self.login("cfi").get(self.url).get_data(as_text=True)
        self.assertNotIn("Add to waitlist", html)
