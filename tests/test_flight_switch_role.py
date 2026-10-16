"""Fly with Kate! "Switch role": a Flight Admin or master admin can pick one
particular student or CFI and look at the Fly with Kate! pages as that person
(look only), with the yellow banner and a way back. Students and CFIs can't."""
from harness import OpsHubTestCase


class FlightSwitchRole(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.stu = self.exec("INSERT INTO students (name, username, password_hash, active) VALUES ('Zed Pilot', 'zedp', 'x', 1)")
        self.cfi2 = self.exec("INSERT INTO cfis (name, username, password_hash, active) VALUES ('Quinn Instructor', 'quinni', 'x', 1)")
        self.gone = self.exec("INSERT INTO cfis (name, username, password_hash, active) VALUES ('Retired CFI', 'retc', 'x', 0)")

    def _sess(self, c):
        with c.session_transaction() as s:
            return dict(s)

    def test_cfi_chip_picks_a_person_on_flight_pages(self):
        for who in ("master", "flight_admin"):
            html = self.login(who).get("/flight/dashboard").get_data(as_text=True)
            self.assertIn("/view-as/flight/cfi?next=", html, who)
            self.assertIn("/view-as/flight/student?next=", html, who)
        for who in ("cfi", "flight_student"):
            html = self.login(who).get("/flight/dashboard").get_data(as_text=True)
            self.assertNotIn("/view-as/flight/cfi", html, who)
            self.assertNotIn("/view-as/flight/student", html, who)

    def test_cfi_picker_lists_active_cfis_for_admins(self):
        for who in ("master", "flight_admin"):
            r = self.login(who).get("/view-as/flight/cfi")
            self.assertEqual(r.status_code, 200, who)
            html = r.get_data(as_text=True)
            self.assertIn("Quinn Instructor", html)
            self.assertNotIn("Retired CFI", html)
            self.assertNotIn("Zed Pilot", html)

    def test_cfi_and_student_cannot_use_it(self):
        for who in ("cfi", "flight_student", "tech"):
            c = self.login(who)
            self.assertEqual(c.get("/view-as/flight/cfi").status_code, 403, who)
            c.post(f"/view-as/flight/cfi/{self.cfi2}")
            self.assertNotIn("_view_as_real", self._sess(c), who)

    def test_admin_sees_flight_pages_as_the_picked_cfi_look_only(self):
        for who in ("master", "flight_admin"):
            c = self.login(who)
            r = c.post(f"/view-as/flight/cfi/{self.cfi2}", data={"next": "/flight/students"})
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith("/flight/dashboard"), who)
            s = self._sess(c)
            self.assertEqual((s["cfi_id"], s["flight_role"], s["is_master_admin"]), (self.cfi2, "cfi", False), who)
            self.assertTrue(s["_view_as_readonly"])
            html = c.get("/flight/dashboard").get_data(as_text=True)
            self.assertIn("Viewing as CFI (Quinn Instructor)", html)
            self.assertIn("Back to my normal view", html)
            # admin-only power is gone
            self.assertIn(c.get("/flight/settings/school").status_code, (302, 403))
            # look only: a write is refused and changes nothing
            self.assert_write_blocked(c)

    def test_admin_sees_flight_pages_as_the_picked_student_look_only(self):
        c = self.login("flight_admin")
        r = c.post(f"/view-as/flight/student/{self.stu}", data={"next": "/flight/students"})
        self.assertTrue(r.headers["Location"].endswith("/flight/dashboard"))
        s = self._sess(c)
        self.assertEqual((s["student_id"], s["flight_role"]), (self.stu, "student"))
        html = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Viewing as Student (Zed Pilot)", html)
        self.assertIn(c.get("/flight/students").status_code, (302, 403))
        self.assert_write_blocked(c)

    def test_admin_picking_their_own_student_profile_can_confirm_their_flight(self):
        """An admin who is also a student (own students row linked to their login)
        is not 'someone else': picking themselves under Switch role is NOT look
        only, so they can OK a flight a CFI booked for them."""
        from datetime import date, timedelta
        import db
        from harness import seed_row
        admin = self.users["flight_admin"]
        mine = self.exec("INSERT INTO students (name, username, password_hash, active, user_id) VALUES ('Admin As Student', 'adminstu', 'x', 1, ?)",
                         (admin["id"],))
        plane = self.make_asset("N555")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (plane,))
        cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        conn = db.get_db()
        sid = seed_row(conn, "scheduled_flights", asset_id=plane, student_id=mine, cfi_id=cfi,
                       scheduled_date=(date.today() + timedelta(days=2)).isoformat(), scheduled_time="09:00",
                       duration_hours="1.0", status="scheduled", confirm_required=1, confirmed_at=None, created_by="CFI")
        conn.commit()
        conn.close()
        c = self.login("flight_admin")
        c.post(f"/view-as/flight/student/{mine}")
        s = self._sess(c)
        self.assertEqual((s["student_id"], s["flight_role"]), (mine, "student"))
        self.assertNotIn("_view_as_readonly", s)
        c.post(f"/flight/schedule/{sid}/confirm")
        self.assertIsNotNone(self.q1("SELECT confirmed_at FROM scheduled_flights WHERE id = ?", (sid,))["confirmed_at"])

    def test_picking_someone_elses_student_profile_still_cannot_confirm(self):
        from datetime import date, timedelta
        import db
        from harness import seed_row
        plane = self.make_asset("N556")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (plane,))
        cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        conn = db.get_db()
        sid = seed_row(conn, "scheduled_flights", asset_id=plane, student_id=self.stu, cfi_id=cfi,
                       scheduled_date=(date.today() + timedelta(days=2)).isoformat(), scheduled_time="10:00",
                       duration_hours="1.0", status="scheduled", confirm_required=1, confirmed_at=None, created_by="CFI")
        conn.commit()
        conn.close()
        c = self.login("flight_admin")
        c.post(f"/view-as/flight/student/{self.stu}")
        self.assertTrue(self._sess(c)["_view_as_readonly"])
        c.post(f"/flight/schedule/{sid}/confirm")
        self.assertIsNone(self.q1("SELECT confirmed_at FROM scheduled_flights WHERE id = ?", (sid,))["confirmed_at"])

    def test_the_write_check_is_real_when_not_switched(self):
        c = self.login("master")
        before = self.q1("SELECT COUNT(*) n FROM student_ledger")["n"]
        c.post(f"/flight/students/{self.stu}/add_funds", data={"amount": "50"})
        self.assertGreater(self.q1("SELECT COUNT(*) n FROM student_ledger")["n"], before)

    def test_inactive_cfi_refused(self):
        c = self.login("master")
        c.post(f"/view-as/flight/cfi/{self.gone}")
        self.assertNotIn("_view_as_real", self._sess(c))

    def test_back_returns_to_the_page_they_were_on_and_restores_admin(self):
        c = self.login("master")
        c.post(f"/view-as/flight/cfi/{self.cfi2}", data={"next": "/flight/students"})
        r = c.post("/view-as/exit")
        self.assertTrue(r.headers["Location"].endswith("/flight/students"))
        s = self._sess(c)
        self.assertTrue(s["is_master_admin"])
        for k in ("_view_as_readonly", "_view_as_real"):
            self.assertNotIn(k, s)

    def test_can_move_from_one_picked_person_to_another(self):
        c = self.login("master")
        c.post(f"/view-as/flight/cfi/{self.cfi2}")
        c.post(f"/view-as/flight/student/{self.stu}")
        s = self._sess(c)
        self.assertEqual((s["flight_role"], s["student_id"]), ("student", self.stu))
        self.assertIsNone(s["cfi_id"])

    def test_academy_picker_and_shop_chips_unchanged(self):
        html = self.login("master").get("/academy").get_data(as_text=True)
        self.assertIn("/view-as/flight/student?next=", html)
        self.assertIn("Shop Admin", self.login("master").get("/shop").get_data(as_text=True))

    def assert_write_blocked(self, c):
        before = self.q1("SELECT COUNT(*) n FROM student_ledger")["n"]
        r = c.post(f"/flight/students/{self.stu}/add_funds", data={"amount": "50"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM student_ledger")["n"], before)
