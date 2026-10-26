"""Idea "Drop": the Availability page's waitlist student picker opens as a pop-up
outside the narrow Booked cell."""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class AvailabilityWaitlistPopupTest(OpsHubTestCase):
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

    def test_availability_page_has_waitlist_popup(self):
        c = self.login("flight_admin")
        html = c.get(f"/flight/schedule/availability?view=day&date={self.day.isoformat()}&mode=calendar").get_data(as_text=True)
        self.assertIn("wl-pop", html)
        self.assertIn("Type a name...", html)

    def test_popup_also_opens_on_phones_as_closable_sheet(self):
        c = self.login("flight_admin")
        html = c.get(f"/flight/schedule/availability?view=day&date={self.day.isoformat()}&mode=calendar").get_data(as_text=True)
        self.assertIn("wl-sheet", html)
        self.assertIn("wlCovered", html)
        self.assertIn("Choose student", html)
