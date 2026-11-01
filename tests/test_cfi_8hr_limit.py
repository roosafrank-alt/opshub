"""QA feat-cfi-8hr-limit: staff see an instructor's flying hours (last 24 hrs,
hours left, 8 max) and a heads-up when a flight would pass 8 hours; students
never see any of it; nothing is blocked."""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db
import flight


class Cfi8HourLimitTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N8HR")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id, name FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))
        self.day = (date.today() + timedelta(days=4)).isoformat()

    def _book(self, time, hours, status="scheduled", solo=0):
        conn = db.get_db()
        sid = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student, cfi_id=self.cfi["id"],
                       scheduled_date=self.day, scheduled_time=time, duration_hours=hours, status=status,
                       solo=solo, created_by="x")
        conn.commit()
        conn.close()
        return sid

    def _load(self, time="16:00", hours=1.5, exclude=None):
        conn = db.get_db()
        try:
            return flight._cfi_flying_load(conn, self.cfi["id"], self.day, time, hours, exclude_id=exclude)
        finally:
            conn.close()

    def test_counts_last_24_hours_and_hours_left(self):
        self._book("08:00", 3)
        self._book("11:30", 3.5)
        r = self._load()
        self.assertEqual(r["before"], 6.5)
        self.assertEqual(r["left"], 1.5)
        self.assertIn("6.5 hrs of flying in the last 24 hrs, 1.5 hrs left (8 max)", r["line"])
        self.assertFalse(r["over"])

    def test_over_and_near(self):
        self._book("08:00", 4)
        self._book("12:00", 3)
        self.assertTrue(self._load(hours=2)["over"])           # 7 + 2 = 9
        r = self._load(hours=0.5)                              # 7.5 total: next one would not fit
        self.assertFalse(r["over"])
        self.assertTrue(r["near"])

    def test_cancelled_solo_and_the_flight_itself_do_not_count(self):
        self._book("08:00", 4, status="cancelled")
        self._book("09:00", 4, solo=1)
        mine = self._book("10:00", 2)
        self.assertEqual(self._load(time="12:00", exclude=mine)["before"], 0)

    def test_real_billed_time_replaces_planned_time(self):
        sid = self._book("08:00", 3)
        conn = db.get_db()
        seed_row(conn, "flights", asset_id=self.plane, student_id=self.student, cfi_id=self.cfi["id"],
                 scheduled_flight_id=sid, flight_date=self.day, hobbs_start=100.0, hobbs_end=101.0)
        conn.commit()
        conn.close()
        self.assertEqual(self._load()["before"], 1.0)

    def test_staff_endpoint_works_and_student_gets_nothing(self):
        self._book("08:00", 3)
        url = f"/flight/schedule/cfi-hours?cfi_id={self.cfi['id']}&date={self.day}&time=16:00&hours=1.5"
        data = self.login("cfi").get(url).get_json()
        self.assertIn("3 hrs of flying", data["line"])
        r = self.login("flight_student").get(url)
        self.assertNotEqual(r.status_code, 200)
        self.assertNotIn("hrs of flying", r.get_data(as_text=True))

    def test_students_form_has_no_hours_ui_and_staff_form_has_it(self):
        self._book("08:00", 3)
        student_html = self.login("flight_student").get("/flight/schedule/new").get_data(as_text=True)
        self.assertNotIn("cfi-hours", student_html)
        self.assertNotIn("Book anyway", student_html)
        staff_html = self.login("cfi").get("/flight/schedule/new").get_data(as_text=True)
        self.assertIn("cfi-hours-line", staff_html)
        self.assertIn("Book anyway", staff_html)
        self.assertIn("Pick another instructor", staff_html)

    def test_pending_request_warning_only_for_staff(self):
        self._book("08:00", 4)
        self._book("12:00", 3.5)
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student, cfi_id=self.cfi["id"],
                 scheduled_date=self.day, scheduled_time="16:30", duration_hours=1.5, status="pending_approval", created_by="x")
        conn.commit()
        conn.close()
        self.assertIn("would be over 8 hours of flying", self.login("cfi").get("/flight/dashboard").get_data(as_text=True))
        sh = self.login("flight_student").get("/flight/dashboard").get_data(as_text=True)
        self.assertNotIn("would be over", sh)
        self.assertNotIn("hrs of flying", sh)
