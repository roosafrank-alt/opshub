"""Idea "add who updated the tach": Log Current Hours now records who
entered a Hobbs/Tach reading (shop staff vs. the owner) alongside the
existing timestamp, on both the shop and My Aircraft portal routes.
"""
from harness import OpsHubTestCase


class HoursUpdatedByTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N42AB")
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, self.asset))

    def asset_row(self):
        return self.q1("SELECT * FROM assets WHERE id = ?", (self.asset,))

    def test_shop_update_is_tagged_with_the_staff_name(self):
        c = self.login("tech")
        r = c.post(f"/assets/{self.asset}/update_hours", data=dict(hobbs_hours="123.4", tach_hours="98.7"))
        self.assertEqual(r.status_code, 302)
        a = self.asset_row()
        self.assertEqual(a["hobbs_hours"], 123.4)
        self.assertEqual(a["hobbs_updated_by"], "Shop - Tech")
        self.assertEqual(a["tach_updated_by"], "Shop - Tech")
        self.assertIsNotNone(a["hobbs_updated_at"])

    def test_owner_update_is_tagged_with_the_owner_name(self):
        c = self.login("customer")
        r = c.post(f"/portal/aircraft/{self.asset}/hours", data=dict(hobbs_hours="200.0"))
        self.assertEqual(r.status_code, 302)
        a = self.asset_row()
        self.assertEqual(a["hobbs_hours"], 200.0)
        self.assertEqual(a["hobbs_updated_by"], "Owner - Owner Customer")

    def test_who_and_when_shown_on_the_asset_page(self):
        self.login("tech").post(f"/assets/{self.asset}/update_hours", data=dict(tach_hours="55.5"))
        html = self.login("tech").get(f"/assets/{self.asset}").get_data(as_text=True)
        self.assertIn("Shop - Tech", html)

    def test_who_and_when_shown_on_the_portal_page(self):
        self.login("customer").post(f"/portal/aircraft/{self.asset}/hours", data=dict(tach_hours="55.5"))
        html = self.login("customer").get(f"/portal/aircraft/{self.asset}").get_data(as_text=True)
        self.assertIn("Owner - Owner Customer", html)

    def test_updating_tach_immediately_changes_a_maintenance_items_status(self):
        self.exec("""INSERT INTO maintenance_items (asset_id, name, interval_hours, last_done_hours, created_at)
                     VALUES (?, 'Oil Change', 50, 0, datetime('now'))""", (self.asset,))
        self.login("tech").post(f"/assets/{self.asset}/update_hours", data=dict(tach_hours="60"))
        html = self.login("tech").get(f"/assets/{self.asset}").get_data(as_text=True)
        self.assertIn("Oil Change", html)
        self.assertIn("overdue", html.lower())
