"""Idea "Schedule": the schedule form shows which planes are open
on the picked day (JSON from /flight/schedule/open-slots, filled into a box
under the date calendar)."""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class ScheduleFormOpenSlotsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.day = date.today() + timedelta(days=3)
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student,
                 cfi_id=self.cfi, scheduled_date=self.day.isoformat(), scheduled_time="09:30",
                 duration_hours=1.5, status="scheduled", created_by="Front Desk")
        conn.commit()
        conn.close()

    def test_json_lists_open_slots_and_skips_booked(self):
        c = self.login("flight_admin")
        data = c.get(f"/flight/schedule/open-slots?date={self.day.isoformat()}").get_json()
        self.assertEqual(data["date"], self.day.isoformat())
        times = [s["time"] for p in data["planes"] if p["id"] == self.plane for s in p["slots"]]
        self.assertIn("08:00", times)
        self.assertNotIn("09:30", times)

    def test_bad_date_gives_empty_list(self):
        c = self.login("flight_admin")
        self.assertEqual(c.get("/flight/schedule/open-slots?date=nope").get_json()["planes"], [])

    def test_form_has_the_box(self):
        c = self.login("flight_admin")
        html = c.get("/flight/schedule/new").get_data(as_text=True)
        self.assertIn('id="day-open-box"', html)
        self.assertIn("/flight/schedule/open-slots", html)
