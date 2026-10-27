"""Idea "on the availability page": a booked slot shows a student an
"Add to waitlist" button that joins the cancellation waitlist for that plane,
weekday and part of the day. CFIs and admins get it too, with a student picker."""
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

    def test_staff_see_button_with_student_picker_and_can_join(self):
        for who in ("cfi", "flight_admin"):
            c = self.login(who)
            html = c.get(self.url).get_data(as_text=True)
            self.assertIn("Add to waitlist", html)
            self.assertIn('name="student_id"', html)
            r = c.post("/flight/waitlist/join", data={
                "student_id": str(self.student), "days": str(self.day.weekday()), "periods": "morning",
                "asset_id": str(self.plane), "until_date": self.day.isoformat()})
            self.assertEqual(r.status_code, 302)
        n = self.q1("SELECT COUNT(*) c FROM flight_waitlist WHERE student_id = ? AND active = 1", (self.student,))["c"]
        self.assertEqual(n, 2)

    def test_student_has_no_picker(self):
        html = self.login("flight_student").get(self.url).get_data(as_text=True)
        self.assertNotIn('name="student_id"', html)


class AvailabilityWaitlistOnStateTest(AvailabilityBookedWaitlistButtonTest):
    """Idea "on the students account when they click add to wait list": after
    joining, the slot shows "On the waitlist" with a check instead of the button."""

    def test_student_join_returns_to_grid_and_shows_check(self):
        c = self.login("flight_student")
        r = c.post("/flight/waitlist/join", data={
            "days": str(self.day.weekday()), "periods": "morning", "asset_id": str(self.plane),
            "until_date": self.day.isoformat(), "next": self.url})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/flight/schedule/availability", r.headers["Location"])
        html = c.get(self.url).get_data(as_text=True)
        self.assertIn("On the waitlist", html)
        self.assertIn("bi-check-square-fill", html)
        self.assertNotIn("Add to waitlist", html)

    def test_off_site_next_is_ignored(self):
        c = self.login("flight_student")
        r = c.post("/flight/waitlist/join", data={
            "days": str(self.day.weekday()), "periods": "morning", "asset_id": str(self.plane),
            "until_date": self.day.isoformat(), "next": "https://evil.example/"})
        self.assertIn("/flight/waitlist", r.headers["Location"])
