"""Sub-area QR codes (idea "scanning": auto QR per sub area + print icon)."""
from harness import OpsHubTestCase, SIDE_EFFECTS


class SectionQrTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.pid = self.make_project(name="Annual - N1")
        self.sid = self.exec("INSERT INTO project_sections (project_id, name) VALUES (?, 'Plugs')", (self.pid,))
        self.code = self.q1("SELECT code FROM projects WHERE id = ?", (self.pid,))["code"]

    def test_project_page_links_each_sub_area_code(self):
        html = self.login("tech").get(f"/projects/{self.pid}").get_data(as_text=True)
        self.assertIn(f"/projects/{self.pid}/sections/{self.sid}/label", html)

    def test_label_page_has_the_task_code(self):
        r = self.login("tech").get(f"/projects/{self.pid}/sections/{self.sid}/label")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f'data-code="TASK-{self.code}::Plugs"', r.get_data(as_text=True))

    def test_print_to_label_printer(self):
        self.login("tech").post(f"/projects/{self.pid}/sections/{self.sid}/print-label")
        self.assertTrue(any(k == "label" for k, *_ in SIDE_EFFECTS))

    def test_wrong_project_or_missing_section_is_404(self):
        other = self.make_project(name="Other")
        c = self.login("tech")
        self.assertEqual(c.get(f"/projects/{other}/sections/{self.sid}/label").status_code, 404)
        self.assertEqual(c.get(f"/projects/{self.pid}/sections/9999/label").status_code, 404)
