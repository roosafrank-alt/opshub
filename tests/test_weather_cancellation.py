"""Weather cancellation (QA feat-weather-day): a CFI/admin calls off a morning's
lessons in one go - no charge, no waitlist offer - and each student gets 3
one-tap new times."""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row, SIDE_EFFECTS
import db


class WeatherCancellationTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.day = date.today() + timedelta(days=2)
        conn = db.get_db()
        self.am = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student, cfi_id=self.cfi,
                           scheduled_date=self.day.isoformat(), scheduled_time="09:00", duration_hours=1.5, status="scheduled")
        self.pm = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student, cfi_id=self.cfi,
                           scheduled_date=self.day.isoformat(), scheduled_time="15:00", duration_hours=1.5, status="scheduled")
        conn.commit()
        conn.close()

    def status(self, fid):
        return self.q1("SELECT status, cancel_reason FROM scheduled_flights WHERE id = ?", (fid,))

    def test_button_and_panel_for_cfi_not_student(self):
        html = self.login("cfi").get("/flight/schedule").get_data(as_text=True)
        self.assertIn("Weather cancellation", html)
        self.assertNotIn("Weather cancellation", self.login("flight_student").get("/flight/schedule").get_data(as_text=True))
        r = self.login("flight_student").get("/flight/schedule/weather")
        self.assertEqual(r.status_code, 302)
        panel = self.login("cfi").get(f"/flight/schedule/weather?date={self.day}&period=morning").get_data(as_text=True)
        self.assertIn("9:00", panel)
        self.assertNotIn("3:00 PM", panel)

    def test_morning_cancel_only_morning_and_offers_three_times(self):
        SIDE_EFFECTS.clear()
        c = self.login("cfi")
        r = c.post("/flight/schedule/weather", data=dict(date=self.day.isoformat(), period="morning",
                                                         do="cancel", ids=[str(self.am)]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.status(self.am)["status"], "cancelled")
        self.assertEqual(self.status(self.am)["cancel_reason"], "Weather cancellation")
        self.assertEqual(self.status(self.pm)["status"], "scheduled")
        self.assertEqual(len(self.q("SELECT id FROM weather_offers WHERE cancelled_flight_id = ?", (self.am,))), 3)
        self.assertEqual(self.q("SELECT id FROM waitlist_offers"), [])

    def test_student_taps_new_time_once(self):
        self.login("cfi").post("/flight/schedule/weather", data=dict(date=self.day.isoformat(), do="cancel", ids=[str(self.am)]))
        o = self.q("SELECT * FROM weather_offers WHERE cancelled_flight_id = ?", (self.am,))
        s = self.login("flight_student")
        self.assertIn("Weather cancellation", s.get(f"/flight/schedule/weather/{self.am}").get_data(as_text=True))
        s.post(f"/flight/schedule/weather/{self.am}", data=dict(offer_id=str(o[1]["id"])))
        n = self.q("SELECT * FROM scheduled_flights WHERE created_by = 'Weather rebook'")
        self.assertEqual(len(n), 1)
        self.assertEqual((n[0]["scheduled_date"], n[0]["scheduled_time"]), (o[1]["scheduled_date"], "09:00"))
        s.post(f"/flight/schedule/weather/{self.am}", data=dict(offer_id=str(o[0]["id"])))
        self.assertEqual(len(self.q("SELECT id FROM scheduled_flights WHERE created_by = 'Weather rebook'")), 1)
