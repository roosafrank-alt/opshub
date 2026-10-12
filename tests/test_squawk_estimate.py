"""Idea "adding estimate price area": a squawk has an editable estimate
(hours and cost) on the Squawks page; blank cost is priced from the hours."""
from harness import OpsHubTestCase, seed_row
import db


class SquawkEstimateTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        conn = db.get_db()
        self.sq = seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Left brake soft")
        conn.commit()
        conn.close()

    def row(self):
        conn = db.get_db()
        r = conn.execute("SELECT est_hours, est_cost FROM plane_squawks WHERE id = ?", (self.sq,)).fetchone()
        conn.close()
        return r

    def test_save_and_update_estimate(self):
        c = self.login("shop_admin")
        c.post(f"/squawks/quick/{self.sq}/estimate", data={"est_hours": "2.5", "est_cost": "300"})
        r = self.row()
        self.assertEqual((r["est_hours"], r["est_cost"]), (2.5, 300.0))
        c.post(f"/squawks/quick/{self.sq}/estimate", data={"est_hours": "4", "est_cost": "450"})
        self.assertEqual(self.row()["est_hours"], 4.0)
        self.assertIn('name="est_hours"', c.get("/squawks").get_data(as_text=True))

    def test_bad_number_rejected(self):
        c = self.login("shop_admin")
        c.post(f"/squawks/quick/{self.sq}/estimate", data={"est_hours": "abc"})
        self.assertIsNone(self.row()["est_hours"])
