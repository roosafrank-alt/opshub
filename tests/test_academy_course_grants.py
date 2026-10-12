"""A lesson plan is a whole course: an admin grants a student whole Ground
School courses (acs_ratings), and the student reads them, read only, under
Flight Academy - nothing else there."""
from harness import OpsHubTestCase
import db

GS = "/flight/groundschool"


class AcademyCourseGrantsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        conn = db.get_db()
        self.course = {}
        for slug, name, status in (("ppl", "Private Pilot", "parsed"), ("ifr", "Instrument", "parsed"),
                                   ("raw", "Unparsed Thing", "empty")):
            rid = conn.execute("INSERT INTO acs_ratings (name, slug, uploaded_at, status) VALUES (?,?,?,?)",
                               (name, slug, db.now_iso(), status)).lastrowid
            area = conn.execute("INSERT INTO acs_areas (rating_id, code, title, order_index) VALUES (?,?,?,1)",
                                (rid, "I", f"{name} Area", )).lastrowid
            task = conn.execute("INSERT INTO acs_tasks (area_id, code, title, acs_references, order_index) "
                                "VALUES (?,?,?,?,1)", (area, "A", f"{name} Task", "14 CFR 61.83")).lastrowid
            el = conn.execute("INSERT INTO acs_task_elements (task_id, kind, code, text, order_index) "
                              "VALUES (?,?,?,?,1)", (task, "knowledge", "K1", f"{name} element")).lastrowid
            item = conn.execute("INSERT INTO acs_element_lesson_items (element_id, order_index, kind, title, required) "
                                "VALUES (?,1,'link','Read it',1)", (el,)).lastrowid
            self.course[slug] = dict(rating=rid, task=task, el=el, item=item)
        conn.commit()
        conn.close()

    def grant(self, slug="ppl"):
        self.exec("INSERT INTO academy_course_grants (student_id, rating_id, granted_by, granted_at) VALUES (?,?,?,?)",
                  (self.sid, self.course[slug]["rating"], "test", db.now_iso()))

    # ----- migration / admin UI -------------------------------------------
    def test_table_exists_with_expected_columns(self):
        cols = [r["name"] for r in self.q("PRAGMA table_info(academy_course_grants)")]
        self.assertEqual(cols, ["student_id", "rating_id", "granted_by", "granted_at"])
        db.init_db()  # safe to run again on an existing database
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM academy_course_grants")[0]["c"], 0)

    def test_admin_sees_section_with_only_ready_courses(self):
        body = self.login("master").get(f"/flight/students/{self.sid}/edit").get_data(as_text=True)
        self.assertIn("Flight Academy courses", body)
        self.assertIn("Private Pilot", body)
        self.assertNotIn("Unparsed Thing", body)
        self.assertIn("read only", body)

    def test_cfi_does_not_see_section(self):
        body = self.login("cfi").get(f"/flight/students/{self.sid}/edit").get_data(as_text=True)
        self.assertNotIn("Flight Academy courses", body)

    def test_admin_grants_and_removes_with_message(self):
        c = self.login("master")
        r = c.post(f"/flight/students/{self.sid}/course-grants",
                   data={"rating_id": [self.course["ppl"]["rating"], self.course["ifr"]["rating"]]}, follow_redirects=True)
        self.assertIn("Granted: Instrument, Private Pilot", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM academy_course_grants")[0]["c"], 2)
        r = c.post(f"/flight/students/{self.sid}/course-grants",
                   data={"rating_id": [self.course["ppl"]["rating"]]}, follow_redirects=True)
        self.assertIn("Removed: Instrument", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT rating_id FROM academy_course_grants")[0]["rating_id"], self.course["ppl"]["rating"])

    def test_cannot_grant_unparsed_course(self):
        self.login("master").post(f"/flight/students/{self.sid}/course-grants",
                                  data={"rating_id": [self.course["raw"]["rating"]]})
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM academy_course_grants")[0]["c"], 0)

    def test_cfi_and_student_cannot_change_grants(self):
        for who in ("cfi", "cfi_billing", "flight_student"):
            self.login(who).post(f"/flight/students/{self.sid}/course-grants",
                                 data={"rating_id": [self.course["ppl"]["rating"]]})
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM academy_course_grants")[0]["c"], 0)

    # ----- student with a grant -------------------------------------------
    def test_granted_student_sees_tile_and_lands_in_ground_school(self):
        self.grant()
        c = self.login("flight_student")
        r = c.get("/academy")
        self.assertEqual(r.status_code, 302)
        self.assertIn(GS, r.headers["Location"])
        body = c.get("/").get_data(as_text=True)
        self.assertIn("Flight Academy", body)

    def test_student_without_grant_has_no_academy(self):
        c = self.login("flight_student")
        r = c.get("/academy", follow_redirects=True)
        self.assertIn("don&#39;t have access to Flight Academy", r.get_data(as_text=True))
        r = c.get(GS, follow_redirects=True)
        self.assertIn("don&#39;t have access to Ground School", r.get_data(as_text=True))

    def test_granted_student_sees_only_granted_course_and_only_that_tab(self):
        self.grant()
        c = self.login("flight_student")
        body = c.get(GS).get_data(as_text=True)
        self.assertIn("Private Pilot", body)
        self.assertNotIn("Instrument", body)
        self.assertNotIn("Leaderboard", body)
        self.assertNotIn("Resources", body)
        self.assertNotIn("Logbook", body)
        self.assertEqual(c.get(f"{GS}/{self.course['ppl']['rating']}").status_code, 200)
        self.assertEqual(c.get(f"{GS}/task/{self.course['ppl']['task']}").status_code, 200)
        self.assertEqual(c.get(f"{GS}/element/{self.course['ppl']['el']}").status_code, 200)

    def test_non_granted_course_task_element_refused_by_url(self):
        self.grant()
        c = self.login("flight_student")
        other = self.course["ifr"]
        for url in (f"{GS}/{other['rating']}", f"{GS}/task/{other['task']}", f"{GS}/element/{other['el']}"):
            r = c.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertTrue(r.headers["Location"].endswith(GS), url)
        r = c.post(f"{GS}/task/{other['task']}/reference-click")
        self.assertEqual(r.status_code, 404)
        c.post(f"{GS}/task/{other['task']}/mark-read")
        r = c.post(f"{GS}/element/{other['el']}/item/{other['item']}/interact")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM acs_element_item_progress")[0]["c"], 0)
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM acs_task_reading_progress")[0]["c"], 0)

    def test_granted_student_reading_actions_work(self):
        self.grant()
        c = self.login("flight_student")
        mine = self.course["ppl"]
        self.assertEqual(c.post(f"{GS}/task/{mine['task']}/reference-click").status_code, 200)
        c.post(f"{GS}/task/{mine['task']}/mark-read")
        self.assertIsNotNone(self.q1("SELECT marked_read_at FROM acs_task_reading_progress WHERE student_id = ?",
                                     (self.sid,))["marked_read_at"])
        r = c.post(f"{GS}/element/{mine['el']}/item/{mine['item']}/interact")
        self.assertEqual(r.status_code, 200)

    def test_granted_student_cannot_signoff_edit_upload_or_open_library(self):
        self.grant()
        c = self.login("flight_student")
        mine = self.course["ppl"]
        before = self.q1("SELECT COUNT(*) AS c FROM acs_task_signoff")["c"]
        c.post(f"{GS}/task/{mine['task']}/signoff", data={"student_id": self.sid})
        c.post(f"{GS}/task/{mine['task']}/lesson", data={"content": "hacked"})
        c.post(f"{GS}/task/{mine['task']}/verify-reading", data={"student_id": self.sid})
        c.post(f"{GS}/element/{mine['el']}/verify", data={"student_id": self.sid})
        c.post(f"{GS}/element/{mine['el']}/item/add", data={"title": "x"})
        c.post(f"{GS}/{mine['rating']}/delete")
        c.post(f"{GS}/{mine['rating']}/reparse")
        c.post(f"{GS}/upload", data={"name": "x"})
        c.post(f"{GS}/resources/upload", data={"title": "x"})
        self.assertEqual(self.q1("SELECT COUNT(*) AS c FROM acs_task_signoff")["c"], before)
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM acs_lesson_content")[0]["c"], 0)
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM acs_ratings")[0]["c"], 3)
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM acs_element_completion")[0]["c"], 0)
        r = c.get(f"{GS}/resources")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith(GS))

    def test_resource_not_cited_by_granted_course_refused(self):
        self.grant()
        self.exec("INSERT INTO acs_resources (title, filename, doc_type, uploaded_at) VALUES ('Other', 'x.pdf', 'handbook', ?)",
                  (db.now_iso(),))
        rid = self.q1("SELECT id FROM acs_resources")["id"]
        r = self.login("flight_student").get(f"{GS}/resources/{rid}/file")
        self.assertEqual(r.status_code, 302)

    def test_removing_grant_takes_effect_immediately(self):
        self.grant()
        c = self.login("flight_student")
        self.assertEqual(c.get(f"{GS}/{self.course['ppl']['rating']}").status_code, 200)
        self.exec("DELETE FROM academy_course_grants")
        self.assertEqual(c.get(f"{GS}/{self.course['ppl']['rating']}").status_code, 302)

    # ----- unchanged behaviour --------------------------------------------
    def test_student_with_academy_access_unchanged(self):
        self.exec("UPDATE users SET academy_access = 1 WHERE username = 'flight_student'")
        self.grant()
        c = self.login("flight_student")
        self.assertEqual(c.get("/academy").status_code, 200)
        body = c.get(GS).get_data(as_text=True)
        self.assertIn("Instrument", body)
        self.assertEqual(c.get(f"{GS}/resources").status_code, 200)

    def test_student_with_groundschool_access_sees_everything(self):
        self.exec("UPDATE users SET groundschool_access = 1 WHERE username = 'flight_student'")
        self.grant()
        c = self.login("flight_student")
        self.assertIn("Instrument", c.get(GS).get_data(as_text=True))
        self.assertEqual(c.get(f"{GS}/task/{self.course['ifr']['task']}").status_code, 200)

    def test_staff_unchanged(self):
        self.grant()
        for who in ("master", "cfi"):
            if who == "cfi":
                self.exec("UPDATE users SET groundschool_access = 1 WHERE username = 'cfi'")
            c = self.login(who)
            self.assertIn("Instrument", c.get(GS).get_data(as_text=True))
            self.assertEqual(c.get(f"{GS}/task/{self.course['ifr']['task']}").status_code, 200)
            self.assertEqual(c.get(f"{GS}/resources").status_code, 200)
