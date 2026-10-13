"""Account admin page: enroll or unenroll ONE Ground School course at a time."""
from harness import OpsHubTestCase
import db


class AdminAccountCourseGrantTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.uid = self.users["flight_student"]["id"]
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.uid,))["id"]
        conn = db.get_db()
        self.r = {}
        for slug, name, status in (("ppl", "Private Pilot", "parsed"), ("ifr", "Instrument", "parsed"),
                                   ("raw", "Unparsed Thing", "empty")):
            self.r[slug] = conn.execute("INSERT INTO acs_ratings (name, slug, uploaded_at, status) VALUES (?,?,?,?)",
                                        (name, slug, db.now_iso(), status)).lastrowid
        conn.commit()
        conn.close()

    def granted(self):
        return {x["rating_id"] for x in self.q("SELECT rating_id FROM academy_course_grants WHERE student_id = ?", (self.sid,))}

    def test_card_lists_only_ready_courses(self):
        body = self.login("master").get(f"/admin/users/{self.uid}/edit").get_data(as_text=True)
        self.assertIn("Enroll in individual ground schools", body)
        self.assertLess(body.index("Enroll in individual ground schools"), body.index("Profiles") if "Profiles" in body else 10**9)
        self.assertIn('data-confirm="Enroll', body)
        self.assertIn("Private Pilot", body)
        self.assertNotIn("Unparsed Thing", body)

    def test_grant_one_then_remove(self):
        c = self.login("master")
        c.post(f"/admin/users/{self.uid}/course-grant", data={"rating_id": self.r["ppl"], "action": "enroll"})
        self.assertEqual(self.granted(), {self.r["ppl"]})
        c.post(f"/admin/users/{self.uid}/course-grant", data={"rating_id": self.r["ifr"], "action": "enroll"})
        self.assertEqual(self.granted(), {self.r["ppl"], self.r["ifr"]})
        c.post(f"/admin/users/{self.uid}/course-grant", data={"rating_id": self.r["ppl"], "action": "unenroll"})
        self.assertEqual(self.granted(), {self.r["ifr"]})

    def test_unready_course_refused(self):
        self.login("master").post(f"/admin/users/{self.uid}/course-grant", data={"rating_id": self.r["raw"], "action": "enroll"})
        self.assertEqual(self.granted(), set())

    def test_non_admin_refused(self):
        self.login("cfi").post(f"/admin/users/{self.uid}/course-grant", data={"rating_id": self.r["ppl"], "action": "enroll"})
        self.assertEqual(self.granted(), set())
