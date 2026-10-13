"""Idea "adding estimate price area": a job Discrepancy has the same editable
estimate (hours and cost) as a squawk; blank cost is priced from the hours."""
from harness import OpsHubTestCase
import db


class DiscrepancyEstimateTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        self.project_id = self.make_project(name="Annual - N999TT", asset_id=self.asset_id)
        self.section_id = self.exec(
            "INSERT INTO project_sections (project_id, name, created_at) VALUES (?,?,?)",
            (self.project_id, "Brakes", db.now_iso()))
        self.url = f"/projects/{self.project_id}/sections/{self.section_id}/estimate"

    def row(self):
        return self.q1("SELECT est_hours, est_cost FROM project_sections WHERE id = ?", (self.section_id,))

    def test_save_and_update_estimate(self):
        c = self.login("shop_admin")
        c.post(self.url, data={"est_hours": "2.5", "est_cost": "300"})
        r = self.row()
        self.assertEqual((r["est_hours"], r["est_cost"]), (2.5, 300.0))
        c.post(self.url, data={"est_hours": "4", "est_cost": "450"})
        self.assertEqual(self.row()["est_hours"], 4.0)
        self.assertIn('name="est_hours"', c.get(f"/projects/{self.project_id}").get_data(as_text=True))

    def test_bad_number_rejected(self):
        c = self.login("shop_admin")
        c.post(self.url, data={"est_hours": "abc"})
        self.assertIsNone(self.row()["est_hours"])

    def test_linked_squawk_shows_squawks_own_estimate(self):
        from harness import seed_row
        conn = db.get_db()
        sq = seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Left brake soft", est_hours=3.0, est_cost=360.0)
        conn.execute("UPDATE project_sections SET linked_squawk_kind='quick', linked_squawk_id=? WHERE id=?",
                     (sq, self.section_id))
        conn.commit()
        conn.close()
        body = self.login("shop_admin").get(f"/projects/{self.project_id}").get_data(as_text=True)
        self.assertIn(f"/squawks/quick/{sq}/estimate", body)
        self.assertIn("360.00", body)


class ShopRateEstimateTest(DiscrepancyEstimateTest):
    def test_rate_hours_parts_total(self):
        c = self.login("shop_admin")
        c.post(self.url, data={"est_hours": "3", "est_rate": "110", "est_parts": "75.50"})
        r = self.q1("SELECT est_hours, est_parts, est_cost FROM project_sections WHERE id = ?", (self.section_id,))
        self.assertEqual((r["est_hours"], r["est_parts"], r["est_cost"]), (3.0, 75.5, 405.5))
        self.assertEqual(self.q1("SELECT shop_rate FROM assets WHERE id = ?", (self.asset_id,))["shop_rate"], 110.0)
        # The rate sticks to the aircraft: a later save without it reuses it.
        c.post(self.url, data={"est_hours": "2", "est_parts": ""})
        self.assertEqual(self.row()["est_cost"], 220.0)
        body = c.get(f"/projects/{self.project_id}").get_data(as_text=True)
        self.assertIn('name="est_rate"', body)
        self.assertIn(">Total</button>", body)

    def test_squawk_uses_aircraft_rate(self):
        from harness import seed_row
        conn = db.get_db()
        sq = seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Soft brake")
        conn.commit()
        conn.close()
        self.login("shop_admin").post(f"/squawks/quick/{sq}/estimate",
                                      data={"est_hours": "2", "est_rate": "100", "est_parts": "50"})
        r = self.q1("SELECT est_hours, est_parts, est_cost FROM plane_squawks WHERE id = ?", (sq,))
        self.assertEqual((r["est_hours"], r["est_parts"], r["est_cost"]), (2.0, 50.0, 250.0))
