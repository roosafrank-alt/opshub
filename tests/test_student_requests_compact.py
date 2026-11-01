"""QA ux-student-requests-compact: on a phone a student's 'Your Requests'
shows one line per request ('Sun, Nov 1 · 4:00 PM · N123' + status tag)."""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class StudentRequestsCompactTest(OpsHubTestCase):
    def test_phone_list_has_one_line_per_request(self):
        plane = self.make_asset("N2231Q")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (plane,))
        student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        day = date.today() + timedelta(days=2)
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=plane, student_id=student, scheduled_date=day.isoformat(),
                 scheduled_time="16:00", duration_hours=1, status="pending_approval", created_by="x")
        conn.commit()
        conn.close()
        html = self.login("flight_student").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("ux-student-requests-compact", html)
        self.assertIn(day.strftime("%b ") + str(day.day) + " &middot; 4:00 PM &middot; N2231Q", html)
        self.assertIn(">Waiting<", html)
