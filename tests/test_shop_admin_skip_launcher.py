"""QA finding ux-shop-admin-skip-launcher: a shop admin saw "Choose a
program" with just Winds Aloft and My Aircraft (really just Customers,
already on the shop's own Manage menu) every time they logged in - an
extra tap for nearly no real choice. Logging in as a shop admin now goes
straight to the shop home; the grid button still opens the real picker,
which now also offers Fly with Kate! for them, viewed as an unbilled,
never-bookable instructor (see db.ensure_flight_profile/auth.log_in_combined).
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

    def test_shop_admin_login_gets_a_station_cfi_profile(self):
        c, r = self._login_for_real("shop_admin")
        user_id = self.users["shop_admin"]["id"]
        cfi = self.q1("SELECT * FROM cfis WHERE user_id = ?", (user_id,))
        self.assertIsNotNone(cfi)
        self.assertEqual(cfi["is_station"], 1)
        self.assertEqual(cfi["rate_per_hour"], 0)
        with c.session_transaction() as s:
            self.assertEqual(s["cfi_id"], cfi["id"])

    def test_shop_admin_can_now_reach_flight_school(self):
        c, r = self._login_for_real("shop_admin")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Welcome", body)

    def test_shop_admin_still_has_no_billing_access(self):
        c, r = self._login_for_real("shop_admin")
        r2 = c.get("/flight/billing")
        self.assertEqual(r2.status_code, 302)

    def test_shop_admins_station_cfi_is_never_offered_when_booking(self):
        c, r = self._login_for_real("shop_admin")
        body = c.get("/flight/schedule/new").get_data(as_text=True)
        start = body.index('name="cfi_id"')
        end = body.index("</select>", start)
        self.assertNotIn("Shop Admin", body[start:end])

    def test_grid_button_still_opens_the_picker_with_three_tiles(self):
        c, r = self._login_for_real("shop_admin")
        body = c.get("/").get_data(as_text=True)
        self.assertIn("Choose a program", body)
        self.assertIn("Winds Aloft", body)
        self.assertIn("Fly with Kate!", body)
        self.assertIn(">Customers<", body)  # SEAM-9: the admin tile is Customers

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
