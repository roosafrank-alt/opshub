"""Idea "techs status": Frank liked the step-pill status bubbles (Reported/
Assigned/Working/Inspection/Done) and asked for the tech's own next-step
button to say "Accepted" (Assigned -> Working) and then "Mark as Repaired"
(Working -> Inspection) - the mechanics (moving through the steps, showing
up in the Inspector's "Repairs To Sign Off" box on their dashboard for
sign-off) already worked from earlier fixes tonight; this just renames the
two buttons to match."""
from harness import OpsHubTestCase
import db


class SquawkAcceptedMarkRepairedWordingTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        self.tech_id = self.users["tech"]["id"]

    def make_squawk(self, **extra):
        cols = dict(asset_id=self.asset_id, notes="Left brake soft", reported_at=db.now_iso())
        cols.update(extra)
        names = ",".join(cols)
        marks = ",".join("?" * len(cols))
        return self.exec(f"INSERT INTO plane_squawks ({names}) VALUES ({marks})", list(cols.values()))

    def test_assigned_squawk_shows_accepted_button_on_my_tasks(self):
        self.make_squawk(assigned_to=self.tech_id, acknowledged_at=db.now_iso(), acknowledged_by="Admin")
        c = self.login("tech")
        body = c.get("/my-tasks").get_data(as_text=True)
        self.assertIn("Accepted", body)
        self.assertNotIn("Start work", body)

    def test_clicking_accepted_moves_it_to_working(self):
        squawk_id = self.make_squawk(assigned_to=self.tech_id, acknowledged_at=db.now_iso(), acknowledged_by="Admin")
        c = self.login("tech")
        r = c.post(f"/squawks/quick/{squawk_id}/worker_ack")
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM plane_squawks WHERE id = ?", (squawk_id,))
        self.assertIsNotNone(row["worker_acknowledged_at"])

    def test_working_squawk_shows_mark_as_repaired_button(self):
        self.make_squawk(assigned_to=self.tech_id, acknowledged_at=db.now_iso(), acknowledged_by="Admin",
                          worker_acknowledged_at=db.now_iso())
        c = self.login("tech")
        body = c.get("/my-tasks").get_data(as_text=True)
        self.assertIn("Mark as Repaired", body)
        self.assertNotIn("Done - send to inspector", body)

    def test_clicking_mark_as_repaired_notifies_the_inspector_dashboard(self):
        squawk_id = self.make_squawk(assigned_to=self.tech_id, acknowledged_at=db.now_iso(), acknowledged_by="Admin",
                                     worker_acknowledged_at=db.now_iso())
        c = self.login("tech")
        r = c.post(f"/squawks/quick/{squawk_id}/repair")
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM plane_squawks WHERE id = ?", (squawk_id,))
        self.assertIsNotNone(row["repair_confirm_requested_at"])
        # Shows up on the Inspector's own dashboard, their "task spot", for sign-off.
        body = self.login("inspector").get("/shop").get_data(as_text=True)
        self.assertIn("Repair To Sign Off", body)
        self.assertIn("Sign off", body)
