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

    # ----- QA "view": an owner preview must never reach staff pages -----
    # The master admin's OWN real shop_role ("admin"), flight_role ("cfi")
    # and academy_access aren't reset by an owner preview (My Aircraft has
    # no role of its own to swap those for), so before this fix they just
    # sat in the session unchanged and every staff page kept working -
    # Shop, Flight School and Flight Academy alike - for as long as the
    # preview was active. These lock in that it's refused now, the same as
    # it would be for a real customer login.

    def test_owner_preview_cannot_reach_the_shop_dashboard(self):
        c = self.login("master")
        c.post(f"/view-as/owner/{self.customer_id}")
        r = c.get("/shop")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/shop", r.headers["Location"])

    def test_owner_preview_cannot_reach_the_flight_dashboard(self):
        c = self.login("master")
        c.post(f"/view-as/owner/{self.customer_id}")
        r = c.get("/flight/dashboard")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/flight/dashboard", r.headers["Location"])

    def test_owner_preview_cannot_reach_a_login_required_only_page(self):
        # academy_page only checks login_required + academy_access, no
        # shop_role/flight_role at all - exactly the kind of page a stale
        # session field could sneak past.
        c = self.login("master")
        c.post(f"/view-as/owner/{self.customer_id}")
        r = c.get("/academy")
        self.assertEqual(r.status_code, 302)

    def test_owner_preview_cannot_reach_admin_pages(self):
        c = self.login("master")
        c.post(f"/view-as/owner/{self.customer_id}")
        r = c.get("/admin/users")
        self.assertEqual(r.status_code, 302)

    def test_owner_preview_still_reaches_the_portal_itself(self):
        c = self.login("master")
        c.post(f"/view-as/owner/{self.customer_id}")
        # A single-plane owner (this test's setUp) is redirected straight
        # into that plane rather than shown a list-of-one - still 200 once
        # that redirect is followed, just not on "/portal/" itself.
        r = c.get("/portal/", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("N555AB", r.get_data(as_text=True))

    def test_exiting_an_owner_preview_restores_full_staff_access(self):
        c = self.login("master")
        c.post(f"/view-as/owner/{self.customer_id}")
        c.post("/view-as/exit", headers={"Referer": "/portal/"})
        self.assertEqual(c.get("/shop").status_code, 200)
        self.assertEqual(c.get("/flight/dashboard").status_code, 200)

    def test_owner_preview_banner_has_a_way_back(self):
        # Bug: the My Aircraft portal showed "Viewing as Owner (...)" with
        # no button at all to get back to the admin's own view - the portal
        # has no "View as" chips of its own (unlike Shop/Flight School), and
        # the banner's only exit button used to be conditioned on
        # person_view_name, which an owner preview never sets. Locks in
        # that the banner now offers a working way back for this preview.
        c = self.login("master")
        c.post(f"/view-as/owner/{self.customer_id}")
        html = c.get("/portal/", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Viewing as Owner", html)
        self.assertIn('action="/view-as/exit"', html)
        self.assertIn("Back to my view", html)
