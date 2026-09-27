"""Idea "My aircraft": add an "owner" View As, so a master admin can see the
My Aircraft portal exactly as a specific customer would, the same way the
existing View As already previews Shop/Flight School roles.
"""
from harness import OpsHubTestCase


class OwnerViewAsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N555AB")
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, self.asset))

    def test_master_admin_can_view_as_the_owner(self):
        c = self.login("master")
        r = c.post(f"/view-as/owner/{self.customer_id}")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/portal", r.headers["Location"])
        html = c.get("/portal/", follow_redirects=True).get_data(as_text=True)
        self.assertIn("N555AB", html)
        self.assertIn("Owner Customer", html)
        with c.session_transaction() as s:
            self.assertFalse(s["is_master_admin"])
            self.assertEqual(s["customer_id"], self.customer_id)

    def test_exiting_restores_admin_and_returns_to_admin_customers_page(self):
        c = self.login("master")
        c.post(f"/view-as/owner/{self.customer_id}")
        r = c.post("/view-as/exit", headers={"Referer": "/portal/"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/customers", r.headers["Location"])
        with c.session_transaction() as s:
            self.assertTrue(s["is_master_admin"])
            self.assertIsNone(s.get("customer_id"))
        # Real admin access still works after exiting.
        self.assertEqual(c.get("/admin/users").status_code, 200)

    def test_non_master_admin_cannot_start_an_owner_preview(self):
        c = self.login("shop_admin")
        r = c.post(f"/view-as/owner/{self.customer_id}", follow_redirects=True)
        self.assertIn("Can&#39;t view as that", r.get_data(as_text=True))
        with c.session_transaction() as s:
            self.assertIsNone(s.get("customer_id"))

    def test_inactive_customer_cannot_be_previewed(self):
        self.exec("UPDATE customers SET active = 0 WHERE id = ?", (self.customer_id,))
        c = self.login("master")
        r = c.post(f"/view-as/owner/{self.customer_id}", follow_redirects=True)
        self.assertIn("isn&#39;t active", r.get_data(as_text=True))

    def test_view_as_button_only_offered_to_a_real_master_admin(self):
        html = self.login("master").get("/customers").get_data(as_text=True)
        self.assertIn("View as", html)
        html = self.login("shop_admin").get("/customers").get_data(as_text=True)
        self.assertNotIn("View as", html)
