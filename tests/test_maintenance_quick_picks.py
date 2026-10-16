"""Add Maintenance Item: type-to-search box with the usual items."""
from harness import OpsHubTestCase


class QuickPickTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.make_asset("N1")
        self.b = self.make_asset("N2")

    def test_new_form_offers_usual_items_and_fold(self):
        self.login("shop_admin")
        html = self.client.get(f"/assets/{self.a}/maintenance/new").get_data(as_text=True)
        for n in ("Oil change", "100-hour", "Annual", "Transponder check", "ELT battery", "Pitot-static"):
            self.assertIn(n, html)
        self.assertIn("More details", html)
        self.assertIn("as a new item", html)

    def test_custom_items_from_other_planes_are_offered(self):
        self.login("shop_admin")
        self.client.post(f"/assets/{self.b}/maintenance/new", data=dict(
            name="Prop balance", type="calendar", interval_days="180", category="scheduled_maint"))
        html = self.client.get(f"/assets/{self.a}/maintenance/new").get_data(as_text=True)
        self.assertIn("Prop balance", html)

    def test_edit_form_keeps_all_fields_open(self):
        self.login("shop_admin")
        self.client.post(f"/assets/{self.a}/maintenance/new", data=dict(name="X", type="hours", interval_hours="10"))
        iid = self.q1("SELECT id FROM maintenance_items")["id"]
        html = self.client.get(f"/maintenance/{iid}/edit").get_data(as_text=True)
        self.assertNotIn("More details", html)
        self.assertIn("Prework Checklist", html)

    def test_submitted_preset_creates_item(self):
        self.login("shop_admin")
        r = self.client.post(f"/assets/{self.a}/maintenance/new", data=dict(
            name="Pitot-static", type="calendar", interval_hours="", interval_days="730",
            category="scheduled_maint", last_done_date="2026-01-05"))
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM maintenance_items WHERE asset_id=?", (self.a,))
        self.assertEqual((row["name"], row["type"], row["interval_days"], row["last_done_date"]),
                         ("Pitot-static", "calendar", 730, "2026-01-05"))
