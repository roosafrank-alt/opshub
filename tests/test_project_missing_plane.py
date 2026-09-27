"""QA fix qa-project-missing-plane: saving New Project or Edit Project with
a plane that's been permanently deleted (or an old form resubmitted) used
to hit a FOREIGN KEY constraint - an error page that lost the typed-in job
details. It now comes back with a plain red message and the project name
kept, ready to pick another aircraft (or none) and save again.
"""
from harness import OpsHubTestCase
import db


class ProjectMissingPlaneTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.gone_asset = self.make_asset("N55512")
        self.exec("DELETE FROM assets WHERE id = ?", (self.gone_asset,))

    def test_new_project_with_a_gone_plane_shows_a_message_not_an_error(self):
        before = len(self.q("SELECT id FROM projects"))
        r = self.login("shop_admin").post("/projects/new", data=dict(
            name="Annual - N55512", asset_id=str(self.gone_asset)), follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("That aircraft is no longer on file. Pick another aircraft (or none) and save again.", html)
        self.assertIn("Annual - N55512", html)
        self.assertEqual(len(self.q("SELECT id FROM projects")), before)

    def test_new_project_with_a_soft_deleted_plane_also_shows_the_message(self):
        trashed = self.make_asset("N77777")
        self.exec("UPDATE assets SET deleted_at = ? WHERE id = ?", (db.now_iso(), trashed))
        r = self.login("shop_admin").post("/projects/new", data=dict(
            name="Oil Change - N77777", asset_id=str(trashed)), follow_redirects=True)
        self.assertIn("That aircraft is no longer on file", r.get_data(as_text=True))

    def test_edit_project_with_a_gone_plane_shows_a_message_and_keeps_other_edits(self):
        pid = self.make_project(name="Annual - N123")
        r = self.login("shop_admin").post(f"/projects/{pid}/edit", data=dict(
            name="Annual - N123 (redo)", asset_id=str(self.gone_asset),
            description="Rebuilt after gear-up landing"), follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("That aircraft is no longer on file. Pick another aircraft (or none) and save again.", html)
        self.assertIn("Annual - N123 (redo)", html)
        self.assertIn("Rebuilt after gear-up landing", html)
        row = self.q1("SELECT name, asset_id FROM projects WHERE id = ?", (pid,))
        self.assertEqual(row["name"], "Annual - N123")
        self.assertIsNone(row["asset_id"])

    def test_saving_with_no_plane_at_all_still_works(self):
        r = self.login("shop_admin").post("/projects/new", data=dict(name="Shop tool repair"))
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(self.q1("SELECT id FROM projects WHERE name = 'Shop tool repair'"))
