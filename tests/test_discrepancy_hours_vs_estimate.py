"""Idea "hours worked vs the estimate": a Discrepancy with an estimate shows a
pill ('2.1 of 3.0 h'), a bar, and a red line once timed hours pass the estimate."""
from harness import OpsHubTestCase
import db


class HoursVsEstimateTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        self.project_id = self.make_project(name="Annual - N999TT", asset_id=self.asset_id)
        self.section_id = self.exec(
            "INSERT INTO project_sections (project_id, name, created_at, est_hours, est_cost) VALUES (?,?,?,?,?)",
            (self.project_id, "Brakes", db.now_iso(), 3.0, 372.0))
        self.laborer = self.exec("INSERT INTO laborers (name, code, rate) VALUES (?,?,?)", ("Tech T", "LABOR-HVE1", 124.0))

    def work(self, hours):
        self.exec("INSERT INTO labor_sessions (laborer_id, project_id, section, started_at, ended_at, hours, rate, cost) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (self.laborer, self.project_id, "Brakes", db.now_iso(), db.now_iso(), hours, 124.0, round(hours * 124.0, 2)))

    def page(self, role="shop_admin"):
        return self.login(role).get(f"/projects/{self.project_id}").get_data(as_text=True)

    def test_under_estimate_grey_no_red_line(self):
        self.work(1.0)
        body = self.page()
        self.assertIn("1.0 of 3.0 h", body)
        self.assertIn("text-bg-secondary", body)
        self.assertNotIn("over the estimate", body)

    def test_yellow_from_80_percent(self):
        self.work(2.5)
        body = self.page()
        self.assertIn("2.5 of 3.0 h", body)
        self.assertIn("text-bg-warning", body)

    def test_red_once_over_with_dollars(self):
        self.work(3.4)
        body = self.page()
        self.assertIn("3.4 of 3.0 h", body)
        self.assertIn("0.4 h over the estimate", body)
        self.assertIn("worked $421.60 vs $372.00 quoted", body)
        self.assertIn("call the owner before going further", body)

    def test_no_estimate_looks_as_before(self):
        self.exec("UPDATE project_sections SET est_hours = NULL, est_cost = NULL WHERE id = ?", (self.section_id,))
        self.work(5.0)
        body = self.page()
        self.assertNotIn("data-change=\"HRS-EST\"", body)
        self.assertNotIn("over the estimate", body)
