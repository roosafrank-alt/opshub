"""QA fix qa-project-bad-dates-saved: a job form with a start or end date that
isn't a real date is refused with a message instead of being saved."""
from harness import OpsHubTestCase


class ProjectBadDatesTest(OpsHubTestCase):
    MSG = "Please enter a real date"

    def test_new_job_with_junk_start_date_is_refused(self):
        before = len(self.q("SELECT id FROM projects"))
        r = self.login("shop_admin").post("/projects/new", data=dict(
            name="Junk date job", scheduled_date="31/31/2026"))
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn(self.MSG, html)
        self.assertIn("Junk date job", html)
        self.assertEqual(len(self.q("SELECT id FROM projects")), before)

    def test_edit_job_with_junk_end_date_is_refused(self):
        pid = self.make_project(name="Edit me")
        r = self.login("shop_admin").post(f"/projects/{pid}/edit", data=dict(
            name="Edit me too", scheduled_date="2026-11-05", scheduled_end_date="soon"))
        self.assertIn(self.MSG, r.get_data(as_text=True))
        row = self.q1("SELECT name, scheduled_date FROM projects WHERE id = ?", (pid,))
        self.assertEqual(row["name"], "Edit me")
        self.assertIsNone(row["scheduled_date"])

    def test_real_dates_still_save(self):
        r = self.login("shop_admin").post("/projects/new", data=dict(
            name="Good date job", scheduled_date="2026-11-05"))
        self.assertEqual(r.status_code, 302)
