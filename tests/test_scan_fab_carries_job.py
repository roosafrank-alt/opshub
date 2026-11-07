"""The round Scan button on a job's page opens Scan on that job; elsewhere it
is unchanged."""
from harness import OpsHubTestCase


class ScanFabJobTests(OpsHubTestCase):
    def test_job_page_button_carries_job(self):
        pid = self.make_project("Annual - N12345")
        code = self.q("SELECT code FROM projects WHERE id=?", (pid,))[0]["code"]
        body = self.login("tech").get(f"/projects/{pid}").get_data(as_text=True)
        self.assertIn(f"/scan?cam=1&amp;autoscan={code}\" class=\"btn btn-warning rounded-circle", body)

    def test_other_page_button_plain(self):
        body = self.login("tech").get("/projects").get_data(as_text=True)
        self.assertIn("/scan?cam=1\" class=\"btn btn-warning rounded-circle", body)
