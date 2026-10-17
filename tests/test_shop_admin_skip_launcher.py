"""QA finding ux-shop-admin-skip-launcher: a shop admin saw "Choose a
program" with just Winds Aloft and My Aircraft (really just Customers,
already on the shop's own Manage menu) every time they logged in - an
extra tap for nearly no real choice. Logging in as a shop admin now goes
straight to the shop home; the grid button still opens the real picker,
offers Winds Aloft and Customers. (Flight Admin role: a Shop Admin no
longer gets Fly with Kate! at all - no tile, no hidden instructor profile,
no pages - because each admin acts only in their own program.)
Master admins are unaffected - they still land on the picker with all four
tiles."""
from harness import OpsHubTestCase, PASSWORD
import db


class ShopAdminSkipLauncherTest(OpsHubTestCase):
    def _login_for_real(self, username):
        client = self.app.test_client()
        return client, client.post("/", data={"username": username, "password": PASSWORD, "remember": "on"})

    def test_shop_admin_login_redirects_straight_to_shop(self):
        c, r = self._login_for_real("shop_admin")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/shop", r.headers["Location"])

    def test_shop_admin_login_gets_no_flight_profile(self):
        c, r = self._login_for_real("shop_admin")
        user_id = self.users["shop_admin"]["id"]
        self.assertIsNone(self.q1("SELECT * FROM cfis WHERE user_id = ?", (user_id,)))
        with c.session_transaction() as s:
            self.assertNotIn("cfi_id", s)

    def test_shop_admin_cannot_reach_flight_school(self):
        c, r = self._login_for_real("shop_admin")
        r2 = c.get("/flight/dashboard")
        self.assertEqual(r2.status_code, 302)
        r3 = c.get("/flight/schedule/new")
        self.assertEqual(r3.status_code, 302)

    def test_shop_admin_still_has_no_billing_access(self):
        c, r = self._login_for_real("shop_admin")
        r2 = c.get("/flight/billing")
        self.assertEqual(r2.status_code, 302)

    def test_grid_button_still_opens_the_picker_with_two_tiles(self):
        c, r = self._login_for_real("shop_admin")
        body = c.get("/").get_data(as_text=True)
        self.assertIn("Choose a program", body)
        self.assertIn("Winds Aloft", body)
        self.assertNotIn("Fly with Kate!</div>", body)
        self.assertIn(">Customers<", body)  # SEAM-9: the admin tile is Customers

    def test_picker_has_phone_row_css_and_keeps_big_tiles(self):
        # QA ux-launcher-phone-rows: phones get short rows via a media query.
        c, r = self._login_for_real("master")
        body = c.get("/").get_data(as_text=True)
        self.assertIn("@media (max-width: 767.98px)", body)
        self.assertIn(".tile-accent { display: none; }", body)
        self.assertIn("launcher-tile tile-shop", body)

    def test_master_admin_login_still_lands_on_the_picker(self):
        c, r = self._login_for_real("master")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/shop", r.headers["Location"])
        body = c.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Choose a program", body)

    def test_tech_login_is_unaffected(self):
        c, r = self._login_for_real("tech")
        self.assertEqual(r.status_code, 302)
        # Tech isn't a shop admin, so login still goes through the picker
        # route (which then applies the existing single-program skip to
        # /shop on its own, unrelated to this idea) - no cfis row created.
        self.assertEqual(r.headers["Location"], "/")
        user_id = self.users["tech"]["id"]
        self.assertIsNone(self.q1("SELECT * FROM cfis WHERE user_id = ?", (user_id,)))
