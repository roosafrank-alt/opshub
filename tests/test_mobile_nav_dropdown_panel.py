"""Idea "Mobile view drop downs": every collapsed mobile header menu (Fly
with Kate!, Winds Aloft, Admin, My Account) now uses the same compact
floating-panel treatment - a same-width-button box hanging off the right of
the bar, instead of a full-width strip. That also fixed the Winds Aloft
header's "Manage" dropdown opening off the left edge of a phone screen: it
was dropdown-menu-end against a toggle that used to sit mid-width in the
full-width strip, and now sits near the true right edge of the panel. The
shared CSS lives in static/css/style.css (`#nav.navbar-collapse` inside the
`.navbar-expand-lg`/`.navbar-expand-xl` media queries); this just checks
every header's markup carries the pieces that CSS depends on."""
from harness import OpsHubTestCase


class MobileNavDropdownPanelTest(OpsHubTestCase):
    def _assert_panel_ready(self, body, nav_class):
        # Bootstrap's own collapse id/class, which style.css's
        # `#nav.navbar-collapse` selector targets.
        self.assertIn('id="nav"', body)
        self.assertIn('collapse navbar-collapse', body)
        self.assertIn(nav_class, body)

    def test_flight_header_uses_the_shared_panel(self):
        c = self.login("cfi")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        self._assert_panel_ready(body, "navbar-expand-lg")
        self.assertIn("bg-primary", body)

    def test_shop_header_uses_the_shared_panel(self):
        c = self.login("master")
        body = c.get("/shop").get_data(as_text=True)
        self._assert_panel_ready(body, "navbar-expand-xl")
        self.assertIn("bg-dark", body)
        # The "Manage" dropdown stays end-aligned - it's the toggle's own
        # position (now near the panel's right edge) that fixed the cutoff,
        # not a change to the dropdown's own alignment class.
        self.assertIn('id="shop-manage-toggle"', body)
        self.assertIn('dropdown-menu dropdown-menu-end" aria-labelledby="shop-manage-toggle"', body)

    def test_admin_header_uses_the_shared_panel(self):
        c = self.login("master")
        body = c.get("/admin").get_data(as_text=True)
        self._assert_panel_ready(body, "navbar-expand-lg")
        self.assertIn("admin-nav", body)

    def test_account_header_uses_the_shared_panel(self):
        c = self.login("master")
        body = c.get("/account").get_data(as_text=True)
        self._assert_panel_ready(body, "navbar-expand-lg")
        self.assertIn("account-nav", body)
