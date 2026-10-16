"""Frank 2026-10-16: Admin > Accounts is the one place to set a person up - tabs for Flight School,
Flight Academy, Shop and Customer, each with one Grant access box. Plus type ratings on the account."""
from harness import OpsHubTestCase


class AccountTabsTest(OpsHubTestCase):
    def _base(self, u, **extra):
        d = {"access_tabs": "1", "tab": "flight", "name": u["name"], "active": "on", "customer_tab": "1"}
        d.update(extra)
        return d

    def test_unticking_grant_removes_that_programs_roles(self):
        u = self.users["cfi"]
        self.login("master").post(f"/admin/users/{u['id']}/edit",
                                  data=self._base(u, flight_roles="cfi", shop_roles="tech", grant_shop="on"))
        row = self.q1("SELECT shop_roles, flight_roles FROM users WHERE id = ?", (u["id"],))
        self.assertEqual(row["shop_roles"], "tech")
        self.assertIsNone(row["flight_roles"])          # Flight School box unticked -> CFI role gone

    def test_grant_without_a_role_is_refused(self):
        u = self.users["cfi"]
        r = self.login("master").post(f"/admin/users/{u['id']}/edit", data=self._base(u, grant_shop="on", grant_flight="on", flight_roles="cfi"))
        self.assertIn("no Shop role is", r.get_data(as_text=True))

    def test_cfi_details_and_type_ratings_save_and_page_stays_on_tab(self):
        u = self.users["cfi"]
        cfi = self.q1("SELECT * FROM cfis WHERE user_id = ?", (u["id"],))
        r = self.login("master").post(f"/admin/users/{u['id']}/edit", data=self._base(
            u, grant_flight="on", flight_roles="cfi", cfi_fields="1", cfi_rate_per_hour="72",
            cfi_cred_cfii="on", cfi_cfi_cert_number="1234567", cfi_medical_class="second",
            type_ratings="ce-525s; b-737, CE-525S"))
        self.assertIn(f"/admin/users/{u['id']}/edit?tab=flight", r.headers["Location"])
        row = self.q1("SELECT * FROM cfis WHERE id = ?", (cfi["id"],))
        self.assertEqual((row["rate_per_hour"], row["cred_cfii"], row["cfi_cert_number"], row["medical_class"]),
                         (72.0, 1, "1234567", "second"))
        self.assertEqual(self.q1("SELECT type_ratings FROM users WHERE id = ?", (u["id"],))["type_ratings"], "CE-525S, B-737")
        self.assertIn("CE-525S, B-737", self.login("cfi").get("/flight/cfis/me").get_data(as_text=True))

    def test_customer_tab_makes_and_closes_the_owner_login(self):
        u = self.users["cfi"]
        self.exec("UPDATE users SET email = 'cfi.person@example.com' WHERE id = ?", (u["id"],))
        c = self.login("master")
        c.post(f"/admin/users/{u['id']}/edit", data=self._base(u, grant_flight="on", flight_roles="cfi",
                                                              email="cfi.person@example.com", grant_customer="on"))
        cust = self.q1("SELECT * FROM customers WHERE lower(email) = 'cfi.person@example.com'")
        self.assertTrue(cust and cust["active"])
        self.assertEqual(cust["password_hash"], self.q1("SELECT password_hash FROM users WHERE id = ?", (u["id"],))["password_hash"])
        c.post(f"/admin/users/{u['id']}/edit", data=self._base(u, grant_flight="on", flight_roles="cfi",
                                                              email="cfi.person@example.com"))
        self.assertEqual(self.q1("SELECT active FROM customers WHERE id = ?", (cust["id"],))["active"], 0)

    def test_badge_rate_edited_on_shop_tab(self):
        u = self.users["tech"]
        self.exec("INSERT INTO laborers (name, code, rate, active, user_id, created_at, updated_at) VALUES ('T', 'LABOR-T', 20, 1, ?, 'x', 'x')", (u["id"],))
        lid = self.q1("SELECT id FROM laborers WHERE user_id = ?", (u["id"],))["id"]
        self.login("master").post(f"/admin/users/{u['id']}/edit", data=self._base(
            u, grant_shop="on", shop_roles="tech", **{f"badge_{lid}_shown": "1", f"badge_{lid}_rate": "31.5"}))
        row = self.q1("SELECT rate, active FROM laborers WHERE id = ?", (lid,))
        self.assertEqual((row["rate"], row["active"]), (31.5, 0))

    def test_master_admin_list_links_open_the_account_tab(self):
        c = self.login("master")
        cfi_user = self.users["cfi"]["id"]
        self.assertIn(f"/admin/users/{cfi_user}/edit?tab=flight", c.get("/flight/cfis").get_data(as_text=True))
        stu_user = self.users["flight_student"]["id"]
        self.assertIn(f"/admin/users/{stu_user}/edit?tab=flight", c.get("/flight/students").get_data(as_text=True))
        # a CFI keeps the old pages
        self.assertNotIn("/admin/users/", self.login("cfi").get("/flight/students").get_data(as_text=True))
