"""Idea "student requests" revision 2: a student should see Request Change /
Cancel Flight buttons on their own booking wherever it shows up on the
Schedule (Day, Month, List/Custom Range), not only on the dashboard. The
Schedule Detail popup itself is shared everywhere - it just needs the
booking's data-request-change-url / data-student-cancel-url / data-student-id
attributes, which used to only be rendered by the dashboard's own chip
markup."""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class StudentScheduleActionsEverywhereTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.day = date.today() + timedelta(days=3)
        conn = db.get_db()
        self.sched = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student,
                              cfi_id=self.cfi, scheduled_date=self.day.isoformat(), scheduled_time="09:00",
                              duration_hours=1.5, status="scheduled", created_by="Front Desk")
        conn.commit()
        conn.close()

    def _urls_present(self, html):
        self.assertIn(f'data-student-id="{self.student}"', html)
        self.assertIn(f"/flight/schedule/{self.sched}/request_change", html)
        self.assertIn(f"/flight/schedule/{self.sched}/student_cancel", html)

    def test_day_view_carries_the_student_action_urls(self):
        html = self.login("flight_student").get(
            f"/flight/schedule?view=day&date={self.day.isoformat()}").get_data(as_text=True)
        self._urls_present(html)

    def test_month_view_carries_the_student_action_urls(self):
        html = self.login("flight_student").get(
            f"/flight/schedule?view=month&date={self.day.isoformat()}").get_data(as_text=True)
        self._urls_present(html)

    def test_list_view_carries_the_student_action_urls(self):
        html = self.login("flight_student").get(
            f"/flight/schedule?view=list&date={self.day.isoformat()}").get_data(as_text=True)
        self._urls_present(html)
