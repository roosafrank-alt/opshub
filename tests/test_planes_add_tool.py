"""Planes page: Add Tool, and a tool switched off for the school stays listed."""
from harness import OpsHubTestCase


class PlanesAddToolTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("master")

    def row(self, tag):
        return self.q1("SELECT * FROM assets WHERE tag=?", (tag,))

    def test_add_tool_and_keep_when_switched_off(self):
        self.client.post("/flight/planes/tool/new", data={"kind": "simulator", "tag": "ZSIM", "sim_rate": "60"})
        self.client.post("/flight/planes/tool/new", data={"kind": "other", "tag": "ZOTHER"})
        self.assertEqual(self.row("ZSIM")["is_simulator"], 1)
        self.assertEqual(self.row("ZOTHER")["is_simulator"], 0)
        self.assertEqual(self.row("ZOTHER")["is_flight_asset"], 1)
        sim_id = self.row("ZSIM")["id"]
        # unticking "Used by Flight School" takes it off the Schedule but not off the list
        self.client.post(f"/flight/planes/simulator/{sim_id}/edit", data={"tag": "ZSIM", "sim_rate": "60"})
        self.assertEqual(self.row("ZSIM")["is_flight_asset"], 0)
        page = self.client.get("/flight/planes").get_data(as_text=True)
        self.assertIn("ZSIM", page)
        self.assertIn("Not used by Flight School", page)
        self.client.post(f"/flight/planes/{sim_id}/school-on")
        self.assertEqual(self.row("ZSIM")["is_flight_asset"], 1)

    def test_maintenance_only_aircraft_not_listed(self):
        self.make_asset("ZMAINT")
        page = self.client.get("/flight/planes").get_data(as_text=True)
        self.assertNotIn("ZMAINT", page)
