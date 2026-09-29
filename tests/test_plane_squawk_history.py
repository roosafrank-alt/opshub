"""Idea "squawks": a completed squawk stays on its plane's page, in a Squawks
section with the same details the Squawks page's Done list shows."""
from harness import OpsHubTestCase, seed_row
import db


class PlaneSquawkHistoryTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N123SQ")
        self.other_id = self.make_asset("N999OT")

    def make_squawk(self, asset_id, notes, **kw):
        conn = db.get_db()
        seed_row(conn, "plane_squawks", asset_id=asset_id, notes=notes, **kw)
        conn.commit()
        conn.close()

    def test_completed_squawk_shows_on_plane_page(self):
        self.make_squawk(self.asset_id, "Left brake soft", reported_by="Kate",
                         repaired_at=db.now_iso(), repaired_by="Tech Tom")
        self.make_squawk(self.other_id, "Other plane problem",
                         repaired_at=db.now_iso(), repaired_by="Someone")
        body = self.login("shop_admin").get(f"/assets/{self.asset_id}").get_data(as_text=True)
        self.assertIn('id="plane-squawks"', body)
        self.assertIn("Left brake soft", body)
        self.assertIn("Kate", body)
        self.assertIn("Tech Tom", body)
        self.assertNotIn("Other plane problem", body)

    def test_no_section_without_completed_squawks(self):
        self.make_squawk(self.asset_id, "Still open")
        body = self.login("shop_admin").get(f"/assets/{self.asset_id}").get_data(as_text=True)
        self.assertNotIn('id="plane-squawks"', body)
