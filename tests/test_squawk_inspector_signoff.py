"""QA finding ux-squawk-inspector-signoff: an Inspector could never reach the
Confirm button for a squawk repair, because the only pages it lived on
(Squawks, a plane's page) said "you don't have access". Inspectors can now
open both pages read-only, and get their own "Repairs To Sign Off" box on
the shop dashboard (see get_squawks_awaiting_confirm in app.py)."""
from harness import OpsHubTestCase, seed_row
import db


class SquawkInspectorSignoffTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        self.tech_id = self.users["tech"]["id"]

    def make_ready_for_confirm(self):
        conn = db.get_db()
        squawk_id = seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Right brake dragging",
                              assigned_to=self.tech_id, acknowledged_at=db.now_iso(), acknowledged_by="Tech",
                              worker_acknowledged_at=db.now_iso(),
                              repair_confirm_requested_at=db.now_iso(), repair_confirm_requested_by="Tech")
        conn.commit()
        conn.close()
        return squawk_id

    def make_new_squawk(self):
        conn = db.get_db()
        squawk_id = seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Left brake soft")
        conn.commit()
        conn.close()
        return squawk_id

    def test_inspector_can_open_squawks_page(self):
        self.make_new_squawk()
        c = self.login("inspector")
        r = c.get("/squawks")
        self.assertEqual(r.status_code, 200)

    def test_inspector_can_open_asset_detail_page(self):
        c = self.login("inspector")
        r = c.get(f"/assets/{self.asset_id}")
        self.assertEqual(r.status_code, 200)

    def test_inspector_sees_sign_off_box_on_dashboard(self):
        self.make_ready_for_confirm()
        c = self.login("inspector")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Repair To Sign Off", body)
        self.assertIn("Sign off", body)
        self.assertIn("Send back", body)

    def test_tech_does_not_see_sign_off_box(self):
        self.make_ready_for_confirm()
        c = self.login("tech")
        body = c.get("/shop").get_data(as_text=True)
        self.assertNotIn("Repair To Sign Off", body)

    def test_inspector_sign_off_confirms_the_repair(self):
        squawk_id = self.make_ready_for_confirm()
        c = self.login("inspector")
        r = c.post(f"/squawks/quick/{squawk_id}/repair_confirm")
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM plane_squawks WHERE id = ?", (squawk_id,))
        self.assertIsNotNone(row["repaired_at"])

    def test_inspector_does_not_see_assign_controls_on_dashboard(self):
        # QA ux-shop-dash-by-role reshaped the Inspector's dashboard around
        # their own projects - the New Squawks box (nothing they can act on)
        # is hidden entirely now instead of showing read-only.
        self.make_new_squawk()
        c = self.login("inspector")
        body = c.get("/shop").get_data(as_text=True)
        self.assertNotIn("New Squawk", body)
        self.assertNotIn("I'll take it", body)

    def test_inspector_does_not_see_assign_controls_on_squawks_page(self):
        self.make_new_squawk()
        c = self.login("inspector")
        body = c.get("/squawks").get_data(as_text=True)
        self.assertNotIn("I'll take it", body)
        self.assertNotIn("Report an Issue", body)

    def test_admin_still_sees_assign_controls_and_sign_off_box(self):
        self.make_new_squawk()
        self.make_ready_for_confirm()
        c = self.login("shop_admin")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("I'll take it", body)
        self.assertIn("Repair To Sign Off", body)
