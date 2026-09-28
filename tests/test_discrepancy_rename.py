"""Idea "Project": Frank's shop uses aviation maintenance terms, so "Sub
Area" is now "Discrepancy" (plural "Discrepancies") everywhere it shows on
screen, and the project page's "Parts Used" section header is now
"Discrepancy List". Only what a user actually sees changed - the
project_sections table, routes and internal naming are untouched."""
from harness import OpsHubTestCase
import db


class DiscrepancyRenameTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.project_id = self.make_project()

    def test_project_page_header_is_discrepancy_list(self):
        c = self.login("shop_admin")
        body = c.get(f"/projects/{self.project_id}").get_data(as_text=True)
        self.assertIn("Discrepancy List", body)
        self.assertNotIn("Parts Used", body)

    def test_add_discrepancy_button_and_flash_message(self):
        c = self.login("shop_admin")
        body = c.get(f"/projects/{self.project_id}").get_data(as_text=True)
        self.assertIn("Add Discrepancy", body)
        self.assertNotIn("Add Sub Area", body)
        r = c.post(f"/projects/{self.project_id}/add_section", data={"name": "Brakes"}, follow_redirects=True)
        self.assertIn("Discrepancy &#39;Brakes&#39; added.", r.get_data(as_text=True))

    def test_job_sheet_checklist_header_is_discrepancies(self):
        self.exec("INSERT INTO project_sections (project_id, name) VALUES (?, 'Plugs')", (self.project_id,))
        c = self.login("tech")
        body = c.get(f"/projects/{self.project_id}/checklist").get_data(as_text=True)
        self.assertIn("Discrepancies", body)
        self.assertNotIn("Sub Areas", body)

    def test_dashboard_awaiting_confirmation_box_says_discrepancy(self):
        self.exec(
            "INSERT INTO project_sections (project_id, name, created_at, confirm_requested_at, confirm_requested_by) "
            "VALUES (?,?,?,?,?)",
            (self.project_id, "Brakes", db.now_iso(), db.now_iso(), "Tech"))
        c = self.login("shop_admin")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Discrepancy Awaiting Confirmation", body)
