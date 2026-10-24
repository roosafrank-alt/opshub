"""Compression "Log a Check" form: Performed By is a pick list of techs, and
Tach and Hobbs start out as the aircraft's last readings (both editable, either
optional, at least one required)."""
from harness import OpsHubTestCase, seed_row
import db


class CompressionForm(OpsHubTestCase):
    def _set_meters(self, aid, tach=None, hobbs=None):
        self.exec("UPDATE assets SET tach_hours = ?, hobbs_hours = ? WHERE id = ?", (tach, hobbs, aid))

    def test_performed_by_list_and_meters_prefill(self):
        aid = self.make_asset()
        self._set_meters(aid, tach=1234.5, hobbs=1301.2)
        html = self.login("tech").get(f"/assets/{aid}/compression").get_data(as_text=True)
        self.assertIn('<select name="performed_by"', html)
        self.assertIn(f'<option value="{self.users["tech"]["name"]}" selected>', html)
        self.assertIn('name="tach_hours" id="cc-tach" class="form-control form-control-sm" value="1234.5"', html)
        self.assertIn('name="hobbs_hours" id="cc-hobbs" class="form-control form-control-sm" value="1301.2"', html)

    def test_no_meters_means_empty_boxes(self):
        aid = self.make_asset()
        html = self.login("tech").get(f"/assets/{aid}/compression").get_data(as_text=True)
        self.assertIn('name="tach_hours" id="cc-tach" class="form-control form-control-sm" value=""', html)
        self.assertIn('name="hobbs_hours" id="cc-hobbs" class="form-control form-control-sm" value=""', html)

    def test_prefill_falls_back_to_last_check(self):
        aid = self.make_asset()
        self.exec("INSERT INTO compression_checks (asset_id, checked_date, tach_hours, hobbs_hours, created_at) "
                  "VALUES (?, '2026-01-01', 500.5, 610.0, ?)", (aid, db.now_iso()))
        html = self.login("tech").get(f"/assets/{aid}/compression").get_data(as_text=True)
        self.assertIn('id="cc-tach" class="form-control form-control-sm" value="500.5"', html)
        self.assertIn('id="cc-hobbs" class="form-control form-control-sm" value="610"', html)

    def test_either_meter_alone_is_enough(self):
        aid = self.make_asset()
        c = self.login("tech")
        c.post(f"/assets/{aid}/compression", data={"cyl1": "70", "tach_hours": "100.5"})
        c.post(f"/assets/{aid}/compression", data={"cyl1": "71", "hobbs_hours": "120"})
        n = self.q1("SELECT COUNT(*) n FROM compression_checks WHERE asset_id = ?", (aid,))["n"]
        self.assertEqual(n, 2)
        first = self.q1("SELECT * FROM compression_checks WHERE cyl1 = 70")
        second = self.q1("SELECT * FROM compression_checks WHERE cyl1 = 71")
        self.assertEqual((first["tach_hours"], first["hobbs_hours"]), (100.5, None))
        self.assertEqual((second["tach_hours"], second["hobbs_hours"]), (None, 120))

    def test_neither_meter_is_rejected(self):
        aid = self.make_asset()
        c = self.login("tech")
        c.post(f"/assets/{aid}/compression", data={"cyl1": "70", "tach_hours": "", "hobbs_hours": ""})
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM compression_checks")["n"], 0)


class CompressionSetupOneLine(OpsHubTestCase):
    def _check(self, aid, master, cyls):
        self.exec("INSERT INTO compression_checks (asset_id, checked_date, tach_hours, master_orifice, cyl1, cyl2, created_at) "
                  "VALUES (?, '2026-01-01', 10, ?, ?, ?, ?)", (aid, master, cyls[0], cyls[1], db.now_iso()))

    def test_first_check_shows_boxes(self):
        aid = self.make_asset()
        html = self.login("tech").get(f"/assets/{aid}/compression").get_data(as_text=True)
        self.assertNotIn('id="cc-setup-line"', html)
        self.assertNotIn('id="cc-setup" class="d-none"', html)
        self.assertIn('name="master_orifice" class="form-control form-control-sm" value="80"', html)

    def test_later_check_shows_one_line_with_last_master(self):
        aid = self.make_asset()
        self._check(aid, 100, (90, 91))
        html = self.login("tech").get(f"/assets/{aid}/compression").get_data(as_text=True)
        self.assertIn("Master 100", html)
        self.assertIn("same as last check", html)
        self.assertIn('id="cc-setup" class="d-none"', html)
        self.assertIn('name="master_orifice" class="form-control form-control-sm" value="100"', html)
        self.assertIn('<option value="2" selected>', html)

    def test_editing_shows_boxes(self):
        aid = self.make_asset()
        self._check(aid, 100, (90, 91))
        cid = self.q1("SELECT id FROM compression_checks")["id"]
        html = self.login("tech").get(f"/assets/{aid}/compression?edit={cid}").get_data(as_text=True)
        self.assertNotIn('id="cc-setup-line"', html)
