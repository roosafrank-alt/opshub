"""QA fix ux-gs-continue-lesson: a plain student lands on the Fly with Kate!
dashboard at login even when enrolled in a Ground School course, the dashboard
has a Continue row into the next unfinished task, and the Ground School page
gets a Continue button per course."""
from harness import OpsHubTestCase, PASSWORD
import db


class GsContinueLessonTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?",
                           (self.users["flight_student"]["id"],))["id"]
        conn = db.get_db()
        rid = conn.execute("INSERT INTO acs_ratings (name, slug, uploaded_at, status) VALUES (?,?,?,?)",
                           ("Private Pilot", "ppl", db.now_iso(), "parsed")).lastrowid
        aid = conn.execute("INSERT INTO acs_areas (rating_id, code, title, order_index) VALUES (?,?,?,?)",
                           (rid, "I", "Preflight", 1)).lastrowid
        self.tasks, self.els = [], []
        for i, (code, title) in enumerate([("A", "Pilot Qualifications"), ("B", "Airworthiness")], 1):
            tid = conn.execute("INSERT INTO acs_tasks (area_id, code, title, order_index) VALUES (?,?,?,?)",
                               (aid, code, title, i)).lastrowid
            eid = conn.execute("INSERT INTO acs_task_elements (task_id, kind, code, text, order_index) "
                               "VALUES (?,?,?,?,?)", (tid, "knowledge", f"PA.I.{code}.K1", f"Element {code}", 1)).lastrowid
            self.tasks.append(tid)
            self.els.append(eid)
        conn.execute("INSERT INTO academy_course_grants (student_id, rating_id, granted_at) VALUES (?,?,?)", (self.sid, rid, db.now_iso()))
        conn.commit()
        conn.close()

    def _login(self, username):
        c = self.app.test_client()
        return c, c.post("/", data={"username": username, "password": PASSWORD})

    def test_enrolled_student_login_skips_the_picker(self):
        c, r = self._login("flight_student")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/flight/dashboard", r.headers["Location"])
        self.assertIn("Choose a program", c.get("/").get_data(as_text=True))

    def test_dashboard_continue_row_points_at_next_unfinished_task(self):
        self.exec("INSERT INTO acs_element_completion (student_id, element_id, self_completed_at) VALUES (?,?,?)",
                  (self.sid, self.els[0], db.now_iso()))
        c = self.login("flight_student")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Continue: Task B. Airworthiness", body)
        self.assertIn("1 of 2 done", body)
        self.assertIn(f"/flight/groundschool/task/{self.tasks[1]}", body)

    def test_course_card_has_continue_and_all_tasks(self):
        c = self.login("flight_student")
        body = c.get("/flight/groundschool").get_data(as_text=True)
        self.assertIn("Continue", body)
        self.assertIn("next up: PA.I.A.K1, Element A", body)
        self.assertIn("All tasks", body)
