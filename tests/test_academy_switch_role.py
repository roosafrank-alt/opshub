"""Flight Academy "Switch role": a Flight Admin or master admin can pick one
particular student and look at Flight Academy as that student (look only),
with a banner and a way back. Students and CFIs can't use it."""
from harness import OpsHubTestCase
import db


class AcademySwitchRole(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.other = self.exec("INSERT INTO students (name, username, password_hash, active) VALUES ('Zed Pilot', 'zedp', 'x', 1)")
        self.gone = self.exec("INSERT INTO students (name, username, password_hash, active) VALUES ('Old Timer', 'oldt', 'x', 0)")

    def _sess(self, c):
        with c.session_transaction() as s:
            return dict(s)

    def test_chip_shows_on_academy_for_admins_only(self):
        for who in ("master", "flight_admin"):
            html = self.login(who).get("/academy").get_data(as_text=True)
            self.assertIn("Switch role:", html, who)
            self.assertIn("/view-as/flight/student?next=", html, who)
        for who in ("cfi", "flight_student"):
            html = self.login(who).get("/academy").get_data(as_text=True)
            self.assertNotIn("/view-as/flight/student", html, who)

    def test_picker_lists_active_students_for_admins(self):
        for who in ("master", "flight_admin"):
            r = self.login(who).get("/view-as/flight/student")
            self.assertEqual(r.status_code, 200)
            html = r.get_data(as_text=True)
            self.assertIn("Zed Pilot", html)
            self.assertNotIn("Old Timer", html)

    def test_cfi_and_student_cannot_use_it(self):
        for who in ("cfi", "flight_student", "tech"):
            c = self.login(who)
            self.assertEqual(c.get("/view-as/flight/student").status_code, 403, who)
            c.post(f"/view-as/flight/student/{self.other}")
            self.assertNotIn("_view_as_real", self._sess(c), who)

    def test_admin_sees_only_that_student_with_banner_and_look_only(self):
        c = self.login("flight_admin")
        r = c.post(f"/view-as/flight/student/{self.other}", data={"next": "/flight/students"})
        self.assertEqual(r.status_code, 302)
        s = self._sess(c)
        self.assertEqual(s["student_id"], self.other)
        self.assertFalse(s["is_master_admin"])
        self.assertNotEqual(s.get("flight_role"), "admin")
        html = c.get("/academy").get_data(as_text=True)
        self.assertIn("Viewing as Student (Zed Pilot)", html)
        self.assertIn("Back to my normal view", html)
        # look only: logging flying as them is refused
        c.post("/academy/entry", data={"student_id": self.other, "kind": "landings", "value": "3"})
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM academy_entries")["n"], 0)
        # a student can't be pointed at another student by URL
        r = c.get("/academy?student_id=1")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Flight Student</option>", r.get_data(as_text=True))

    def test_back_returns_to_the_page_they_were_on(self):
        c = self.login("master")
        c.post(f"/view-as/flight/student/{self.other}", data={"next": "/flight/students"})
        r = c.post("/view-as/exit")
        self.assertTrue(r.headers["Location"].endswith("/flight/students"))
        s = self._sess(c)
        self.assertTrue(s["is_master_admin"])
        self.assertNotIn("_view_as_readonly", s)
        self.assertNotIn("_view_as_real", s)
        c.post("/academy/entry", data={"student_id": self.other, "kind": "landings", "value": "2"})
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM academy_entries")["n"], 1)

    def test_inactive_student_refused(self):
        c = self.login("master")
        c.post(f"/view-as/flight/student/{self.gone}")
        self.assertNotIn("_view_as_real", self._sess(c))

    def test_existing_role_switch_still_works(self):
        c = self.login("master")
        c.post("/view-as/flight/cfi")
        self.assertEqual(self._sess(c)["flight_role"], "cfi")
        self.assertNotIn("_view_as_readonly", self._sess(c))
        c.post("/view-as/exit")
        c = self.login("master")
        c.post(f"/customer/view-as/{self.customer_id}")
