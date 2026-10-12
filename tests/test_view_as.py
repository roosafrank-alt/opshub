"""View As (master admin previews the app as a lower role): enter, exit,
cannot escalate, cannot stack, bad input.
"""
from harness import OpsHubTestCase


class ViewAsTest(OpsHubTestCase):
    def sess(self):
        with self.client.session_transaction() as s:
            return dict(s)

    def test_non_master_cannot_start(self):
        for role in ("shop_admin", "tech", "inspector", "cfi", "cfi_billing", "flight_student", "no_roles"):
            self.login(role)
            before = self.sess()
            for path in ("/view-as/shop/tech", "/view-as/flight/student", "/view-as/shop/admin"):
                r = self.client.post(path)
                self.assertNotEqual(r.status_code, 500, (role, path))
            after = self.sess()
            self.assertNotIn("_view_as_real", after, role)
            self.assertEqual(before.get("shop_role"), after.get("shop_role"), role)
            self.assertEqual(before.get("flight_role"), after.get("flight_role"), role)
            self.assertFalse(after.get("is_master_admin"), role)

    def test_logged_out_cannot_start(self):
        self.login(None)
        r = self.client.post("/view-as/shop/tech")
        self.assertNotEqual(r.status_code, 500)
        self.assertNotIn("_view_as_real", self.sess())

    def test_get_not_allowed(self):
        self.login("master")
        self.assertEqual(self.client.get("/view-as/shop/tech").status_code, 405)

    def test_shop_view_as_drops_admin_rights_and_exit_restores(self):
        self.login("master")
        self.assertEqual(self.client.get("/admin/users").status_code, 200)
        r = self.client.post("/view-as/shop/tech")
        self.assertEqual(r.status_code, 302)
        s = self.sess()
        self.assertEqual(s["shop_role"], "tech")
        self.assertFalse(s["is_master_admin"])
        self.assertFalse(s["can_bill"])
        # Admin-only pages are closed while previewing.
        self.assertIn(self.client.get("/admin/users").status_code, (302, 403))
        r = self.client.post("/view-as/exit")
        self.assertEqual(r.status_code, 302)
        s = self.sess()
        self.assertTrue(s["is_master_admin"])
        self.assertEqual(s["shop_role"], "admin")
        self.assertTrue(s["can_bill"])
        self.assertNotIn("_view_as_real", s)
        self.assertEqual(self.client.get("/admin/users").status_code, 200)

    def test_switching_previews_never_loses_the_real_admin_account(self):
        # Switching straight from one preview to another is intended; the
        # real admin rights must still come back on exit, and the previews
        # must not pile up leftovers (a Tech shop role while previewing flight).
        self.login("master")
        self.client.post("/view-as/shop/tech")
        self.client.post("/view-as/flight/student")
        s = self.sess()
        self.assertEqual(s["flight_role"], "student")
        self.assertEqual(s["shop_role"], "admin")  # baseline re-applied, no leftover Tech
        self.assertFalse(s["is_master_admin"])
        self.client.post("/view-as/shop/inspector")
        self.assertEqual(self.sess()["shop_role"], "inspector")
        self.assertEqual(self.sess()["flight_role"], "cfi")
        self.client.post("/view-as/exit")
        s = self.sess()
        self.assertTrue(s["is_master_admin"])
        self.assertEqual(s["shop_role"], "admin")
        self.assertEqual(s["flight_role"], "cfi")
        self.assertTrue(s["can_bill"])

    def test_own_role_chip_goes_back_to_normal_view(self):
        self.login("master")
        self.client.post("/view-as/shop/tech")
        self.client.post("/view-as/shop/admin")
        s = self.sess()
        self.assertTrue(s["is_master_admin"])
        self.assertNotIn("_view_as_real", s)

    def test_unknown_program_or_level_changes_nothing(self):
        self.login("master")
        self.assertEqual(self.client.post("/view-as/bogus/tech").status_code, 404)
        for level in ("master", "owner", "", "TECH", "tech%20", "cfi"):
            r = self.client.post(f"/view-as/shop/{level}")
            self.assertNotEqual(r.status_code, 500, level)
        for level in ("admin", "master", "tech"):
            r = self.client.post(f"/view-as/flight/{level}")
            self.assertNotEqual(r.status_code, 500, level)
        s = self.sess()
        self.assertNotIn("_view_as_real", s)
        self.assertTrue(s["is_master_admin"])

    def test_exit_without_view_as_is_harmless(self):
        self.login("master")
        r = self.client.post("/view-as/exit")
        self.assertEqual(r.status_code, 302)
        s = self.sess()
        self.assertTrue(s["is_master_admin"])
        self.assertEqual(s["shop_role"], "admin")

    def test_exit_for_non_master_does_not_grant_anything(self):
        self.login("tech")
        self.client.post("/view-as/exit")
        s = self.sess()
        self.assertFalse(s.get("is_master_admin"))
        self.assertEqual(s["shop_role"], "tech")

    def test_flight_view_as_student_and_cfi_then_exit(self):
        self.login("master")
        for level in ("student", "cfi"):
            r = self.client.post(f"/view-as/flight/{level}")
            self.assertIn(r.status_code, (302,), level)
            s = self.sess()
            if "_view_as_real" in s:
                self.assertEqual(s["flight_role"], level)
                self.assertFalse(s["is_master_admin"])
                self.assertFalse(s["can_bill"])
                self.assertIn(self.client.get("/admin/users").status_code, (302, 403))
                self.client.post("/view-as/exit")
            s = self.sess()
            self.assertTrue(s["is_master_admin"], level)
            self.assertEqual(s["flight_role"], "cfi", level)
            self.assertTrue(s["can_bill"], level)

    def test_flight_view_as_with_no_such_person_shows_message_not_error(self):
        self.login("master")
        self.exec("UPDATE students SET active=0")
        r = self.client.post("/view-as/flight/student")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("_view_as_real", self.sess())
        self.assertTrue(self.sess()["is_master_admin"])

    def test_previewing_as_tech_cannot_post_admin_actions(self):
        self.login("master")
        self.client.post("/view-as/shop/tech")
        before = self.q1("SELECT COUNT(*) n FROM users")["n"]
        self.client.post("/admin/users/new", data=dict(name="X", username="vx", password="pw-12345", shop_role="admin"))
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM users")["n"], before)

    def test_view_as_does_not_change_stored_account(self):
        self.login("master")
        self.client.post("/view-as/shop/tech")
        row = self.q1("SELECT * FROM users WHERE username='master'")
        self.assertEqual(row["shop_role"], "admin")
        self.assertEqual(row["is_master_admin"], 1)


