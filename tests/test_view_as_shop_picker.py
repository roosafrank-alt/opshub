"""Idea: Switch role on the Maintenance side works like Flight's: Tech /
Apprentice / Inspector open a picker with "Use it as a ... myself" plus a
look-only list of the other people in that role (own name left out)."""
from harness import OpsHubTestCase


class ViewAsShopPickerTest(OpsHubTestCase):
    def test_picker_lists_others_and_hides_me(self):
        c = self.login("shop_admin_tech")
        body = c.get("/view-as/shop/pick/tech").get_data(as_text=True)
        self.assertIn("Use it as a Tech myself", body)
        self.assertIn(self.users["tech"]["name"].lower(), body.lower())
        self.assertNotIn(f'data-name="{self.users["shop_admin_tech"]["name"].lower()}"', body)

    def test_look_as_person_is_look_only_and_exit_restores_me(self):
        c = self.login("shop_admin_tech")
        me = self.users["shop_admin_tech"]
        r = c.post(f"/view-as/shop/tech/{self.users['tech']['id']}", data={"next": "/"})
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertEqual(s["user_id"], self.users["tech"]["id"])
            self.assertEqual(s["shop_role"], "tech")
            self.assertTrue(s["_view_as_readonly"])
        # nothing can be changed while looking
        r = c.post("/logout-nope-not-a-route", data={"x": 1})
        r = c.post("/view-as/exit")
        with c.session_transaction() as s:
            self.assertEqual(s["user_id"], me["id"])
            self.assertEqual(s["shop_role"], "admin")
            self.assertFalse(s.get("_view_as_readonly"))

    def test_myself_button_is_not_look_only(self):
        c = self.login("master")
        c.post("/view-as/shop/tech")
        with c.session_transaction() as s:
            self.assertEqual(s["shop_role"], "tech")
            self.assertFalse(s.get("_view_as_readonly"))
            self.assertEqual(s["user_id"], self.users["master"]["id"])

    def test_cannot_look_as_self_or_wrong_role_or_without_rights(self):
        c = self.login("master")
        c.post(f"/view-as/shop/tech/{self.users['master']['id']}")
        with c.session_transaction() as s:
            self.assertEqual(s["user_id"], self.users["master"]["id"])
            self.assertFalse(s.get("_view_as_readonly"))
        c.post(f"/view-as/shop/inspector/{self.users['tech']['id']}")
        with c.session_transaction() as s:
            self.assertNotEqual(s.get("shop_role"), "inspector")
        t = self.login("tech")
        self.assertEqual(t.get("/view-as/shop/pick/tech").status_code, 403)


class SwitchedRolesOnlyOpenTheirOwnProgramsTest(OpsHubTestCase):
    def test_master_as_tech_cannot_open_flight_or_academy(self):
        c = self.login("master")
        c.post("/view-as/shop/tech")
        self.assertNotEqual(c.get("/flight/").status_code, 200)
        r = c.get("/academy")
        self.assertNotEqual(r.status_code, 200)

    def test_master_as_cfi_cannot_open_shop(self):
        uid = self.users["master"]["id"]
        self.exec("INSERT INTO cfis (name, username, password_hash, active, user_id) VALUES ('M CFI','mcfi2','x',1,?)", (uid,))
        c = self.login("master")
        c.post("/view-as/flight/cfi/me")
        with c.session_transaction() as s:
            self.assertIsNone(s.get("shop_role"))

    def test_forbidden_page_has_a_way_back(self):
        c = self.login("master")
        c.post("/view-as/shop/tech")
        r = c.get("/admin/users")
        self.assertIn(r.status_code, (302, 403))
        if r.status_code == 403:
            self.assertIn("Back to my normal view", r.get_data(as_text=True))
        c.post("/view-as/exit")
        self.assertEqual(c.get("/admin/users").status_code, 200)


class CustomerViewStaysInPortalTest(OpsHubTestCase):
    def test_other_pages_bounce_back_to_the_portal(self):
        c = self.login("master")
        cid = self.exec("INSERT INTO customers (name, email, password_hash, active) VALUES ('Cust A', 'a@example.com', 'x', 1)")
        c.post(f"/portal/view-as/{cid}")
        r = c.get("/admin/users")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/portal", r.headers["Location"])
        c.post("/portal/view-as/exit")
        self.assertEqual(c.get("/admin/users").status_code, 200)


class NoStaffRolesInCustomerViewTest(OpsHubTestCase):
    def test_switch_role_row_hidden_while_looking_at_customers(self):
        c = self.login("master")
        self.assertIn("Switch role", c.get("/shop").get_data(as_text=True))
        body = c.get("/portal/view-as").get_data(as_text=True)
        self.assertNotIn("Switch role", body)
        cid = self.exec("INSERT INTO customers (name, email, password_hash, active) VALUES ('Cust B', 'b@example.com', 'x', 1)")
        c.post(f"/portal/view-as/{cid}")
        self.assertNotIn("Switch role", c.get("/portal/").get_data(as_text=True))
