"""QA finding ux-squawk-my-list-dashboard: a tech's squawks used to vanish
from the dashboard the moment they hit "Got It", and marking one repaired
meant navigating to My Tasks. Both the dashboard's "My Squawks" box and My
Tasks now list every squawk assigned to the tech that isn't signed off yet
(see get_my_squawks in app.py), each with exactly one next-step button
(squawk_my_actions in templates/_squawk_macros.html)."""
from harness import OpsHubTestCase, seed_row
import db


class SquawkMyListDashboardTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        self.tech_id = self.users["tech"]["id"]

    def make_squawk(self, worker_acknowledged=False, confirm_requested=False, sent_back_note=None):
        conn = db.get_db()
        squawk_id = seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Left brake soft",
                              assigned_to=self.tech_id, acknowledged_at=db.now_iso(), acknowledged_by="Admin",
                              worker_acknowledged_at=db.now_iso() if worker_acknowledged else None,
                              repair_confirm_requested_at=db.now_iso() if confirm_requested else None,
                              repair_confirm_requested_by="Tech" if confirm_requested else None,
                              sent_back_note=sent_back_note)
        conn.commit()
        conn.close()
        return squawk_id

    def test_dashboard_shows_my_squawks_box_not_yet_accepted(self):
        self.make_squawk(worker_acknowledged=False)
        c = self.login("tech")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("My Squawks", body)
        self.assertIn("Start work", body)

    def test_dashboard_still_shows_squawk_once_accepted_but_not_repaired(self):
        # This is the actual bug: it used to disappear from the dashboard
        # once the tech hit "Got It" / Start work.
        self.make_squawk(worker_acknowledged=True)
        c = self.login("tech")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("My Squawks", body)
        self.assertIn("Done - send to inspector", body)

    def test_dashboard_shows_with_inspector_while_awaiting_confirm(self):
        self.make_squawk(worker_acknowledged=True, confirm_requested=True)
        c = self.login("tech")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("With inspector (tap to undo)", body)

    def test_my_squawks_box_links_to_my_tasks(self):
        self.make_squawk(worker_acknowledged=False)
        c = self.login("tech")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn(f'href="/my-tasks"', body)

    def test_sent_back_note_shows_on_dashboard(self):
        self.make_squawk(worker_acknowledged=True, sent_back_note="Brake still soft, check the line")
        c = self.login("tech")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Sent back: Brake still soft, check the line", body)

    def test_inspector_send_back_saves_note_and_tech_sees_it(self):
        squawk_id = self.make_squawk(worker_acknowledged=True, confirm_requested=True)
        c = self.login("inspector")
        r = c.post(f"/squawks/quick/{squawk_id}/repair_confirm", data={"action": "send_back", "note": "Not fixed yet"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM plane_squawks WHERE id = ?", (squawk_id,))
        self.assertEqual(row["sent_back_note"], "Not fixed yet")
        self.assertIsNone(row["repair_confirm_requested_at"])
        c2 = self.login("tech")
        body = c2.get("/shop").get_data(as_text=True)
        self.assertIn("Sent back: Not fixed yet", body)

    def test_note_clears_once_tech_sends_it_back_to_inspector_again(self):
        squawk_id = self.make_squawk(worker_acknowledged=True, sent_back_note="Old reason")
        c = self.login("tech")
        r = c.post(f"/squawks/quick/{squawk_id}/repair")
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM plane_squawks WHERE id = ?", (squawk_id,))
        self.assertIsNone(row["sent_back_note"])
        self.assertIsNotNone(row["repair_confirm_requested_at"])

    def test_my_tasks_uses_same_rows_and_buttons_as_dashboard(self):
        self.make_squawk(worker_acknowledged=True)
        c = self.login("tech")
        body = c.get("/my-tasks").get_data(as_text=True)
        self.assertIn("Done - send to inspector", body)
        self.assertIn("Reported", body)
        self.assertIn("Working", body)
