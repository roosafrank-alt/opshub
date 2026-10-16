"""The shop's shared "Shop" login (and any station CFI login) does not get the
Fly with Kate! Students tab in the nav or the Students tile on the dashboard -
that account sits on the shop computer/TV and the roster is not for it. Every
other instructor/admin still sees both.

Also: the Manage tab sits right after the nav tabs (it takes the leftover
space) instead of floating halfway across the bar."""
from harness import OpsHubTestCase

NAV_TAB = 'id="tour-nav-flight-students"'
TILE = 'fw-bold mt-1">Students<'


class ShopLoginHidesStudentsTest(OpsHubTestCase):
    def dash(self, client):
        r = client.get("/flight/dashboard")
        self.assertEqual(r.status_code, 200)
        return r.data.decode()

    def test_regular_cfi_sees_students_tab_and_tile(self):
        html = self.dash(self.login("cfi"))
        self.assertIn(NAV_TAB, html)
        self.assertIn(TILE, html)

    def test_regular_flight_admin_sees_students_tab_and_tile(self):
        html = self.dash(self.login("flight_admin"))
        self.assertIn(NAV_TAB, html)

    def test_shared_shop_login_has_no_students_tab_or_tile(self):
        c = self.login("cfi")
        with c.session_transaction() as s:
            s["user_name"] = "Shop"
        html = self.dash(c)
        self.assertNotIn(NAV_TAB, html)
        self.assertNotIn(TILE, html)
        # the other shortcut tiles are still there
        self.assertIn('fw-bold mt-1">Schedule<', html)
        self.assertIn('fw-bold mt-1">Waitlist<', html)

    def test_shop_named_by_username_is_also_hidden(self):
        c = self.login("cfi")
        self.exec("UPDATE users SET username = 'shop' WHERE id = ?", (self.users["cfi"]["id"],))
        with c.session_transaction() as s:
            s["user_name"] = "Front Desk"
        html = self.dash(c)
        self.assertNotIn(NAV_TAB, html)
        self.assertNotIn(TILE, html)

    def test_admin_with_hidden_station_profile_keeps_students(self):
        """A Flight Admin with no instructor profile of their own gets a hidden
        station CFI row; that must not make them lose Students."""
        c = self.login("flight_admin")
        row = self.q1("SELECT is_station FROM cfis WHERE user_id = ?", (self.users["flight_admin"]["id"],))
        self.assertTrue(row and row["is_station"])
        html = self.dash(c)
        self.assertIn(NAV_TAB, html)

    def test_manage_takes_the_leftover_space_so_it_sits_next_to_the_tabs(self):
        html = self.dash(self.login("cfi"))
        before_manage = html[html.index("manage-menu-toggle") - 400:html.index("manage-menu-toggle")]
        self.assertIn("navbar-nav me-auto flight-nav-boxed", before_manage)
        # the tab list in front of it no longer takes the space itself
        self.assertNotIn("navbar-nav me-auto mb-2", html)

    def test_student_without_manage_keeps_tab_list_pushing_account_right(self):
        html = self.dash(self.login("flight_student"))
        self.assertNotIn("manage-menu-toggle", html)
        self.assertIn("navbar-nav me-auto mb-2", html)
