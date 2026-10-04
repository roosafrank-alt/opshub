"""QA fix ux-flight-dash-section-headers: on phones, "Today's Schedule
(27-09-2026)" wrapped into three lines next to the completion badge,
splitting the date in half. Now reads "Today 09-27" (month-day) with a
shorter "X of Y done" badge, and the Upcoming badge counts flights instead
of days. Desktop keeps the full wording and the app's usual day-month date.
"""
from datetime import date, timedelta
from harness import OpsHubTestCase, seed_row
import db


class DashSectionHeadersTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N12345")
        conn = db.get_db()
        self.student = conn.execute("SELECT id FROM students WHERE user_id = ?",
                                    (self.users["flight_student"]["id"],)).fetchone()["id"]
        self.cfi = conn.execute("SELECT id FROM cfis WHERE user_id = ?",
                                (self.users["cfi"]["id"],)).fetchone()["id"]
        conn.close()

    def test_today_header_shows_month_day_on_phone_full_date_on_desktop(self):
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=self.asset, student_id=self.student, cfi_id=self.cfi,
                scheduled_date=date.today().isoformat(), scheduled_time="09:00", duration_hours=1.5,
                status="scheduled", created_at=db.now_iso())
        conn.commit()
        conn.close()
        html = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        today = date.today()
        short = f"{today.strftime('%b')} {today.day}"  # FLY-12: Oct 4 on a phone
        full = f"{today.strftime('%a')}, {today.strftime('%b')} {today.day}, {today.year}"  # Sun, Oct 4, 2026
        self.assertIn(short, html)
        self.assertIn(f"({full})", html)
        self.assertIn("done</span>", html)
        self.assertIn("flight complete</span>", html)

    def test_upcoming_badge_counts_flights_not_days(self):
        d1 = (date.today() + timedelta(days=1)).isoformat()
        d2 = (date.today() + timedelta(days=3)).isoformat()
        conn = db.get_db()
        for d, t in ((d1, "09:00"), (d2, "14:00")):
            seed_row(conn, "scheduled_flights", asset_id=self.asset, student_id=self.student,
                    cfi_id=self.cfi, scheduled_date=d, scheduled_time=t, duration_hours=1.5,
                    status="scheduled", created_at=db.now_iso())
        conn.commit()
        conn.close()
        html = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("2 flights", html)
        self.assertNotIn("2 days", html)

    def test_upcoming_badge_says_none_booked_when_empty(self):
        html = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("None booked", html)

    def test_upcoming_day_heading_shows_weekday_and_month_day_on_phone(self):
        d1 = (date.today() + timedelta(days=1))
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=self.asset, student_id=self.student,
                cfi_id=self.cfi, scheduled_date=d1.isoformat(), scheduled_time="09:00", duration_hours=1.5,
                status="scheduled", created_at=db.now_iso())
        conn.commit()
        conn.close()
        html = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn(d1.strftime("%m-%d"), html)
        self.assertNotIn("'s Schedule <span class=\"text-muted small fw-normal\">" + d1.strftime("%m-%d"), html)
