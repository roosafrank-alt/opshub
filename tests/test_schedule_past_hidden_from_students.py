"""Idea "availability": students shouldn't be able to tell how busy the
school has been. On the Schedule calendar (Day, Month and List views), a
PAST time block used to show the same detail as a future one to a student
viewer - plane tag, time, status - just without the student/instructor
names (those were already CFI/admin-only). That still let a student read
the shop's historical booking density off the calendar. Past blocks now
collapse to a plain "Past" label for a student viewer in every view; a
CFI/admin viewer is unaffected and still sees full detail.
"""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class SchedulePastHiddenFromStudentsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.day = date.today() - timedelta(days=3)
        conn = db.get_db()
        self.sched = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student,
                              cfi_id=self.cfi, scheduled_date=self.day.isoformat(), scheduled_time="09:00",
                              duration_hours=1.5, status="completed", created_by="Front Desk")
        conn.commit()
        conn.close()

    def _student_hides_it(self, html):
        self.assertIn("Past", html)
        self.assertNotIn(f'data-plane="N123"', html)
        self.assertNotIn(f'data-student-id="{self.student}"', html)
        self.assertNotIn("9:00 AM", html)

    def _cfi_still_sees_it(self, html):
        self.assertIn(f'data-plane="N123"', html)
        self.assertIn("9:00 AM", html)

    def test_day_view_hides_past_detail_from_a_student(self):
        html = self.login("flight_student").get(
            f"/flight/schedule?view=day&date={self.day.isoformat()}").get_data(as_text=True)
        self._student_hides_it(html)

    def test_day_view_keeps_past_detail_for_a_cfi(self):
        html = self.login("cfi").get(
            f"/flight/schedule?view=day&date={self.day.isoformat()}").get_data(as_text=True)
        self._cfi_still_sees_it(html)

    def test_month_view_hides_past_detail_from_a_student(self):
        html = self.login("flight_student").get(
            f"/flight/schedule?view=month&year={self.day.year}&month={self.day.month}").get_data(as_text=True)
        self._student_hides_it(html)

    def test_month_view_keeps_past_detail_for_a_cfi(self):
        html = self.login("cfi").get(
            f"/flight/schedule?view=month&year={self.day.year}&month={self.day.month}").get_data(as_text=True)
        self._cfi_still_sees_it(html)

    def test_list_view_hides_past_detail_from_a_student(self):
        html = self.login("flight_student").get(
            f"/flight/schedule?view=list&year={self.day.year}").get_data(as_text=True)
        self._student_hides_it(html)

    def test_list_view_keeps_past_detail_for_a_cfi(self):
        html = self.login("cfi").get(
            f"/flight/schedule?view=list&year={self.day.year}").get_data(as_text=True)
        self._cfi_still_sees_it(html)

    def test_future_block_is_unaffected_for_a_student(self):
        future_day = date.today() + timedelta(days=3)
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student,
                  cfi_id=self.cfi, scheduled_date=future_day.isoformat(), scheduled_time="10:00",
                  duration_hours=1.5, status="scheduled", created_by="Front Desk")
        conn.commit()
        conn.close()
        html = self.login("flight_student").get(
            f"/flight/schedule?view=day&date={future_day.isoformat()}").get_data(as_text=True)
        self.assertIn(f'data-plane="N123"', html)
        self.assertIn("10:00 AM", html)
