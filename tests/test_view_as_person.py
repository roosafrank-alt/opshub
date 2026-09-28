"""Idea "view anybody's account": an admin picks any user from the account
menu and sees the app exactly as that person does, without their password.
View only, and only for a real master admin."""
from harness import OpsHubTestCase


class ViewAsPersonTest(OpsHubTestCase):
    def test_admin_sees_the_persons_own_session_and_can_go_back(self):
        c = self.login("master")
        cfi = self.users["cfi"]
        r = c.post(f"/view-as/person/{cfi['id']}")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertEqual(s["user_id"], cfi["id"])
            self.assertFalse(s["is_master_admin"])
            self.assertEqual(s["flight_role"], "cfi")
            self.assertEqual(s["cfi_id"], self.q1("SELECT id FROM cfis WHERE user_id = ?", (cfi["id"],))["id"])
        html = c.get("/flight/dashboard", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Viewing as Cfi (view only)", html)
        self.assertIn("Back to my view", html)
        # Their account, not the admin's: admin pages are closed.
        self.assertEqual(c.get("/admin/users").status_code, 302)

        r = c.post("/view-as/person/exit")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertEqual(s["user_id"], self.users["master"]["id"])
            self.assertTrue(s["is_master_admin"])
            self.assertNotIn("_person_view_real", s)
        self.assertEqual(c.get("/admin/users").status_code, 200)

    def test_view_only_nothing_can_be_changed(self):
        c = self.login("master")
        tech = self.users["tech"]
        c.post(f"/view-as/person/{tech['id']}")
        before = self.q1("SELECT password_hash, name FROM users WHERE id = ?", (tech["id"],))
        r = c.post("/account", data={"name": "Hacked", "new_password": "x12345678", "confirm_password": "x12345678"},
                   headers={"Referer": "/account"})
        self.assertEqual(r.status_code, 302)
        after = self.q1("SELECT password_hash, name FROM users WHERE id = ?", (tech["id"],))
        self.assertEqual(tuple(before), tuple(after))
        r = c.post("/api/anything", json={})
        self.assertEqual(r.status_code, 403)
        html = c.get("/", follow_redirects=True).get_data(as_text=True)
        self.assertIn("nothing can be changed", html)

    def test_switch_straight_to_someone_else_then_back_to_admin(self):
        c = self.login("master")
        c.post(f"/view-as/person/{self.users['cfi']['id']}")
        r = c.post(f"/view-as/person/{self.users['tech']['id']}")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertEqual(s["user_id"], self.users["tech"]["id"])
        c.post("/view-as/person/exit")
        with c.session_transaction() as s:
            self.assertEqual(s["user_id"], self.users["master"]["id"])
            self.assertTrue(s["is_master_admin"])

    def test_picking_yourself_is_your_normal_view(self):
        c = self.login("master")
        c.post(f"/view-as/person/{self.users['cfi']['id']}")
        c.post(f"/view-as/person/{self.users['master']['id']}")
        with c.session_transaction() as s:
            self.assertEqual(s["user_id"], self.users["master"]["id"])
            self.assertNotIn("_person_view_real", s)

    def test_starting_from_a_role_preview_returns_to_the_real_admin(self):
        c = self.login("master")
        c.post("/view-as/shop/tech")
        c.post(f"/view-as/person/{self.users['cfi']['id']}")
        c.post("/view-as/person/exit")
        with c.session_transaction() as s:
            self.assertTrue(s["is_master_admin"])
            self.assertNotIn("_view_as_real", s)

    def test_only_a_master_admin_can_view_as_someone(self):
        for role in ("shop_admin", "tech", "cfi"):
            c = self.login(role)
            c.post(f"/view-as/person/{self.users['master']['id']}")
            with c.session_transaction() as s:
                self.assertEqual(s["user_id"], self.users[role]["id"], role)
            self.assertEqual(c.get("/view-as/people").status_code, 302, role)

    def test_inactive_account_cannot_be_viewed(self):
        self.exec("UPDATE users SET active = 0 WHERE id = ?", (self.users["tech"]["id"],))
        c = self.login("master")
        html = c.post(f"/view-as/person/{self.users['tech']['id']}", follow_redirects=True).get_data(as_text=True)
        self.assertIn("isn&#39;t active", html)

    def test_picker_lists_people_with_you_first_and_menu_link_is_admin_only(self):
        c = self.login("master")
        html = c.get("/view-as/people").get_data(as_text=True)
        self.assertIn("Your view (default)", html)
        self.assertLess(html.index("Your view (default)"), html.index(f"/view-as/person/{self.users['cfi']['id']}"))
        self.assertIn("Owner Customer", html)  # an owner with no staff login
        self.assertIn("View as someone", c.get("/shop").get_data(as_text=True))
        self.assertNotIn("View as someone", self.login("shop_admin").get("/shop").get_data(as_text=True))

    def test_while_viewing_the_menu_offers_the_way_back_not_log_out(self):
        c = self.login("master")
        c.post(f"/view-as/person/{self.users['tech']['id']}")
        html = c.get("/shop").get_data(as_text=True)
        self.assertIn("View someone else", html)
        self.assertNotIn("Log Out", html)
        self.assertEqual(c.get("/view-as/people").status_code, 200)
