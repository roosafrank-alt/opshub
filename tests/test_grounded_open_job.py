"""QA finding feat-grounded-open-job: a grounded plane has an 'Open shop job'
button; completing the job leaves the plane Down until Return to service."""
import db
from harness import OpsHubTestCase


class GroundedOpenJob(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.exec("INSERT INTO assets (tag, name, is_flight_asset, schedule_order, solo_allowed, created_at, updated_at) "
                               "VALUES ('N81PA','N81PA',1,10,1,?,?)", (db.now_iso(), db.now_iso()))
        self.login("master").post(f"/flight/planes/{self.plane}/ground", data={"reason": "left brake soft"})

    def _asset(self):
        return self.q1("SELECT * FROM assets WHERE id = ?", (self.plane,))

    def test_button_shows_only_on_grounded_plane(self):
        html = self.login("master").get("/flight/planes").get_data(as_text=True)
        self.assertIn("Open shop job", html)
        self.client.post(f"/flight/planes/{self.plane}/return-to-service")
        self.assertNotIn("Open shop job", self.client.get("/flight/planes").get_data(as_text=True))

    def test_open_job_names_it_from_reason_and_links_it(self):
        r = self.login("master").post(f"/flight/planes/{self.plane}/open-job")
        self.assertEqual(r.status_code, 302)
        p = self.q1("SELECT * FROM projects WHERE asset_id = ?", (self.plane,))
        self.assertIn("left brake soft", p["name"])
        self.assertEqual(p["status"], "active")
        self.assertEqual(self._asset()["grounded_project_id"], p["id"])
        # pressing again goes to the same job instead of making a second one
        self.client.post(f"/flight/planes/{self.plane}/open-job")
        self.assertEqual(len(self.q("SELECT id FROM projects WHERE asset_id = ?", (self.plane,))), 1)
        html = self.client.get("/flight/planes").get_data(as_text=True)
        self.assertIn(p["code"], html)
        self.assertNotIn("Open shop job", html)

    def test_completed_job_keeps_plane_down_until_return(self):
        self.login("master").post(f"/flight/planes/{self.plane}/open-job")
        pid = self.q1("SELECT id FROM projects WHERE asset_id = ?", (self.plane,))["id"]
        self.exec("UPDATE projects SET status = 'completed', completed_at = ? WHERE id = ?", (db.now_iso(), pid))
        self.assertTrue(self._asset()["grounded_at"])
        html = self.client.get("/flight/planes").get_data(as_text=True)
        self.assertIn("Press Return to service after the test flight", html)
        self.client.post(f"/flight/planes/{self.plane}/return-to-service")
        a = self._asset()
        self.assertIsNone(a["grounded_at"])
        self.assertIsNone(a["grounded_project_id"])

    def test_not_grounded_plane_gets_no_job_and_students_cannot(self):
        other = self.exec("INSERT INTO assets (tag, name, is_flight_asset, schedule_order, created_at, updated_at) "
                          "VALUES ('N2','N2',1,20,?,?)", (db.now_iso(), db.now_iso()))
        self.login("master").post(f"/flight/planes/{other}/open-job")
        self.assertEqual(len(self.q("SELECT id FROM projects WHERE asset_id = ?", (other,))), 0)
        self.login("flight_student").post(f"/flight/planes/{self.plane}/open-job")
        self.assertEqual(len(self.q("SELECT id FROM projects WHERE asset_id = ?", (self.plane,))), 0)
