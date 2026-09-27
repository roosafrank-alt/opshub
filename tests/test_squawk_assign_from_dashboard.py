"""QA finding ux-squawk-assign-from-dashboard: a brand-new squawk's only
action used to be a plain Acknowledge, separate from picking who fixes it -
two trips instead of one. Now picking a name (or "I'll take it") acknowledges
and assigns in the same request (see squawk_acknowledge/_apply_squawk_assignment
in app.py, and squawk_new_actions in templates/_squawk_macros.html)."""
from harness import OpsHubTestCase, seed_row
import db


class SquawkAssignFromDashboardTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        self.tech_id = self.users["tech"]["id"]

    def make_quick_squawk(self):
        conn = db.get_db()
        squawk_id = seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Left brake soft")
        conn.commit()
        conn.close()
        return squawk_id

    def get_squawk(self, squawk_id):
        return self.q1("SELECT * FROM plane_squawks WHERE id = ?", (squawk_id,))

    def test_picking_a_name_acknowledges_and_assigns_together(self):
        squawk_id = self.make_quick_squawk()
        c = self.login("shop_admin")
        r = c.post(f"/squawks/quick/{squawk_id}/acknowledge", data={"assigned_to": str(self.tech_id)})
        self.assertEqual(r.status_code, 302)
        row = self.get_squawk(squawk_id)
        self.assertIsNotNone(row["acknowledged_at"])
        self.assertEqual(row["assigned_to"], self.tech_id)

    def test_ill_take_it_assigns_to_self_and_acknowledges(self):
        squawk_id = self.make_quick_squawk()
        c = self.login("tech")
        r = c.post(f"/squawks/quick/{squawk_id}/acknowledge", data={"assigned_to": str(self.tech_id)})
        self.assertEqual(r.status_code, 302)
        row = self.get_squawk(squawk_id)
        self.assertIsNotNone(row["acknowledged_at"])
        self.assertEqual(row["assigned_to"], self.tech_id)

    def test_acknowledge_without_a_name_still_works(self):
        # Defensive: the form always posts an assigned_to now, but the route
        # itself should still tolerate a blank one.
        squawk_id = self.make_quick_squawk()
        c = self.login("shop_admin")
        r = c.post(f"/squawks/quick/{squawk_id}/acknowledge", data={"assigned_to": ""})
        self.assertEqual(r.status_code, 302)
        row = self.get_squawk(squawk_id)
        self.assertIsNotNone(row["acknowledged_at"])
        self.assertIsNone(row["assigned_to"])

    def test_dashboard_shows_assign_to_and_ill_take_it_not_plain_acknowledge(self):
        self.make_quick_squawk()
        c = self.login("shop_admin")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Pick Who Fixes Them", body)
        self.assertIn("I'll take it", body)
        self.assertIn("Assign to...", body)
        self.assertNotIn(">Acknowledge<", body)

    def test_squawks_page_new_squawk_has_no_separate_acknowledge_or_mark_repaired(self):
        self.make_quick_squawk()
        c = self.login("shop_admin")
        body = c.get("/squawks").get_data(as_text=True)
        self.assertIn("I'll take it", body)
        self.assertNotIn(">Acknowledge<", body)
        self.assertNotIn(">Mark Repaired<", body)

    def test_squawks_page_step_pills_show_reported_as_current_for_new_squawk(self):
        self.make_quick_squawk()
        c = self.login("shop_admin")
        body = c.get("/squawks").get_data(as_text=True)
        self.assertIn("bg-primary text-white\">Reported", body)

    def test_asset_detail_new_squawk_has_assign_actions(self):
        self.make_quick_squawk()
        c = self.login("shop_admin")
        body = c.get(f"/assets/{self.asset_id}").get_data(as_text=True)
        self.assertIn("Pick Who Fixes Them", body)
        self.assertIn("I'll take it", body)
