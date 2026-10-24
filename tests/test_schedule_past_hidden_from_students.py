"""Idea "availability", then idea "past views": students shouldn't be able
to tell how busy the school has been. On the Schedule calendar's List view,
a PAST time block still collapses to a plain "Past" label for a student
viewer (no plane tag, time or status) - that part is unchanged. But on the
Day/Week/Month/Quarter/Year views, idea "past views" narrowed this: a
student's OWN past flight now shows full detail again, exactly like a
future one (so they can still see their own flight history) - only
another student's past booking (or a CFI break) is affected, and it now
drops off the calendar entirely for a student viewer instead of showing an
anonymized "Past" placeholder. A CFI/admin viewer is unaffected everywhere
and still sees full detail for every flight, past or not.
"""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class SchedulePastHiddenFromStudentsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        # self.make_asset()/self.exec() each open and commit their own
        # connection - do all of those first, before opening the single
        # `conn` below for seed_row, so it's never left mid-transaction
        # at the same time as one of those (SQLite only allows one writer
        # at a time; interleaving them deadlocks/"database is locked").
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.other_plane = self.make_asset("N999")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.other_plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.day = date.today() - timedelta(days=3)
        conn = db.get_db()
        # The logged-in student's OWN past flight.
        self.sched = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student,
                              cfi_id=self.cfi, scheduled_date=self.day.isoformat(), scheduled_time="09:00",
                              duration_hours=1.5, status="completed", created_by="Front Desk")
        # A different student's past flight, same day, different plane so
        # the two are easy to tell apart in the HTML.
        self.other_student = seed_row(conn, "students", name="Other Student", active=1)
        self.other_sched = seed_row(conn, "scheduled_flights", asset_id=self.other_plane, student_id=self.other_student,
                                    cfi_id=self.cfi, scheduled_date=self.day.isoformat(), scheduled_time="11:00",
                                    duration_hours=1.5, status="completed", created_by="Front Desk")
        conn.commit()
        conn.close()

    def _student_sees_own_past_flight_in_full(self, html):
        self.assertIn('data-plane="N123"', html)
        self.assertIn(f'data-student-id="{self.student}"', html)
        self.assertIn("9:00 AM", html)

    def _student_never_sees_the_other_students_past_flight(self, html):
        self.assertNotIn('data-plane="N999"', html)
        self.assertNotIn(f'data-student-id="{self.other_student}"', html)
        self.assertNotIn("11:00 AM", html)

    def _cfi_still_sees_both(self, html):
        self.assertIn('data-plane="N123"', html)
        self.assertIn("9:00 AM", html)
        self.assertIn('data-plane="N999"', html)
        self.assertIn("11:00 AM", html)

    def test_day_view_keeps_a_students_own_past_flight_and_hides_the_other_students(self):
        html = self.login("flight_student").get(
            f"/flight/schedule?view=day&date={self.day.isoformat()}").get_data(as_text=True)
        self._student_sees_own_past_flight_in_full(html)
        self._student_never_sees_the_other_students_past_flight(html)

    def test_day_view_keeps_past_detail_for_a_cfi(self):
        html = self.login("cfi").get(
            f"/flight/schedule?view=day&date={self.day.isoformat()}").get_data(as_text=True)
        self._cfi_still_sees_both(html)

    def test_month_view_keeps_a_students_own_past_flight_and_hides_the_other_students(self):
        html = self.login("flight_student").get(
            f"/flight/schedule?view=month&year={self.day.year}&month={self.day.month}").get_data(as_text=True)
        self._student_sees_own_past_flight_in_full(html)
        self._student_never_sees_the_other_students_past_flight(html)

    def test_month_view_keeps_past_detail_for_a_cfi(self):
        html = self.login("cfi").get(
            f"/flight/schedule?view=month&year={self.day.year}&month={self.day.month}").get_data(as_text=True)
        self._cfi_still_sees_both(html)

    def test_week_view_keeps_a_students_own_past_flight_and_hides_the_other_students(self):
        html = self.login("flight_student").get(
            f"/flight/schedule?view=week&date={self.day.isoformat()}").get_data(as_text=True)
        self._student_sees_own_past_flight_in_full(html)
        self._student_never_sees_the_other_students_past_flight(html)

    def test_year_view_keeps_a_students_own_past_flight_and_hides_the_other_students(self):
        html = self.login("flight_student").get(
            f"/flight/schedule?view=year&year={self.day.year}").get_data(as_text=True)
        self._student_sees_own_past_flight_in_full(html)
        self._student_never_sees_the_other_students_past_flight(html)

    # The List view is a different, less-visited view (not named in idea
    # "past views") and keeps its older, simpler behavior: every past
    # flight - the student's own included - collapses to a plain "Past"
    # row for a student viewer.
    def test_list_view_hides_past_detail_from_a_student_including_their_own(self):
        html = self.login("flight_student").get(
            f"/flight/schedule?view=list&year={self.day.year}").get_data(as_text=True)
        self.assertIn("Past", html)
        self.assertNotIn('data-plane="N123"', html)
        self.assertNotIn('data-plane="N999"', html)
        self.assertNotIn("9:00 AM", html)
        self.assertNotIn("11:00 AM", html)

    def test_list_view_keeps_past_detail_for_a_cfi(self):
        html = self.login("cfi").get(
            f"/flight/schedule?view=list&year={self.day.year}").get_data(as_text=True)
        self._cfi_still_sees_both(html)

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
        self.assertIn('data-plane="N123"', html)
        self.assertIn("10:00 AM", html)
