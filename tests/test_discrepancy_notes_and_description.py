"""Idea "Discrepancy List": each discrepancy (project_sections row) can now
have Internal Notes (shop-only - never on the invoice or in My Aircraft)
and a Description (shown to the aircraft owner in My Aircraft, once it's
filled in). See project_section_notes() in app.py and
customer._project_discrepancy_descriptions()."""
from harness import OpsHubTestCase
import db


class DiscrepancyNotesAndDescriptionTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        self.project_id = self.make_project(name="Annual - N999TT", asset_id=self.asset_id)
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)",
                   (self.customer_id, self.asset_id))
        self.section_id = self.exec(
            "INSERT INTO project_sections (project_id, name, created_at) VALUES (?,?,?)",
            (self.project_id, "Brakes", db.now_iso()))

    def test_saving_notes_and_description(self):
        c = self.login("shop_admin")
        r = c.post(f"/projects/{self.project_id}/sections/{self.section_id}/notes",
                   data={"notes": "Owner is difficult, confirm price before starting", "description": "Replaced worn brake pads."})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM project_sections WHERE id = ?", (self.section_id,))
        self.assertEqual(row["notes"], "Owner is difficult, confirm price before starting")
        self.assertEqual(row["description"], "Replaced worn brake pads.")

    def test_project_page_shows_both_fields_to_shop_staff(self):
        self.exec("UPDATE project_sections SET notes = ?, description = ? WHERE id = ?",
                  ("Internal-only detail", "Owner-facing writeup", self.section_id))
        c = self.login("shop_admin")
        body = c.get(f"/projects/{self.project_id}").get_data(as_text=True)
        self.assertIn("Internal-only detail", body)
        self.assertIn("Owner-facing writeup", body)

    def test_customer_portal_shows_description_not_notes(self):
        self.exec("UPDATE project_sections SET notes = ?, description = ? WHERE id = ?",
                  ("Internal-only detail", "Replaced worn brake pads.", self.section_id))
        c = self.login("customer")
        body = c.get(f"/portal/aircraft/{self.asset_id}").get_data(as_text=True)
        self.assertIn("Replaced worn brake pads.", body)
        self.assertNotIn("Internal-only detail", body)

    def test_customer_portal_omits_discrepancies_with_no_description(self):
        # Notes-only, no description filled in - nothing to show the owner.
        self.exec("UPDATE project_sections SET notes = ? WHERE id = ?",
                  ("Internal-only detail", self.section_id))
        c = self.login("customer")
        body = c.get(f"/portal/aircraft/{self.asset_id}").get_data(as_text=True)
        self.assertNotIn("Internal-only detail", body)
        self.assertNotIn("Brakes:", body)

    def test_invoice_csv_never_includes_notes_or_description(self):
        self.exec("UPDATE project_sections SET notes = ?, description = ? WHERE id = ?",
                  ("Internal-only detail", "Replaced worn brake pads.", self.section_id))
        c = self.login("shop_admin")
        csv_body = c.get(f"/projects/{self.project_id}/invoice.csv").get_data(as_text=True)
        self.assertNotIn("Internal-only detail", csv_body)
        self.assertNotIn("Replaced worn brake pads.", csv_body)

    def test_only_admin_or_tech_can_save_notes(self):
        c = self.login("inspector")
        r = c.post(f"/projects/{self.project_id}/sections/{self.section_id}/notes",
                   data={"notes": "x", "description": "y"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM project_sections WHERE id = ?", (self.section_id,))
        self.assertIsNone(row["notes"])
