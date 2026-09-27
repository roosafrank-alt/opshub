"""QA finding ux-squawk-on-project: a plane's squawks and to-dos live on the
plane, but the work happens on the project. "Fix on this job"/"Do on this
job" claims one and gives it a matching Sub Area on the project
(_find_or_create_linked_section in app.py); checking that Sub Area off
moves the squawk/to-do to Inspection, and confirming/sending it back
does the same, so there's one record, not a copy."""
from harness import OpsHubTestCase, seed_row
import db


class SquawkOnProjectTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        self.project_id = self.make_project(name="Annual - N999TT", asset_id=self.asset_id)
        self.tech_id = self.users["tech"]["id"]

    def make_squawk(self, notes="Left brake soft"):
        conn = db.get_db()
        squawk_id = seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes=notes)
        conn.commit()
        conn.close()
        return squawk_id

    def make_todo(self, description="Replace cracked nav light lens"):
        return self.exec(
            "INSERT INTO plane_todos (asset_id, description, created_by, created_at) VALUES (?,?,?,?)",
            (self.asset_id, description, "Admin", db.now_iso()))

    def get_squawk(self, squawk_id):
        return self.q1("SELECT * FROM plane_squawks WHERE id = ?", (squawk_id,))

    def get_todo(self, todo_id):
        return self.q1("SELECT * FROM plane_todos WHERE id = ?", (todo_id,))

    def get_section(self, **where):
        clause = " AND ".join(f"{k} = ?" for k in where)
        return self.q1(f"SELECT * FROM project_sections WHERE {clause}", tuple(where.values()))

    # ----- project page shows the box ------------------------------------

    def test_project_page_shows_plane_squawks_and_todos_box(self):
        self.make_squawk()
        self.make_todo()
        c = self.login("tech")
        body = c.get(f"/projects/{self.project_id}").get_data(as_text=True)
        self.assertIn("squawks &amp; to-dos", body)
        self.assertIn("Fix on this job", body)
        self.assertIn("Do on this job", body)

    # ----- fix on this job -------------------------------------------------

    def test_fix_on_this_job_creates_linked_section_and_claims_squawk(self):
        squawk_id = self.make_squawk("Right brake dragging")
        c = self.login("tech")
        r = c.post(f"/projects/{self.project_id}/squawks/quick/{squawk_id}/fix_on_job")
        self.assertEqual(r.status_code, 302)
        row = self.get_squawk(squawk_id)
        self.assertIsNotNone(row["acknowledged_at"])
        self.assertEqual(row["assigned_to"], self.tech_id)
        self.assertIsNotNone(row["worker_acknowledged_at"])
        section = self.get_section(project_id=self.project_id, linked_squawk_kind="quick", linked_squawk_id=squawk_id)
        self.assertIsNotNone(section)
        self.assertEqual(section["name"], "Right brake dragging")

    def test_fix_on_this_job_is_idempotent(self):
        squawk_id = self.make_squawk()
        c = self.login("tech")
        c.post(f"/projects/{self.project_id}/squawks/quick/{squawk_id}/fix_on_job")
        c.post(f"/projects/{self.project_id}/squawks/quick/{squawk_id}/fix_on_job")
        rows = self.q("SELECT * FROM project_sections WHERE project_id = ? AND linked_squawk_id = ?",
                      (self.project_id, squawk_id))
        self.assertEqual(len(rows), 1)

    def test_project_page_shows_sub_area_on_this_job_after_claiming(self):
        squawk_id = self.make_squawk("Nav light out")
        c = self.login("tech")
        c.post(f"/projects/{self.project_id}/squawks/quick/{squawk_id}/fix_on_job")
        body = c.get(f"/projects/{self.project_id}").get_data(as_text=True)
        self.assertIn("Discrepancy on this job", body)
        self.assertNotIn("Fix on this job", body)

    # ----- checking the linked sub area off propagates --------------------

    def test_checking_linked_section_moves_squawk_to_inspection(self):
        squawk_id = self.make_squawk()
        c = self.login("tech")
        c.post(f"/projects/{self.project_id}/squawks/quick/{squawk_id}/fix_on_job")
        section = self.get_section(project_id=self.project_id, linked_squawk_id=squawk_id)
        r = c.post(f"/projects/{self.project_id}/sections/{section['id']}/complete", data={"completed": "1"})
        self.assertEqual(r.status_code, 302)
        row = self.get_squawk(squawk_id)
        self.assertIsNotNone(row["repair_confirm_requested_at"])
        self.assertIsNone(row["repaired_at"])

    def test_confirming_linked_section_repairs_the_squawk(self):
        squawk_id = self.make_squawk()
        c = self.login("tech")
        c.post(f"/projects/{self.project_id}/squawks/quick/{squawk_id}/fix_on_job")
        section = self.get_section(project_id=self.project_id, linked_squawk_id=squawk_id)
        c.post(f"/projects/{self.project_id}/sections/{section['id']}/complete", data={"completed": "1"})
        c2 = self.login("inspector")
        r = c2.post(f"/projects/{self.project_id}/sections/{section['id']}/confirm")
        self.assertEqual(r.status_code, 302)
        row = self.get_squawk(squawk_id)
        self.assertIsNotNone(row["repaired_at"])

    def test_sending_back_linked_section_reopens_the_squawk(self):
        squawk_id = self.make_squawk()
        c = self.login("tech")
        c.post(f"/projects/{self.project_id}/squawks/quick/{squawk_id}/fix_on_job")
        section = self.get_section(project_id=self.project_id, linked_squawk_id=squawk_id)
        c.post(f"/projects/{self.project_id}/sections/{section['id']}/complete", data={"completed": "1"})
        c2 = self.login("inspector")
        r = c2.post(f"/projects/{self.project_id}/sections/{section['id']}/confirm", data={"action": "send_back"})
        self.assertEqual(r.status_code, 302)
        row = self.get_squawk(squawk_id)
        self.assertIsNone(row["repair_confirm_requested_at"])
        self.assertIsNone(row["repaired_at"])

    # ----- do on this job (to-do) ------------------------------------------

    def test_do_on_this_job_creates_linked_section_and_claims_todo(self):
        todo_id = self.make_todo("Re-torque prop bolts")
        c = self.login("tech")
        r = c.post(f"/projects/{self.project_id}/todos/{todo_id}/do_on_job")
        self.assertEqual(r.status_code, 302)
        row = self.get_todo(todo_id)
        self.assertEqual(row["assigned_to"], self.tech_id)
        section = self.get_section(project_id=self.project_id, linked_todo_id=todo_id)
        self.assertIsNotNone(section)
        self.assertEqual(section["name"], "Re-torque prop bolts")

    def test_project_page_shows_working_step_for_claimed_todo(self):
        todo_id = self.make_todo()
        c = self.login("tech")
        c.post(f"/projects/{self.project_id}/todos/{todo_id}/do_on_job")
        body = c.get(f"/projects/{self.project_id}").get_data(as_text=True)
        self.assertIn("bg-primary text-white\">Working", body)

    def test_checking_linked_todo_section_moves_it_to_inspection(self):
        todo_id = self.make_todo()
        c = self.login("tech")
        c.post(f"/projects/{self.project_id}/todos/{todo_id}/do_on_job")
        section = self.get_section(project_id=self.project_id, linked_todo_id=todo_id)
        c.post(f"/projects/{self.project_id}/sections/{section['id']}/complete", data={"completed": "1"})
        row = self.get_todo(todo_id)
        self.assertIsNotNone(row["confirm_requested_at"])
        self.assertEqual(row["done"], 0)

    def test_confirming_linked_todo_section_completes_the_todo(self):
        todo_id = self.make_todo()
        c = self.login("tech")
        c.post(f"/projects/{self.project_id}/todos/{todo_id}/do_on_job")
        section = self.get_section(project_id=self.project_id, linked_todo_id=todo_id)
        c.post(f"/projects/{self.project_id}/sections/{section['id']}/complete", data={"completed": "1"})
        c2 = self.login("inspector")
        c2.post(f"/projects/{self.project_id}/sections/{section['id']}/confirm")
        row = self.get_todo(todo_id)
        self.assertEqual(row["done"], 1)
        self.assertIsNotNone(row["completed_at"])

    # ----- linked elsewhere -------------------------------------------------

    def test_squawk_claimed_on_a_different_project_shows_that_job_not_a_button(self):
        other_project_id = self.make_project(name="Oil Change - N999TT", asset_id=self.asset_id)
        squawk_id = self.make_squawk("Oil seep")
        c = self.login("tech")
        c.post(f"/projects/{other_project_id}/squawks/quick/{squawk_id}/fix_on_job")
        body = c.get(f"/projects/{self.project_id}").get_data(as_text=True)
        self.assertIn("Discrepancy on", body)
        self.assertNotIn("Fix on this job", body)

    # ----- intake ------------------------------------------------------------

    def intake_form(self, **extra):
        form = {"mags_status": "ok", "oil_status": "ok", "brakes_status": "ok", "gauges_status": "ok"}
        form.update(extra)
        return form

    def test_intake_squawk_becomes_a_real_plane_squawk(self):
        c = self.login("tech")
        form = self.intake_form(**{"squawks_0": "Static on Com 2"})
        r = c.post(f"/projects/{self.project_id}/intake", data=form)
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM plane_squawks WHERE asset_id = ? AND notes = ?",
                      (self.asset_id, "Static on Com 2"))
        self.assertIsNotNone(row)

    def test_intake_squawk_addressed_gets_claimed_on_this_job(self):
        c = self.login("tech")
        form = self.intake_form(**{"squawks_0": "Oil seep at rocker cover", "squawks_0_address": "1"})
        c.post(f"/projects/{self.project_id}/intake", data=form)
        row = self.q1("SELECT * FROM plane_squawks WHERE asset_id = ? AND notes = ?",
                      (self.asset_id, "Oil seep at rocker cover"))
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["worker_acknowledged_at"])
        section = self.get_section(project_id=self.project_id, linked_squawk_id=row["id"])
        self.assertIsNotNone(section)

    def test_intake_squawk_not_addressed_has_no_linked_section(self):
        c = self.login("tech")
        form = self.intake_form(**{"squawks_0": "Minor paint chip"})
        c.post(f"/projects/{self.project_id}/intake", data=form)
        row = self.q1("SELECT * FROM plane_squawks WHERE asset_id = ? AND notes = ?",
                      (self.asset_id, "Minor paint chip"))
        self.assertIsNotNone(row)
        self.assertIsNone(row["worker_acknowledged_at"])
        section = self.get_section(project_id=self.project_id, linked_squawk_id=row["id"])
        self.assertIsNone(section)
