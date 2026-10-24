"""Oil Consumption page + the plane's own page: a yellow or red 'Oil burn up'
note when the last 3 top-offs came much closer together than the plane's own
normal, red has an Add to discrepancy button, and the page shows 3 boxes."""
from harness import OpsHubTestCase
import db


class OilBurnUp(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.aid = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, created_at, updated_at) VALUES (?,?,1,?,?)",
            ("N12345", "Cessna 172", db.now_iso(), db.now_iso()))
        self.cfi = self.q1("SELECT id FROM cfis WHERE username = 'cfi'")["id"]
        self.stu = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]
        self.n = 0

    def flight(self, hours, qt):
        self.n += 1
        self.exec("INSERT INTO flights (asset_id, cfi_id, student_id, started_at, flight_date, recorded_hours, oil_added_qt) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (self.aid, self.cfi, self.stu, db.now_iso(), "2026-%02d-%02d" % (1 + self.n // 28, 1 + self.n % 28), hours, qt))

    def history(self, normal, recent):
        for _ in range(6):
            self.flight(normal, 1)
        for _ in range(3):
            self.flight(recent, 1)

    def oil(self, role="tech"):
        return self.login(role).get(f"/assets/{self.aid}/oil").get_data(as_text=True)

    def test_red_note_button_and_three_boxes(self):
        self.history(9, 3)
        html = self.oil()
        self.assertIn('id="oil-note"', html)
        self.assertIn("alert-danger", html)
        self.assertIn("Add to discrepancy", html)
        self.assertIn("Lately", html)
        self.assertNotIn("Top-off Events", html)
        self.assertNotIn("Quarts / Top-off", html)
        plane = self.login("tech").get(f"/assets/{self.aid}").get_data(as_text=True)
        self.assertIn("Oil burn up:", plane)
        self.assertIn("1 qt / 3.0 hrs lately (normal 9.0)", plane)

    def test_button_files_one_squawk(self):
        self.history(9, 3)
        c = self.login("tech")
        c.post(f"/assets/{self.aid}/oil/discrepancy")
        c.post(f"/assets/{self.aid}/oil/discrepancy")
        rows = self.q("SELECT notes FROM plane_squawks WHERE asset_id = ?", (self.aid,))
        self.assertEqual(len(rows), 1)
        self.assertIn("Oil burn up", rows[0]["notes"])
        self.assertIn("Already added to discrepancies", self.oil())

    def test_yellow_has_no_button(self):
        self.history(9, 5)
        html = self.oil()
        self.assertIn("alert-warning", html)
        self.assertNotIn("Add to discrepancy", html)

    def test_steady_or_short_history_has_no_note(self):
        self.history(9, 9)
        self.assertNotIn('id="oil-note"', self.oil())
        self.assertNotIn("Oil burn up:", self.login("tech").get(f"/assets/{self.aid}").get_data(as_text=True))

    def test_not_enough_history(self):
        for _ in range(2):
            self.flight(9, 1)
        for _ in range(3):
            self.flight(2, 1)
        self.assertNotIn('id="oil-note"', self.oil())
