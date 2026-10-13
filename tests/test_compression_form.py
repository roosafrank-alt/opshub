"""Compression "Log a Check" form: Performed By is a pick list of techs and
Hours starts out as the last hours entered."""
from harness import OpsHubTestCase, seed_row
import db


class CompressionForm(OpsHubTestCase):
    def test_performed_by_list_and_hours_prefill(self):
        aid = self.make_asset()
        self.exec("INSERT INTO compression_checks (asset_id, checked_date, hours, created_at) VALUES (?, '2026-01-01', 1234.5, ?)",
                  (aid, db.now_iso()))
        html = self.login("tech").get(f"/assets/{aid}/compression").get_data(as_text=True)
        self.assertIn('<select name="performed_by"', html)
        self.assertIn(f'<option value="{self.users["tech"]["name"]}" selected>', html)
        self.assertIn('name="hours" class="form-control form-control-sm" value="1234.5"', html)

    def test_first_check_has_empty_hours(self):
        aid = self.make_asset()
        html = self.login("tech").get(f"/assets/{aid}/compression").get_data(as_text=True)
        self.assertIn('name="hours" class="form-control form-control-sm" value=""', html)
