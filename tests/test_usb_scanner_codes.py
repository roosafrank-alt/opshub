"""A USB scanner types like a keyboard, so every kind of code has to be
recognized the way it actually arrives: with Caps Lock on the shop PC (every
letter flipped), from any page (Shift pressed before each capital), and
ended by Enter or Tab. Parts, laborer badges, GENERAL-SHOP, project codes
and TASK- codes all route by the Scan page's logic."""
from harness import OpsHubTestCase, seed_row
import db  # noqa: E402 - harness puts the app folder on sys.path


class UsbScannerCodesTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        conn = db.get_db()
        self.laborer = seed_row(conn, "laborers", name="Kate", code="LABOR-AB12CD34", rate=30, active=1)
        conn.commit()
        conn.close()
        self.part = self.make_part(name="Oil Filter", barcode="SHOP-QW12ER34", qty=5)
        self.project = self.make_project()
        self.project_code = self.q1("SELECT code FROM projects WHERE id = ?", (self.project,))["code"]
        self.exec("INSERT INTO project_sections (project_id, name) VALUES (?, ?)", (self.project, "Brakes"))
        self.login("tech")

    def test_part_barcode_found_exactly_and_with_caps_lock(self):
        for code in ("SHOP-QW12ER34", "shop-qw12er34", " SHOP-QW12ER34 "):
            with self.subTest(code=code):
                r = self.client.get("/api/lookup/" + code.strip())
                self.assertTrue(r.json["found"])
                self.assertEqual(r.json["barcode"], "SHOP-QW12ER34")

    def test_caps_lock_part_can_be_scanned_out(self):
        r = self.client.post("/api/scan", json=dict(barcode="shop-qw12er34", action="out", qty=1,
                                                    project_id=self.project, performed_by="Kate"))
        self.assertTrue(r.json["ok"], r.json)
        self.assertEqual(self.q1("SELECT qty_on_hand FROM parts WHERE id = ?", (self.part,))[0], 4)

    def test_case_blind_part_match_never_guesses_between_two_parts(self):
        self.make_part(name="Other", barcode="shop-QW12ER34", qty=1)
        self.assertFalse(self.client.get("/api/lookup/SHOP-qw12er34").json["found"])
        # An exact match still wins.
        self.assertEqual(self.client.get("/api/lookup/SHOP-QW12ER34").json["name"], "Oil Filter")

    def test_laborer_badge_with_caps_lock_clocks_in(self):
        r = self.client.post("/api/labor/scan", json=dict(code="labor-ab12cd34", project_id=self.project))
        self.assertEqual(r.json.get("action"), "clock_in", r.json)
        self.assertEqual(r.json["laborer"], "Kate")

    def test_laborer_badge_with_caps_lock_clocks_into_general_shop(self):
        r = self.client.post("/api/labor/scan", json=dict(code="labor-ab12cd34", general=True))
        self.assertEqual(r.json.get("action"), "clock_in", r.json)
        self.assertTrue(r.json["general"])

    def test_task_code_with_caps_lock_finds_project_and_real_area_name(self):
        r = self.client.get("/api/labor/task_lookup", query_string={"code": f"task-{self.project_code}::bRAKES"})
        self.assertTrue(r.json["found"])
        self.assertEqual((r.json["project_id"], r.json["section"]), (self.project, "Brakes"))
        r = self.client.get("/api/labor/task_lookup", query_string={"code": f"TASK-{self.project_code}::Brakes"})
        self.assertEqual(r.json["section"], "Brakes")

    def test_project_code_lookup(self):
        self.assertTrue(self.client.get("/api/project_lookup/" + self.project_code).json["found"])

    def test_scan_page_routes_every_code_type_case_blind(self):
        html = self.client.get("/scan").get_data(as_text=True)
        self.assertIn("function normalizeScanValue", html)
        self.assertIn("upper === GENERAL_SHOP_CODE || upper.startsWith('LABOR-')", html)
        self.assertIn("e.key === 'Tab' && scanInput.value.trim()", html)
        # A scan while the "Scanning as" dropdown has focus goes to the scan box.
        self.assertIn("scanInput.value += e.key", html)

    def test_other_pages_keep_the_scan_through_shift_presses(self):
        html = self.client.get("/projects").get_data(as_text=True)
        self.assertIn("if (MODIFIER_KEYS[e.key]) return;", html)
