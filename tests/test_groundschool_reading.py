"""Idea "ground school." revision 3: Mark as Read is now a checkbox for the
student, and once checked it unlocks a Reading Review checkbox for the CFI
to confirm they went over the knowledge area with the student."""
from harness import OpsHubTestCase
import db


class GroundschoolReadingTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.exec("UPDATE users SET groundschool_access = 1 WHERE username IN ('flight_student', 'cfi')")
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?",
                                  (self.users["flight_student"]["id"],))["id"]
        conn = db.get_db()
        cur = conn.execute("INSERT INTO acs_ratings (name, slug, uploaded_at, status) VALUES (?,?,?,?)",
                          ("Private Pilot", "ppl", db.now_iso(), "ready"))
        rating_id = cur.lastrowid
        cur = conn.execute("INSERT INTO acs_areas (rating_id, code, title, order_index) VALUES (?,?,?,?)",
                          (rating_id, "I", "Preflight Preparation", 1))
        area_id = cur.lastrowid
        cur = conn.execute("INSERT INTO acs_tasks (area_id, code, title, acs_references, order_index) "
                           "VALUES (?,?,?,?,?)",
                           (area_id, "A", "Pilot Qualifications", "14 CFR 61.83", 1))
        self.task_id = cur.lastrowid
        conn.commit()
        conn.close()

    def test_mark_read_requires_opening_a_reference_first(self):
        c = self.login("flight_student")
        c.post(f"/flight/groundschool/task/{self.task_id}/mark-read", follow_redirects=True)
        row = self.q1("SELECT * FROM acs_task_reading_progress WHERE student_id = ? AND task_id = ?",
                      (self.student_id, self.task_id))
        self.assertIsNone(row)

    def test_student_can_mark_read_after_opening_reference(self):
        c = self.login("flight_student")
        c.post(f"/flight/groundschool/task/{self.task_id}/reference-click")
        c.post(f"/flight/groundschool/task/{self.task_id}/mark-read", follow_redirects=True)
        row = self.q1("SELECT * FROM acs_task_reading_progress WHERE student_id = ? AND task_id = ?",
                      (self.student_id, self.task_id))
        self.assertIsNotNone(row["marked_read_at"])
        self.assertIsNone(row["cfi_verified_at"])

    def test_cfi_cannot_verify_before_student_marks_read(self):
        c = self.login("cfi")
        c.post(f"/flight/groundschool/task/{self.task_id}/verify-reading",
              data={"student_id": self.student_id}, follow_redirects=True)
        row = self.q1("SELECT * FROM acs_task_reading_progress WHERE student_id = ? AND task_id = ?",
                      (self.student_id, self.task_id))
        self.assertIsNone(row)

    def test_cfi_can_verify_after_student_marks_read(self):
        student_client = self.login("flight_student")
        student_client.post(f"/flight/groundschool/task/{self.task_id}/reference-click")
        student_client.post(f"/flight/groundschool/task/{self.task_id}/mark-read")

        cfi_client = self.login("cfi")
        r = cfi_client.post(f"/flight/groundschool/task/{self.task_id}/verify-reading",
                            data={"student_id": self.student_id}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        row = self.q1("SELECT * FROM acs_task_reading_progress WHERE student_id = ? AND task_id = ?",
                      (self.student_id, self.task_id))
        self.assertIsNotNone(row["cfi_verified_at"])
        cfi_row = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))
        self.assertEqual(row["cfi_id"], cfi_row["id"])

    def test_task_page_shows_checkbox_and_review_box(self):
        student_client = self.login("flight_student")
        student_client.post(f"/flight/groundschool/task/{self.task_id}/reference-click")
        student_client.post(f"/flight/groundschool/task/{self.task_id}/mark-read")

        cfi_html = self.login("cfi").get(
            f"/flight/groundschool/task/{self.task_id}?student_id={self.student_id}").get_data(as_text=True)
        self.assertIn("Reading Review", cfi_html)
        self.assertIn("verify-reading-checkbox", cfi_html)
