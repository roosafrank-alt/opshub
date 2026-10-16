"""100-hr due day: the booked lesson whose planned length reaches the 100-hour,
shown on Planes and Alerts (admins only) with a Book 100-hr button."""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class HundredHrDueDayTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N2231Q")
        self.exec("UPDATE assets SET is_flight_asset = 1, tach_hours = 850 WHERE id = ?", (self.plane,))
        # 8.4 hours left
        self.exec("""INSERT INTO maintenance_items (asset_id, name, type, category, hour_type, interval_hours,
                     last_done_hours, active) VALUES (?, '100-Hour Inspection', 'hours', '100hour', 'tach', 100, 758.4, 1)""",
                  (self.plane,))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        conn = db.get_db()
        self.student = seed_row(conn, "students", name="Sam Smith", active=1)
        conn.commit()
        conn.close()

    def book(self, days, time, dur):
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student, cfi_id=self.cfi,
                 scheduled_date=(date.today() + timedelta(days=days)).isoformat(), scheduled_time=time,
                 duration_hours=dur, status="scheduled")
        conn.commit()
        conn.close()

    def projection(self):
        import flight
        conn = db.get_db()
        a = conn.execute("SELECT * FROM assets WHERE id = ?", (self.plane,)).fetchone()
        r = flight.hundred_hr_projection(conn, a, flight.hundred_hr_status(conn, a))
        conn.close()
        return r

    def test_no_bookings_no_day(self):
        self.assertIsNone(self.projection())
        self.assertNotIn("Due about", self.login("master").get("/flight/planes").get_data(as_text=True))

    def test_bookings_that_do_not_reach_it(self):
        self.book(1, "09:00", 2.0)
        self.book(2, "09:00", 2.0)
        self.assertIsNone(self.projection())

    def test_day_is_lesson_where_planned_hours_pass_hours_left(self):
        # 8.4 left: 2 + 2 + 2 = 6, then 14:00 lesson of 3 reaches 9 >= 8.4.
        self.book(1, "09:00", 2.0)
        self.book(2, "09:00", 2.0)
        self.book(3, "09:00", 2.0)
        self.book(5, "14:00", 3.0)
        self.book(6, "09:00", 2.0)
        p = self.projection()
        self.assertEqual(p["date"], (date.today() + timedelta(days=5)).isoformat())
        self.assertEqual(p["time"], "2:00 PM")
        self.assertEqual(p["who"], "Sam")
        self.assertEqual(p["duration"], 3.0)

    def test_missing_length_counts_as_one_and_a_half_and_order_is_by_date(self):
        conn = db.get_db()
        for d in (4, 3, 2, 1, 5, 6):
            seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student, cfi_id=self.cfi,
                     scheduled_date=(date.today() + timedelta(days=d)).isoformat(), scheduled_time="10:00",
                     duration_hours=None, status="scheduled")
        conn.commit()
        conn.close()
        # 1.5 each: sixth lesson (day 6) takes the total to 9.0 >= 8.4.
        self.assertEqual(self.projection()["date"], (date.today() + timedelta(days=6)).isoformat())

    def test_cancelled_bookings_do_not_count(self):
        for d in range(1, 8):
            self.book(d, "09:00", 2.0)
        self.exec("UPDATE scheduled_flights SET status = 'cancelled'")
        self.assertIsNone(self.projection())

    def test_admin_sees_day_and_book_button_with_date(self):
        for d in (1, 2, 3, 5):
            self.book(d, "09:00", 3.0)
        due = (date.today() + timedelta(days=3)).isoformat()  # 3+3+3 = 9 >= 8.4
        html = self.login("master").get("/flight/planes").get_data(as_text=True)
        self.assertIn("Due about", html)
        self.assertIn("Book 100-hr for", html)
        self.assertIn(f"scheduled_date={due}", html)
        alerts = self.login("master").get("/flight/alerts").get_data(as_text=True)
        self.assertIn("Due about", alerts)
        self.assertIn("Book 100-hr for", alerts)
        # The project form opens with that date filled in.
        form = self.login("master").get(f"/projects/new?asset_id={self.plane}&quick_type=100hr+Inspection&scheduled_date={due}")
        self.assertIn(f'value="{due}"', form.get_data(as_text=True))

    def test_cfi_sees_neither(self):
        for d in (1, 2, 3, 5):
            self.book(d, "09:00", 3.0)
        c = self.login("cfi")
        for url in ("/flight/planes", "/flight/alerts"):
            html = c.get(url).get_data(as_text=True)
            self.assertNotIn("Due about", html)
            self.assertNotIn("Book 100-hr", html)
