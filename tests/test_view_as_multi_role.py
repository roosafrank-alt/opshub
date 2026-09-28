"""Idea "view as for multi-role accounts": Frank is an Admin AND an
Inspector. Roles in Admin > Accounts are now separate checkboxes, so one
account can hold several per program (users.shop_roles/flight_roles, main
role in shop_role/flight_role - see db.user_shop_roles). Anyone with more
than one role in a program gets "View as" chips for just their own roles,
and switching isn't view-only - it's using the app as that role. Clicking
your normal role's chip (Admin, for a master admin) goes straight back to
your normal view instead of needing "Back to admin"."""
from harness import OpsHubTestCase
import db


class ViewAsMultiRoleTest(OpsHubTestCase):
    def _add_roles(self, key, shop_roles=None, flight_roles=None):
        u = self.users[key]
        self.exec("UPDATE users SET shop_roles = ?, flight_roles = ? WHERE id = ?", (shop_roles, flight_roles, u["id"]))
        conn = db.get_db()
        db.ensure_flight_profile(conn, conn.execute("SELECT * FROM users WHERE id = ?", (u["id"],)).fetchone())
        conn.close()

    # ----- Accounts form -------------------------------------------------
    def test_account_form_saves_every_ticked_role(self):
        c = self.login("master")
        r = c.post("/admin/users/new", data={"name": "Pat", "username": "pat", "password": "pw12345",
                                             "shop_roles": ["inspector", "admin"], "flight_roles": ["student", "cfi"]})
        self.assertEqual(r.status_code, 302)
        u = self.q1("SELECT * FROM users WHERE username = 'pat'")
        self.assertEqual(u["shop_role"], "admin")  # main role = the highest ticked
        self.assertEqual(u["flight_role"], "cfi")
        self.assertEqual(db.user_shop_roles(u), ["admin", "inspector"])
        self.assertEqual(db.user_flight_roles(u), ["cfi", "student"])
        # Both flight profiles exist, so either role can be switched to.
        self.assertIsNotNone(self.q1("SELECT id FROM cfis WHERE user_id = ?", (u["id"],)))
        self.assertIsNotNone(self.q1("SELECT id FROM students WHERE user_id = ?", (u["id"],)))

    def test_edit_form_shows_checkboxes_and_can_remove_a_role(self):
        self._add_roles("tech", shop_roles="tech,inspector")
        t = self.users["tech"]
        c = self.login("master")
        html = c.get(f"/admin/users/{t['id']}/edit").get_data(as_text=True)
        self.assertIn('value="tech" id="shop-role-tech" checked', html)
        self.assertIn('value="inspector" id="shop-role-inspector" checked', html)
        self.assertIn('value="admin" id="shop-role-admin" >', html)
        c.post(f"/admin/users/{t['id']}/edit", data={"name": t["name"], "active": "on", "shop_roles": ["inspector"]})
        u = self.q1("SELECT * FROM users WHERE id = ?", (t["id"],))
        self.assertEqual((u["shop_role"], db.user_shop_roles(u)), ("inspector", ["inspector"]))

    def test_accounts_list_shows_every_role(self):
        self._add_roles("tech", shop_roles="tech,inspector")
        html = self.login("master").get("/admin/users").get_data(as_text=True)
        self.assertIn(">Tech</span><span", html.replace("\n", ""))
        self.assertIn(">Inspector</span>", html)

    # ----- Switching roles -----------------------------------------------
    def test_multi_role_account_gets_chips_for_just_its_own_roles(self):
        self._add_roles("tech", shop_roles="tech,inspector")
        html = self.login("tech").get("/shop").get_data(as_text=True)
        self.assertIn("/view-as/shop/inspector", html)
        self.assertNotIn("/view-as/shop/admin", html)
        self.assertNotIn("/view-as/shop/apprentice", html)

    def test_single_role_account_gets_no_chips(self):
        html = self.login("tech").get("/shop").get_data(as_text=True)
        self.assertNotIn("View as:", html)

    def test_multi_role_account_can_switch_and_use_the_other_role(self):
        self._add_roles("tech", shop_roles="tech,inspector")
        c = self.login("tech")
        r = c.post("/view-as/shop/inspector")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertEqual(s["shop_role"], "inspector")
        # Inspector-only page (admin + inspector) now opens for real.
        self.assertEqual(c.get("/squawks").status_code in (200, 302), True)
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Viewing as Inspector - everything works as it does for that role.", body)
        self.assertNotIn("not yours", body)

    def test_multi_role_account_cannot_switch_to_a_role_it_does_not_have(self):
        self._add_roles("tech", shop_roles="tech,inspector")
        c = self.login("tech")
        c.post("/view-as/shop/inspector")
        r = c.post("/view-as/shop/admin", follow_redirects=True)
        self.assertIn("Can&#39;t view as that", r.get_data(as_text=True))
        with c.session_transaction() as s:
            self.assertEqual(s["shop_role"], "inspector")
            self.assertFalse(s["is_master_admin"])
        # And no owner previews either - that's master admins only.
        c.post(f"/view-as/owner/{self.customer_id}")
        with c.session_transaction() as s:
            self.assertIsNone(s.get("customer_id"))

    def test_clicking_your_own_main_role_goes_back_to_normal(self):
        self._add_roles("tech", shop_roles="tech,inspector")
        c = self.login("tech")
        c.post("/view-as/shop/inspector")
        c.post("/view-as/shop/tech")
        with c.session_transaction() as s:
            self.assertNotIn("_view_as_real", s)
            self.assertEqual(s["shop_role"], "tech")

    def test_cfi_and_student_switches_to_their_own_student_profile_only(self):
        self._add_roles("cfi", flight_roles="cfi,student")
        own_student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        c = self.login("cfi")
        c.post("/view-as/flight/student")
        with c.session_transaction() as s:
            self.assertEqual(s["flight_role"], "student")
            self.assertEqual(s["student_id"], own_student)
            self.assertIsNone(s.get("cfi_id"))

    # ----- Master admin: Admin chip = back to normal -------------------------
    def test_master_admin_clicking_admin_goes_back_to_the_normal_view(self):
        c = self.login("master")
        c.post("/view-as/shop/inspector")
        c.post("/view-as/shop/tech")
        r = c.post("/view-as/shop/admin")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertNotIn("_view_as_real", s)
            self.assertTrue(s["is_master_admin"])
            self.assertEqual(s["shop_role"], "admin")

    def test_master_admin_flight_chips_offer_admin_to_go_back(self):
        c = self.login("master")
        c.post("/view-as/flight/student")
        html = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn('title="Back to your normal view">Admin</button>', html)
        c.post("/view-as/flight/admin")
        with c.session_transaction() as s:
            self.assertTrue(s["is_master_admin"])
            self.assertEqual(s["flight_role"], "cfi")

    def test_master_admin_viewing_as_inspector_is_not_labelled_view_only(self):
        c = self.login("master")
        c.post("/view-as/shop/inspector")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Viewing as Inspector", body)
        self.assertNotIn("not yours", body)
        self.assertIn("Back to your normal view", body)
