"""Project page: Add Quick Type drops down the Quick Types and adds that
type's required preset discrepancies to the job."""
from harness import OpsHubTestCase, seed_row
import db


class AddQuickType(OpsHubTestCase):
    def test_dropdown_and_add(self):
        pid = self.make_project()
        self.exec("INSERT INTO task_template_areas (quick_type, name, sort_order, is_optional, created_at) VALUES ('Annual Inspection', 'Brakes', 0, 0, ?)", (db.now_iso(),))
        self.exec("INSERT INTO task_template_areas (quick_type, name, sort_order, is_optional, created_at) VALUES ('Annual Inspection', 'Extra', 1, 1, ?)", (db.now_iso(),))
        c = self.login("tech")
        html = c.get(f"/projects/{pid}").get_data(as_text=True)
        self.assertIn("Quick Type", html)
        self.assertIn(f"/projects/{pid}/add_quick_type", html)
        r = c.post(f"/projects/{pid}/add_quick_type", data={"quick_type": "Annual Inspection"})
        self.assertEqual(r.status_code, 302)
        names = [x["name"] for x in self.q("SELECT name FROM project_sections WHERE project_id = ?", (pid,))]
        self.assertEqual(sorted(names), ["Annual Inspection", "Brakes"])
        c.post(f"/projects/{pid}/add_quick_type", data={"quick_type": "Annual Inspection"})
        self.assertEqual(len(self.q("SELECT 1 FROM project_sections WHERE project_id = ?", (pid,))), 2)
