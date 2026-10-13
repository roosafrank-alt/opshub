"""Account admin page: enroll or unenroll ONE Ground School course at a time."""
from harness import OpsHubTestCase
import db


class AdminAccountCourseGrantTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.uid = self.users["flight_student"]["id"]
        self.exec("UPDATE users SET academy_access = 1 WHERE id = ?", (self.uid,))
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

    def test_no_all_access_checkbox_and_legacy_flag_kept_then_cleared(self):
        self.exec("UPDATE users SET groundschool_access = 1 WHERE id = ?", (self.uid,))
        c = self.login("master")
        body = c.get(f"/admin/users/{self.uid}/edit").get_data(as_text=True)
        self.assertNotIn('name="groundschool_access"', body)
        self.assertIn("Switch to enrollment", body)
        # saving the form must not wipe the older flag
        u = self.q1("SELECT * FROM users WHERE id = ?", (self.uid,))
        c.post(f"/admin/users/{self.uid}/edit", data={"name": u["name"], "username": u["username"], "active": "on",
                                                     "flight_roles": "student"})
        self.assertEqual(self.q1("SELECT groundschool_access AS g FROM users WHERE id = ?", (self.uid,))["g"], 1)
        c.post(f"/admin/users/{self.uid}/course-grant", data={"action": "clear_full"})
        self.assertEqual(self.q1("SELECT groundschool_access AS g FROM users WHERE id = ?", (self.uid,))["g"], 0)

    def test_enroll_list_visible_even_with_academy_access(self):
        self.exec("UPDATE users SET academy_access = 1 WHERE id = ?", (self.uid,))
        body = self.login("master").get(f"/admin/users/{self.uid}/edit").get_data(as_text=True)
        self.assertIn("Private Pilot", body)
        self.assertIn('id="courses-list"', body)
        self.assertNotIn('id="courses-list" class="d-none"', body)

    def test_enroll_buttons_and_academy_note_are_in_the_page(self):
        body = self.login("master").get(f"/admin/users/{self.uid}/edit").get_data(as_text=True)
        self.assertIn("js-enroll", body)
        self.assertIn("Tick <strong>Flight Academy Access</strong>", body)
        self.assertNotIn("sees every ground school whatever", body)

    def test_academy_access_student_sees_only_enrolled_courses(self):
        self.exec("UPDATE users SET academy_access = 1 WHERE id = ?", (self.uid,))
        self.exec("INSERT INTO academy_course_grants (student_id, rating_id, granted_by, granted_at) VALUES (?,?,?,?)",
                  (self.sid, self.r["ppl"], "test", db.now_iso()))
        body = self.login("flight_student").get("/flight/groundschool").get_data(as_text=True)
        self.assertIn(f'/flight/groundschool/{self.r["ppl"]}"', body)
        self.assertNotIn(f'/flight/groundschool/{self.r["ifr"]}"', body)

    def test_cannot_enroll_without_flight_academy_access(self):
        self.exec("UPDATE users SET academy_access = 0 WHERE id = ?", (self.uid,))
        self.login("master").post(f"/admin/users/{self.uid}/course-grant", data={"rating_id": self.r["ppl"], "action": "enroll"})
        self.assertEqual(self.granted(), set())
