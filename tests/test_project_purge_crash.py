"""QA fix qa-project-purge-crash: Delete Forever (and Empty Trash) on a job
with a logbook entry or an extra-repair ("found") item used to hit a
FOREIGN KEY constraint (a 500 page) instead of deleting the job, since
neither reference was cleared first. Both are kept - detached from the
purged job but still tied to the plane by asset_id - instead of blocking
the delete or being silently lost.
"""
from harness import OpsHubTestCase, seed_row
import db


class ProjectPurgeCrashTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N12345")
        self.project = self.make_project(name="Annual - N12345", asset_id=self.asset)

    def add_logbook_entry(self):
        conn = db.get_db()
        eid = seed_row(conn, "logbook_entries", asset_id=self.asset, project_id=self.project,
                       log_type="airframe", created_at=db.now_iso())
        conn.commit()
        conn.close()
        return eid

    def add_found_item(self):
        return self.exec(
            "INSERT INTO found_items (project_id, asset_id, description, status, created_at) "
            "VALUES (?, ?, 'Cracked exhaust stack', 'waiting', ?)",
            (self.project, self.asset, db.now_iso()))

    def trash_it(self):
        self.exec("UPDATE projects SET deleted_at = ? WHERE id = ?", (db.now_iso(), self.project))

    def test_purge_with_a_logbook_entry_does_not_crash(self):
        eid = self.add_logbook_entry()
        self.trash_it()
        r = self.login("shop_admin").post(f"/projects/{self.project}/purge")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.q1("SELECT id FROM projects WHERE id = ?", (self.project,)))
        entry = self.q1("SELECT project_id, asset_id FROM logbook_entries WHERE id = ?", (eid,))
        self.assertIsNotNone(entry, "the logbook entry should not be deleted")
        self.assertIsNone(entry["project_id"])
        self.assertEqual(entry["asset_id"], self.asset)

    def test_purge_with_a_found_item_does_not_crash(self):
        fid = self.add_found_item()
        self.trash_it()
        r = self.login("shop_admin").post(f"/projects/{self.project}/purge")
        self.assertEqual(r.status_code, 302)
        item = self.q1("SELECT project_id, asset_id, description FROM found_items WHERE id = ?", (fid,))
        self.assertIsNotNone(item, "the found item should not be deleted")
        self.assertIsNone(item["project_id"])
        self.assertEqual(item["asset_id"], self.asset)
        self.assertEqual(item["description"], "Cracked exhaust stack")

    def test_purge_with_both_shows_the_kept_on_record_message(self):
        self.add_logbook_entry()
        self.add_found_item()
        self.trash_it()
        r = self.login("shop_admin").post(f"/projects/{self.project}/purge", follow_redirects=True)
        self.assertIn("kept on the plane&#39;s record", r.get_data(as_text=True))

    def test_empty_trash_also_does_not_crash_on_these(self):
        self.add_logbook_entry()
        self.add_found_item()
        self.trash_it()
        r = self.login("shop_admin").post("/trash/empty")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.q1("SELECT id FROM projects WHERE id = ?", (self.project,)))

    def test_new_found_items_already_carry_asset_id(self):
        r = self.login("tech").post(f"/projects/{self.project}/found-items", data=dict(
            description="Loose wire", est_parts="0", est_labor_hours="0", est_labor_rate="0"))
        self.assertEqual(r.status_code, 302)
        item = self.q("SELECT asset_id FROM found_items ORDER BY id DESC")[0]
        self.assertEqual(item["asset_id"], self.asset)
