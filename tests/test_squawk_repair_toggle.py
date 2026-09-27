"""Idea 'squalks' (revision 2): a tech's Mark Repaired button on My Tasks
used to always request repair confirmation, with no way back short of an
Inspector Sending it back. Frank asked for the same button to grey out once
pressed and, if pressed again, undo the request - so squawk_repair now
toggles instead of only ever setting the request."""
from harness import OpsHubTestCase, seed_row
import db


class SquawkRepairToggleTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        self.tech_id = self.users["tech"]["id"]

    def make_quick_squawk(self, assigned_to=None, worker_acknowledged=False):
        conn = db.get_db()
        squawk_id = seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Left brake soft",
                              assigned_to=assigned_to,
                              worker_acknowledged_at=db.now_iso() if worker_acknowledged else None)
        conn.commit()
        conn.close()
        return squawk_id

    def get_squawk(self, squawk_id):
        return self.q1("SELECT * FROM plane_squawks WHERE id = ?", (squawk_id,))

    def test_mark_repaired_requests_confirmation(self):
        squawk_id = self.make_quick_squawk(assigned_to=self.tech_id, worker_acknowledged=True)
        c = self.login("tech")
        r = c.post(f"/squawks/quick/{squawk_id}/repair")
        self.assertEqual(r.status_code, 302)
        row = self.get_squawk(squawk_id)
        self.assertIsNotNone(row["repair_confirm_requested_at"])
        self.assertIsNone(row["repaired_at"])

    def test_pressing_it_again_undoes_the_request(self):
        squawk_id = self.make_quick_squawk(assigned_to=self.tech_id, worker_acknowledged=True)
        c = self.login("tech")
        c.post(f"/squawks/quick/{squawk_id}/repair")
        self.assertIsNotNone(self.get_squawk(squawk_id)["repair_confirm_requested_at"])

        r = c.post(f"/squawks/quick/{squawk_id}/repair")
        self.assertEqual(r.status_code, 302)
        row = self.get_squawk(squawk_id)
        self.assertIsNone(row["repair_confirm_requested_at"])
        self.assertIsNone(row["repaired_at"])

    def test_my_tasks_shows_with_inspector_and_greyed_button_while_awaiting_confirmation(self):
        squawk_id = self.make_quick_squawk(assigned_to=self.tech_id, worker_acknowledged=True)
        c = self.login("tech")
        c.post(f"/squawks/quick/{squawk_id}/repair")
        r = c.get("/my-tasks")
        body = r.get_data(as_text=True)
        self.assertIn("With inspector (tap to undo)", body)
        self.assertIn("btn-secondary", body)
        self.assertNotIn("Done - send to inspector", body)

    def test_my_tasks_shows_done_send_to_inspector_before_any_request(self):
        squawk_id = self.make_quick_squawk(assigned_to=self.tech_id, worker_acknowledged=True)
        c = self.login("tech")
        r = c.get("/my-tasks")
        body = r.get_data(as_text=True)
        self.assertIn("Done - send to inspector", body)
        self.assertNotIn("With inspector (tap to undo)", body)

    def test_undo_still_leaves_it_acknowledged(self):
        squawk_id = self.make_quick_squawk(assigned_to=self.tech_id, worker_acknowledged=True)
        c = self.login("tech")
        c.post(f"/squawks/quick/{squawk_id}/repair")
        c.post(f"/squawks/quick/{squawk_id}/repair")
        row = self.get_squawk(squawk_id)
        self.assertIsNotNone(row["acknowledged_at"])
