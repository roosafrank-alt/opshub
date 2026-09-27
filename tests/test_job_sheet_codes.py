"""Job Sheet scan codes (idea "Job sheet"): the printable job sheet shows the
project and each sub-area as QR codes. Revision 3: only the project's own
code sits in the header (no plane code), to keep the header simple."""
from harness import OpsHubTestCase


class JobSheetCodesTest(OpsHubTestCase):
    def test_job_sheet_shows_project_and_sub_area_codes(self):
        asset = self.make_asset("N555")
        pid = self.make_project(name="Annual - N555", asset_id=asset)
        self.exec("INSERT INTO project_sections (project_id, name) VALUES (?, 'Plugs')", (pid,))
        code = self.q1("SELECT code FROM projects WHERE id = ?", (pid,))["code"]
        r = self.login("tech").get(f"/projects/{pid}/checklist")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn(f'data-code="{code}"', html)
        self.assertIn(f'data-code="TASK-{code}::Plugs"', html)

    def test_job_sheet_never_shows_a_plane_code(self):
        """Revision 3: Frank asked for only the project code up top, not the
        plane's, even when the project has a plane attached."""
        asset = self.make_asset("N555")
        pid = self.make_project(name="Annual - N555", asset_id=asset)
        html = self.login("tech").get(f"/projects/{pid}/checklist").get_data(as_text=True)
        self.assertNotIn(">Plane<", html)
        self.assertNotIn(f'/assets/{asset}"', html)

    def test_project_details_come_before_sub_areas_each_in_their_own_row(self):
        """idea "Job sheet" revision 2: project details up top, then each
        sub area in its own dedicated row/section below, not mixed together
        in one strip of codes."""
        pid = self.make_project(name="Annual - N777")
        self.exec("INSERT INTO project_sections (project_id, name) VALUES (?, 'Plugs')", (pid,))
        self.exec("INSERT INTO project_sections (project_id, name) VALUES (?, 'Brakes')", (pid,))
        code = self.q1("SELECT code FROM projects WHERE id = ?", (pid,))["code"]
        html = self.login("tech").get(f"/projects/{pid}/checklist").get_data(as_text=True)
        # The project's own name/header comes before the Sub Areas section.
        self.assertLess(html.index("Annual - N777"), html.index("Sub Areas"))
        # Each sub area name appears in its own row, in order, after the header.
        self.assertLess(html.index("Sub Areas"), html.index("Plugs"))
        self.assertLess(html.index("Plugs"), html.index("Brakes"))
        self.assertIn(f'data-code="TASK-{code}::Plugs"', html)
        self.assertIn(f'data-code="TASK-{code}::Brakes"', html)

    def test_job_sheet_without_sub_areas_has_no_sub_areas_section(self):
        pid = self.make_project(name="Shop job, no sub areas")
        html = self.login("tech").get(f"/projects/{pid}/checklist").get_data(as_text=True)
        self.assertNotIn("Sub Areas", html)
