"""QA finding ux-squawk-page-steps: the Squawks page used to open on a long
explanation and a report form, then 3 lists that each mixed several steps
together with a different mix of buttons. It now opens with step filters
(New/Assigned/Working/Inspection/Done) and one list, filtered to a step,
with exactly one next-step control per squawk (see squawk_step_index in
app.py and squawk_step_action in templates/_squawk_macros.html)."""
from harness import OpsHubTestCase, seed_row
import db


class SquawkPageStepsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        self.tech_id = self.users["tech"]["id"]

    def make_squawk(self, notes="Left brake soft", **kw):
        conn = db.get_db()
        squawk_id = seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes=notes, **kw)
        conn.commit()
        conn.close()
        return squawk_id

    def test_page_defaults_to_new_step_when_new_squawks_exist(self):
        self.make_squawk()
        c = self.login("shop_admin")
        body = c.get("/squawks").get_data(as_text=True)
        self.assertIn("New - pick who fixes it (1)", body)
        self.assertIn("I'll take it", body)

    def test_step_counts_shown_on_filter_row(self):
        self.make_squawk(notes="New one")
        self.make_squawk(notes="Working one", assigned_to=self.tech_id,
                          acknowledged_at=db.now_iso(), acknowledged_by="Admin",
                          worker_acknowledged_at=db.now_iso())
        c = self.login("shop_admin")
        body = c.get("/squawks").get_data(as_text=True)
        self.assertIn("New 1", body)
        self.assertIn("Working 1", body)

    def test_step_query_param_filters_the_list(self):
        self.make_squawk(notes="New one")
        self.make_squawk(notes="Working one", assigned_to=self.tech_id,
                          acknowledged_at=db.now_iso(), acknowledged_by="Admin",
                          worker_acknowledged_at=db.now_iso())
        c = self.login("shop_admin")
        body = c.get("/squawks?step=working").get_data(as_text=True)
        self.assertIn("Working one", body)
        self.assertNotIn("New one", body)

    def test_working_step_shows_done_send_to_inspector_button(self):
        self.make_squawk(assigned_to=self.tech_id, acknowledged_at=db.now_iso(),
                          acknowledged_by="Admin", worker_acknowledged_at=db.now_iso())
        c = self.login("shop_admin")
        body = c.get("/squawks?step=working").get_data(as_text=True)
        self.assertIn("Mark as Repaired", body)

    def test_assigned_step_shows_reassign_control(self):
        self.make_squawk(assigned_to=self.tech_id, acknowledged_at=db.now_iso(), acknowledged_by="Admin")
        c = self.login("shop_admin")
        body = c.get("/squawks?step=assigned").get_data(as_text=True)
        self.assertIn("Reassign", body)

    def test_inspection_step_shows_sign_off_for_inspector(self):
        self.make_squawk(assigned_to=self.tech_id, acknowledged_at=db.now_iso(), acknowledged_by="Admin",
                          worker_acknowledged_at=db.now_iso(), repair_confirm_requested_at=db.now_iso(),
                          repair_confirm_requested_by="Tech")
        c = self.login("inspector")
        body = c.get("/squawks?step=inspection").get_data(as_text=True)
        self.assertIn("Sign off", body)

    def test_report_form_is_collapsed_by_default(self):
        c = self.login("shop_admin")
        body = c.get("/squawks").get_data(as_text=True)
        self.assertIn('id="report-squawk-form" class="collapse card', body)

    def test_report_query_param_opens_the_form(self):
        c = self.login("shop_admin")
        body = c.get("/squawks?report=1").get_data(as_text=True)
        self.assertIn('id="report-squawk-form" class="collapse show card', body)

    def test_dashboard_has_report_a_squawk_button(self):
        c = self.login("shop_admin")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Report squawk", body)
        self.assertIn("report=1", body)

    def test_plane_todo_list_shows_next_step_button_not_checkbox(self):
        self.make_squawk(assigned_to=self.tech_id, acknowledged_at=db.now_iso(),
                          acknowledged_by="Admin", worker_acknowledged_at=db.now_iso())
        c = self.login("shop_admin")
        body = c.get(f"/assets/{self.asset_id}").get_data(as_text=True)
        self.assertIn("Mark as Repaired", body)
        self.assertNotIn("bi-square fs-5 text-warning", body)
