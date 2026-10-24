"""QA fix qa-project-purge-erases-pay: Delete Forever on a job with hours
clocked or parts taken used to silently erase a worker's pay and a part's
usage history. Now the admin has to say what happens to each: parts
returned to stock (or kept as used, history annotated) and hours removed
from pay (or transferred to General Shop time, unchanged). A job with
neither is unaffected - same simple delete as before.
"""
from harness import OpsHubTestCase, seed_row
import db


class ProjectPurgeErasesPayTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.part = self.make_part(qty=10)
        self.project = self.make_project()
        self.laborer_id = self.exec(
            "INSERT INTO laborers (name, code, rate, active, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            ("Joe Tech", "LABOR-JOE00001", 32, 1, db.now_iso(), db.now_iso()))

    def take_parts(self, qty=3):
        self.login("tech")
        self.scan("PART-001", "out", qty, self.project, section="Brakes")

    def clock_hours(self, hours=4.0, rate=32):
        conn = db.get_db()
        seed_row(conn, "labor_sessions", laborer_id=self.laborer_id, project_id=self.project,
                 section="Engine", started_at="2026-09-24 08:00:00", ended_at="2026-09-24 12:00:00",
                 hours=hours, rate=rate, cost=hours * rate)
        conn.commit()
        conn.close()

    def trash_it(self):
        self.exec("UPDATE projects SET deleted_at = ? WHERE id = ?", (db.now_iso(), self.project))

    # --- no records: unchanged simple delete --------------------------------
    def test_job_with_no_records_just_deletes(self):
        self.trash_it()
        r = self.login("shop_admin").post(f"/projects/{self.project}/purge")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.q1("SELECT id FROM projects WHERE id = ?", (self.project,)))

    # --- parts ---------------------------------------------------------------
    def test_purge_requires_a_parts_choice_when_parts_were_taken(self):
        self.take_parts(3)
        self.trash_it()
        before = self.qty(self.part)
        r = self.login("shop_admin").post(f"/projects/{self.project}/purge", follow_redirects=True)
        self.assertIn("Choose what happens to the parts", r.get_data(as_text=True))
        self.assertIsNotNone(self.q1("SELECT id FROM projects WHERE id = ?", (self.project,)))
        self.assertEqual(self.qty(self.part), before)

    def test_return_to_stock_increases_qty_and_logs_a_return(self):
        self.take_parts(3)
        before = self.qty(self.part)
        self.trash_it()
        r = self.login("shop_admin").post(f"/projects/{self.project}/purge", data=dict(parts_action="return"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.qty(self.part), before + 3)
        ret = self.q1("SELECT * FROM transactions WHERE part_id = ? AND type = 'in' AND note LIKE '%Returned%'",
                      (self.part,))
        self.assertIsNotNone(ret)
        self.assertEqual(ret["qty"], 3)
        self.assertIsNone(ret["project_id"])

    def test_deleted_keeps_qty_and_annotates_history(self):
        self.take_parts(3)
        before = self.qty(self.part)
        self.trash_it()
        self.login("shop_admin").post(f"/projects/{self.project}/purge", data=dict(parts_action="deleted"))
        self.assertEqual(self.qty(self.part), before)
        out_txn = self.q1("SELECT * FROM transactions WHERE part_id = ? AND type = 'out'", (self.part,))
        self.assertIsNotNone(out_txn, "the original out transaction should be kept, not deleted")
        self.assertIn("deleted job", (out_txn["note"] or "").lower())
        self.assertIsNone(out_txn["project_id"])

    # --- hours -----------------------------------------------------------
    def test_purge_requires_an_hours_choice_when_hours_were_clocked(self):
        self.clock_hours()
        self.trash_it()
        r = self.login("shop_admin").post(f"/projects/{self.project}/purge", follow_redirects=True)
        self.assertIn("Choose what happens to the hours", r.get_data(as_text=True))
        self.assertIsNotNone(self.q1("SELECT id FROM labor_sessions WHERE laborer_id = ?", (self.laborer_id,)))

    def test_remove_from_pay_deletes_the_session(self):
        self.clock_hours()
        self.trash_it()
        self.login("shop_admin").post(f"/projects/{self.project}/purge", data=dict(hours_action="remove"))
        self.assertEqual(len(self.q("SELECT id FROM labor_sessions WHERE laborer_id = ?", (self.laborer_id,))), 0)

    def test_transfer_keeps_hours_rate_and_pay_as_general_shop_time(self):
        self.clock_hours(hours=4.0, rate=32)
        self.trash_it()
        self.login("shop_admin").post(f"/projects/{self.project}/purge", data=dict(hours_action="transfer"))
        row = self.q1("SELECT * FROM labor_sessions WHERE laborer_id = ?", (self.laborer_id,))
        self.assertIsNotNone(row)
        self.assertIsNone(row["project_id"])
        self.assertIsNone(row["section"])
        self.assertEqual(row["hours"], 4.0)
        self.assertEqual(row["rate"], 32)
        self.assertEqual(row["cost"], 128.0)

    # --- both, and the success message ------------------------------------
    def test_success_message_reports_what_happened(self):
        self.take_parts(3)
        self.clock_hours(hours=4.0, rate=32)
        self.trash_it()
        r = self.login("shop_admin").post(f"/projects/{self.project}/purge",
                                          data=dict(parts_action="return", hours_action="transfer"),
                                          follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("3 parts returned to stock", html)
        self.assertIn("4 hours moved to General Shop time", html)

    # --- Empty Trash applies one answer to every job -------------------------
    def test_empty_trash_requires_the_same_choices_and_applies_them_to_every_job(self):
        self.take_parts(2)
        other_part = self.make_part(name="Spark Plug", barcode="PART-002", qty=20)
        other_project = self.make_project(name="Oil change - N999")
        self.login("tech")
        self.scan("PART-002", "out", 5, other_project)
        self.trash_it()
        self.exec("UPDATE projects SET deleted_at = ? WHERE id = ?", (db.now_iso(), other_project))

        r = self.login("shop_admin").post("/trash/empty", follow_redirects=True)
        self.assertIn("Choose what happens to the parts", r.get_data(as_text=True))

        r = self.login("shop_admin").post("/trash/empty", data=dict(parts_action="return"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.qty(self.part), 10)
        self.assertEqual(self.qty(other_part), 20)
        self.assertIsNone(self.q1("SELECT id FROM projects WHERE id = ?", (self.project,)))
        self.assertIsNone(self.q1("SELECT id FROM projects WHERE id = ?", (other_project,)))
