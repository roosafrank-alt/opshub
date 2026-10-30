"""A squawk ticked 'Not safe to fly' grounds its plane (same Ground as the
Planes page) until signed off; other groundings are never released by it."""
from datetime import date, timedelta

import db
from harness import OpsHubTestCase


class SquawkGroundsPlane(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N777GG")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.asset,))

    def plane(self):
        return self.q1("SELECT * FROM assets WHERE id = ?", (self.asset,))

    def report(self, tick, who="tech"):
        c = self.login(who)
        data = {"asset_id": self.asset, "notes": "Tire cord showing"}
        if tick:
            data["grounds_plane"] = "1"
        c.post("/squawks/new", data=data)
        return self.q1("SELECT * FROM plane_squawks ORDER BY id DESC LIMIT 1")

    def test_plain_squawk_does_not_ground(self):
        self.report(False)
        self.assertIsNone(self.plane()["grounded_at"])

    def test_tick_grounds_then_signoff_releases(self):
        sq = self.report(True)
        self.assertTrue(sq["grounds_plane"])
        self.assertIsNotNone(self.plane()["grounded_at"])
        self.assertIn("Not safe to fly", self.plane()["grounded_reason"])
        html = self.login("tech").get("/squawks").get_data(as_text=True)
        self.assertIn("Grounded", html)
        self.login("inspector").post(f"/squawks/quick/{sq['id']}/repair_confirm", data={"action": "self_repair"})
        self.assertIsNone(self.plane()["grounded_at"])

    def test_signoff_keeps_admin_ground_and_second_squawk(self):
        self.login("master").post(f"/flight/planes/{self.asset}/ground", data={"reason": "annual"})
        sq = self.report(True)
        self.login("inspector").post(f"/squawks/quick/{sq['id']}/repair_confirm", data={"action": "self_repair"})
        self.assertEqual(self.plane()["grounded_reason"], "annual")
        self.exec("UPDATE assets SET grounded_at = NULL, grounded_reason = NULL WHERE id = ?", (self.asset,))
        a = self.report(True)
        b = self.report(True)
        self.login("inspector").post(f"/squawks/quick/{a['id']}/repair_confirm", data={"action": "self_repair"})
        self.assertIsNotNone(self.plane()["grounded_at"])
        self.login("inspector").post(f"/squawks/quick/{b['id']}/repair_confirm", data={"action": "self_repair"})
        self.assertIsNone(self.plane()["grounded_at"])

    def test_reopen_regrounds(self):
        sq = self.report(True)
        self.login("inspector").post(f"/squawks/quick/{sq['id']}/repair_confirm", data={"action": "self_repair"})
        self.login("tech").post(f"/squawks/quick/{sq['id']}/reopen")
        self.assertIsNotNone(self.plane()["grounded_at"])