class ViewAsPersonTest(OpsHubTestCase):
    def sess(self):
        with self.client.session_transaction() as s:
            return dict(s)

    def test_non_master_cannot_view_as_a_person(self):
        master_id = self.users["master"]["id"]
        for role in ("shop_admin", "tech", "inspector", "cfi", "flight_student", "no_roles"):
            self.login(role)
            before = self.sess()
            r = self.client.post(f"/view-as/person/{master_id}")
            self.assertNotEqual(r.status_code, 500, role)
            after = self.sess()
            self.assertEqual(after.get("user_id"), before.get("user_id"), role)
            self.assertFalse(after.get("is_master_admin"), role)
            self.assertEqual(self.client.get("/view-as/people").status_code in (200,), False, role)

    def test_missing_or_deactivated_person_is_refused_cleanly(self):
        self.login("master")
        me = self.sess()["user_id"]
        self.assertNotEqual(self.client.post("/view-as/person/999999").status_code, 500)
        self.exec("UPDATE users SET active=0 WHERE username='tech'")
        r = self.client.post(f"/view-as/person/{self.users['tech']['id']}")
        self.assertNotEqual(r.status_code, 500)
        self.assertEqual(self.sess()["user_id"], me)

    def test_person_view_then_exit_returns_to_real_admin(self):
        self.login("master")
        me = self.sess()["user_id"]
        r = self.client.post(f"/view-as/person/{self.users['tech']['id']}")
        self.assertEqual(r.status_code, 302)
        s = self.sess()
        self.assertEqual(s["user_id"], self.users["tech"]["id"])
        self.assertFalse(s.get("is_master_admin"))
        self.assertIn(self.client.get("/admin/users").status_code, (302, 403))
        self.client.post("/view-as/person/exit")
        s = self.sess()
        self.assertEqual(s["user_id"], me)
        self.assertTrue(s["is_master_admin"])

    def test_person_view_cannot_start_another_person_view_as_that_person(self):
        self.login("master")
        self.client.post(f"/view-as/person/{self.users['tech']['id']}")
        self.client.post(f"/view-as/person/{self.users['master']['id']}")
        # Viewing yourself just goes back; nobody ended up stuck or escalated wrongly.
        s = self.sess()
        self.assertEqual(s["user_id"], self.users["master"]["id"])
        self.assertTrue(s["is_master_admin"])
