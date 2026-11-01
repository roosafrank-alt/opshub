"""QA ux-book-usual-when-booked: the "Book your usual" offer is hidden once a
student already has 2 or more lessons booked or requested from today on.
Also QA ux-avail-student-picker-zoom: the availability 'Student...' picker
carries the phone-16px class so iPhone doesn't zoom."""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db
import flight


class BookUsualHidesWhenBookedTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N321")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT * FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        conn = db.get_db()
        for back in (30, 23, 16):  # the last 3 lessons make up the usual
            seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student["id"], cfi_id=self.cfi,
                     scheduled_date=(date.today() - timedelta(days=back)).isoformat(), scheduled_time="23:30",
                     duration_hours=1, status="completed", created_by="x")
        conn.commit()
        conn.close()

    def _book(self, days_ahead, status):
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student["id"], cfi_id=self.cfi,
                 scheduled_date=(date.today() + timedelta(days=days_ahead)).isoformat(), scheduled_time="09:00",
                 duration_hours=1, status=status, created_by="x")
        conn.commit()
        conn.close()

    def _offer(self):
        conn = db.get_db()
        try:
            return flight._student_usual_offer(conn, self.student)
        finally:
            conn.close()

    def test_shows_with_none_or_one_ahead_hides_with_two(self):
        self.assertIsNotNone(self._offer())
        self._book(5, "scheduled")
        self.assertIsNotNone(self._offer())
        self._book(6, "pending_approval")  # a waiting request counts too
        self.assertIsNone(self._offer())

    def test_availability_picker_has_phone_size_class(self):
        day = date.today() + timedelta(days=3)
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student["id"], cfi_id=self.cfi,
                 scheduled_date=day.isoformat(), scheduled_time="09:00", duration_hours=1.5, status="scheduled", created_by="x")
        conn.commit()
        conn.close()
        html = self.login("cfi").get(f"/flight/schedule/availability?view=day&date={day.isoformat()}&mode=calendar").get_data(as_text=True)
        self.assertIn("lb-phone16", html)
