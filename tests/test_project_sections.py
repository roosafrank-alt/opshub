"""Project sub-areas (discrepancies): add, rename (history follows), mark ready,
inspector confirm or send back, plus rejections and missing records."""
from harness import OpsHubTestCase, open_finding, seed_row
import db


class ProjectSectionsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N5SEC")
        self.project = self.make_project(name="Annual - N5SEC", asset_id=self.plane)
        c = self.login("shop_admin")
        c.post(f"/projects/{self.project}/add_section", data=dict(name="Left wing"))
        self.sec = self.q1("SELECT id FROM project_sections WHERE project_id = ? AND name = ?",
                           (self.project, "Left wing"))["id"]

    def row(self, sid=None):
        return self.q1("SELECT * FROM project_sections WHERE id = ?", (sid or self.sec,))

    def url(self, action, sid=None, pid=None):
        return f"/projects/{pid or self.project}/sections/{sid or self.sec}/{action}"

    # --- rename ---------------------------------------------------------
    def test_rename_blank_is_refused(self):
        c = self.login("tech")
        r = c.post(self.url("rename"), data=dict(name="   "))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.row()["name"], "Left wing")

    def test_rename_to_existing_name_is_refused(self):
        self.login("shop_admin").post(f"/projects/{self.project}/add_section", data=dict(name="Right wing"))
        self.login("tech").post(self.url("rename"), data=dict(name="Right wing"))
        self.assertEqual(self.row()["name"], "Left wing")

    def test_rename_carries_history_and_labor(self):
        conn = db.get_db()
        lab = seed_row(conn, "laborers", name="Pat")
        seed_row(conn, "labor_sessions", laborer_id=lab, project_id=self.project, section="Left wing",
                 started_at=db.now_iso())
        conn.commit()
        conn.close()
        self.login("tech").post(self.url("rename"), data=dict(name="Port wing"))
        self.assertEqual(self.row()["name"], "Port wing")
        self.assertEqual(len(self.q("SELECT 1 FROM labor_sessions WHERE project_id = ? AND section = 'Port wing'",
                                    (self.project,))), 1)
        self.assertEqual(len(self.q("SELECT 1 FROM labor_sessions WHERE project_id = ? AND section = 'Left wing'",
                                    (self.project,))), 0)

    def test_rename_missing_section_or_wrong_job_is_404(self):
        c = self.login("tech")
        self.assertEqual(c.post(self.url("rename", sid=99999), data=dict(name="x")).status_code, 404)
        other = self.make_project(name="Other job")
        self.assertEqual(c.post(self.url("rename", pid=other), data=dict(name="x")).status_code, 404)

    def test_rename_roles(self):
        for role in ("inspector", "shop_student", "flight_student", "no_roles"):
            c = self.login(role)
            r = c.post(self.url("rename"), data=dict(name="Hacked"))
            self.assertNotEqual(self.row()["name"], "Hacked", role)

    # --- ready / confirm / send back -------------------------------------
    def test_tech_marks_ready_but_it_is_not_complete_until_confirmed(self):
        self.login("tech").post(self.url("complete"), data=dict(completed="1"))
        r = self.row()
        self.assertIsNotNone(r["confirm_requested_at"])
        self.assertIsNone(r["completed_at"])

    def test_inspector_confirms_then_tech_cannot_reopen(self):
        self.login("tech").post(self.url("complete"), data=dict(completed="1"))
        self.login("inspector").post(self.url("confirm"), data=dict(action="confirm"))
        self.assertIsNotNone(self.row()["completed_at"])
        self.login("tech").post(self.url("complete"), data=dict(completed="0"))
        self.assertIsNotNone(self.row()["completed_at"])
        self.login("shop_admin").post(self.url("complete"), data=dict(completed="0"))
        r = self.row()
        self.assertIsNone(r["completed_at"])
        self.assertIsNotNone(r["reopened_at"])

    def test_send_back_clears_ready_and_completion(self):
        self.login("tech").post(self.url("complete"), data=dict(completed="1"))
        self.login("inspector").post(self.url("confirm"), data=dict(action="send_back"))
        r = self.row()
        self.assertIsNone(r["confirm_requested_at"])
        self.assertIsNone(r["completed_at"])
        self.assertIsNotNone(r["sent_back_at"])

    def test_tech_cannot_confirm_own_work(self):
        self.login("tech").post(self.url("complete"), data=dict(completed="1"))
        self.login("tech").post(self.url("confirm"), data=dict(action="confirm"))
        self.assertIsNone(self.row()["completed_at"])

    def test_inspector_cannot_mark_ready(self):
        self.login("inspector").post(self.url("complete"), data=dict(completed="1"))
        self.assertIsNone(self.row()["confirm_requested_at"])

    def test_confirm_missing_section_is_404(self):
        self.assertEqual(self.login("inspector").post(self.url("confirm", sid=99999),
                                                      data=dict(action="confirm")).status_code, 404)
        self.assertEqual(self.login("tech").post(self.url("complete", sid=99999),
                                                 data=dict(completed="1")).status_code, 404)

    def test_confirming_twice_keeps_first_confirmation(self):
        self.login("tech").post(self.url("complete"), data=dict(completed="1"))
        c = self.login("inspector")
        c.post(self.url("confirm"), data=dict(action="confirm"))
        first = self.row()["completed_at"]
        self.exec("UPDATE project_sections SET completed_at = '2020-01-01T00:00:00' WHERE id = ?", (self.sec,))
        c.post(self.url("confirm"), data=dict(action="confirm"))
        self.assertEqual(self.row()["completed_at"], "2020-01-01T00:00:00")

    def test_confirm_requires_that_it_was_marked_ready(self):
        self.login("inspector").post(self.url("confirm"), data=dict(action="confirm"))
        self.assertIsNone(self.row()["completed_at"])

    def test_inspector_can_mark_ready_and_confirm_in_one_tap(self):
        self.login("inspector").post(self.url("confirm"), data=dict(action="ready_and_confirm"))
        r = self.row()
        self.assertIsNotNone(r["completed_at"])
        self.assertEqual(r["confirm_requested_at"], r["completed_at"])
        self.assertEqual(r["confirm_requested_by"], r["completed_by"])

    def test_tech_cannot_mark_ready_and_confirm(self):
        self.login("tech").post(self.url("confirm"), data=dict(action="ready_and_confirm"))
        self.assertIsNone(self.row()["completed_at"])

    def test_trashed_job_sections_are_read_only(self):
        self.exec("UPDATE projects SET deleted_at = ? WHERE id = ?", (db.now_iso(), self.project))
        self.login("tech").post(self.url("complete"), data=dict(completed="1"))
        self.login("tech").post(self.url("rename"), data=dict(name="Nope"))
        self.login("inspector").post(self.url("confirm"), data=dict(action="confirm"))
        r = self.row()
        self.assertEqual(r["name"], "Left wing")
        self.assertIsNone(r["confirm_requested_at"])
        self.assertIsNone(r["completed_at"])

    # --- add / assign ----------------------------------------------------
    def test_add_section_blank_and_duplicate(self):
        c = self.login("shop_admin")
        c.post(f"/projects/{self.project}/add_section", data=dict(name="  "))
        c.post(f"/projects/{self.project}/add_section", data=dict(name="Left wing"))
        n = self.q1("SELECT COUNT(*) AS n FROM project_sections WHERE project_id = ?", (self.project,))["n"]
        self.assertEqual(n, 1)

    def test_assign_to_unknown_person_is_refused(self):
        self.login("tech").post(self.url("assign"), data=dict(assigned_to="99999"))
        self.assertIsNone(self.row()["assigned_to"])
        self.login("tech").post(self.url("assign"), data=dict(assigned_to="abc"))
        self.assertIsNone(self.row()["assigned_to"])
