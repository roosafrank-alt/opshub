"""QA finding ux-portal-none-none: on the owner portal (/portal), a plane
with no make/model on file showed the literal words "None None" under its
tail number. Fix: show make/model when at least one is filled in, leave the
line out entirely when both are blank."""
from harness import OpsHubTestCase


class PortalAssetMakeModelTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.blank_asset = self.make_asset("N111BL")
        self.filled_asset = self.exec(
            "INSERT INTO assets (tag, name, make, model, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("N222FL", "Cessna 172", "Cessna", "172", self._now(), self._now()))
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)",
                   (self.customer_id, self.blank_asset))
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)",
                   (self.customer_id, self.filled_asset))

    def _now(self):
        import db
        return db.now_iso()

    def test_dashboard_list_hides_make_model_line_when_blank(self):
        html = self.login("customer").get("/portal/").get_data(as_text=True)
        self.assertNotIn("None None", html)
        self.assertIn("N111BL", html)

    def test_dashboard_list_shows_make_model_when_present(self):
        html = self.login("customer").get("/portal/").get_data(as_text=True)
        self.assertIn("Cessna 172", html)

    def test_single_plane_owner_asset_page_hides_make_model_when_blank(self):
        # A single-plane owner is redirected straight to their plane's page,
        # which has the same tag/make/model header.
        self.exec("DELETE FROM customer_assets WHERE asset_id = ?", (self.filled_asset,))
        html = self.login("customer").get("/portal/", follow_redirects=True).get_data(as_text=True)
        self.assertNotIn("None None", html)
        self.assertIn("N111BL", html)
