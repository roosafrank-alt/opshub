"""Account admin page: a master admin picks which Ground School courses a
student account can read (same grants as the student page)."""
from harness import OpsHubTestCase
import db


class AccountCourseGrantsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.users["flight_student"]
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.user["id"],))["id"]
        conn = db.get_db()
        self.rid = {}
        for slug, name, status in (("ppl", "Private Pilot", "parsed"), ("ifr", "Instrument", "parsed"),
                                   ("raw", "Unparsed Thing", "empty")):
            self.rid[slug] = conn.execute("INSERT INTO acs_ratings (name, slug, uploaded_at, status) VALUES (?,?,?,?)",
                                          (name, slug, db.now_iso(), status)).lastrowid
        conn.commit()
        conn.close()

    def url(self):
        return f"/admin/users/{self.user['id']}/course-grants"

    def test_section_lists_only_ready_courses(self):
        body = self.login("master").get(f"/admin/users/{self.user['id']}/edit").get_data(as_text=True)
        self.assertIn("Ground School courses", body)
        self.assertIn("Private Pilot", body)
        self.assertNotIn("Unparsed Thing", body)

    def test_grant_and_remove(self):
        c = self.login("master")
        r = c.post(self.url(), data={"rating_id": [self.rid["ppl"], self.rid["ifr"]]}, follow_redirects=True)
        self.assertIn("Granted: Instrument, Private Pilot", r.get_data(as_text=True))
        r = c.post(self.url(), data={"rating_id": [self.rid["ppl"]]}, follow_redirects=True)
        self.assertIn("Removed: Instrument", r.get_data(as_text=True))
        rows = self.q("SELECT student_id, rating_id FROM academy_course_grants")
        self.assertEqual([(r["student_id"], r["rating_id"]) for r in rows], [(self.sid, self.rid["ppl"])])

    def test_unparsed_course_not_grantable(self):
        self.login("master").post(self.url(), data={"rating_id": [self.rid["raw"]]})
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM academy_course_grants")[0]["c"], 0)

    def test_non_master_cannot_change(self):
        for who in ("cfi", "flight_student"):
            self.login(who).post(self.url(), data={"rating_id": [self.rid["ppl"]]})
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM academy_course_grants")[0]["c"], 0)

    def test_account_without_student_profile(self):
        other = self.users["cfi"]
        body = self.login("master").get(f"/admin/users/{other['id']}/edit").get_data(as_text=True)
        self.assertIn("no student profile", body)
        self.login("master").post(f"/admin/users/{other['id']}/course-grants", data={"rating_id": [self.rid["ppl"]]})
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM academy_course_grants")[0]["c"], 0)
