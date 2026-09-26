"""Job Sheet scan codes (idea "Job sheet"): the printable job sheet shows the
plane, project and each sub-area as QR codes."""
from harness import OpsHubTestCase


class JobSheetCodesTest(OpsHubTestCase):
    def test_job_sheet_shows_plane_project_and_sub_area_codes(self):
        asset = self.make_asset("N555")
        pid = self.make_project(name="Annual - N555", asset_id=asset)
        self.exec("INSERT INTO project_sections (project_id, name) VALUES (?, 'Plugs')", (pid,))
        code = self.q1("SELECT code FROM projects WHERE id = ?", (pid,))["code"]
        r = self.login("tech").get(f"/projects/{pid}/checklist")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn(f'data-code="{code}"', html)
        self.assertIn(f'data-code="TASK-{code}::Plugs"', html)
        self.assertIn(f'/assets/{asset}"', html)

    def test_job_sheet_without_a_plane_has_no_plane_code(self):
        pid = self.make_project(name="Shop job")
        html = self.login("tech").get(f"/projects/{pid}/checklist").get_data(as_text=True)
        self.assertNotIn(">Plane<", html)
