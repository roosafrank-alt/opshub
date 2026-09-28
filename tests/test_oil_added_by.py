"""Idea "oil" (log by who): whenever Oil Added is recorded on a flight -
mid-flight progress update, End Session, Log a Flight, or editing a logged
flight - flights.oil_added_by now records who entered it (the logged-in
user's name), next to the existing oil_added_qt. Shown on the plane's
Maintenance page, the Full Oil Report and a flight's own detail page.
Mirrors the existing hobbs_updated_by/tach_updated_by pattern (see
test_hours_updated_by.py)."""
from harness import OpsHubTestCase
import db


class OilAddedByTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("N81PA", "Piper PA-28-181 Archer III", 1, 500.0, 480.0, db.now_iso(), db.now_iso()))
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE username = 'cfi'")["id"]
        self.student_id = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]

    def start_flight(self, **overrides):
        cols = dict(asset_id=self.asset_id, cfi_id=self.cfi_id, student_id=self.student_id,
                    started_at=db.now_iso(), paused_seconds=0, flight_date=db.now_iso()[:10],
                    hobbs_start=500.0, tach_start=480.0)
        cols.update(overrides)
        names = ",".join(cols)
        marks = ",".join("?" * len(cols))
        return self.exec(f"INSERT INTO flights ({names}) VALUES ({marks})", list(cols.values()))

    def flight_row(self, flight_id):
        return self.q1("SELECT * FROM flights WHERE id = ?", (flight_id,))

    # ----- mid-flight progress update -------------------------------------
    def test_progress_update_tags_who_entered_oil(self):
        flight_id = self.start_flight()
        c = self.login("cfi")
        c.post(f"/flight/log/{flight_id}/update_progress", data=dict(oil_added_qt="1.5", notes=""))
        row = self.flight_row(flight_id)
        self.assertEqual(row["oil_added_qt"], 1.5)
        self.assertEqual(row["oil_added_by"], "Cfi")

    def test_progress_update_without_oil_leaves_it_blank(self):
        flight_id = self.start_flight()
        c = self.login("cfi")
        c.post(f"/flight/log/{flight_id}/update_progress", data=dict(oil_added_qt="", notes=""))
        row = self.flight_row(flight_id)
        self.assertIsNone(row["oil_added_qt"])
        self.assertIsNone(row["oil_added_by"])

    # ----- End Session ------------------------------------------------
    def test_end_session_tags_who_entered_oil(self):
        flight_id = self.start_flight()
        c = self.login("cfi")
        c.post(f"/flight/log/{flight_id}/end", data=dict(
            hobbs_end="502.0", tach_end="481.5", oil_added_qt="2", paid="0"))
        row = self.flight_row(flight_id)
        self.assertEqual(row["oil_added_qt"], 2.0)
        self.assertEqual(row["oil_added_by"], "Cfi")

    def test_end_session_keeps_mid_flight_oil_when_form_blank(self):
        # Idea "adding oil": oil added mid-flight (log_update_progress) must
        # survive End Session even when its form doesn't send a new
        # oil_added_qt (blank field, or no field at all) - it used to get
        # overwritten back to NULL, losing the earlier entry.
        flight_id = self.start_flight()
        c = self.login("cfi")
        c.post(f"/flight/log/{flight_id}/update_progress", data=dict(oil_added_qt="1", notes=""))
        c.post(f"/flight/log/{flight_id}/end", data=dict(
            hobbs_end="502.0", tach_end="481.5", oil_added_qt="", paid="0"))
        row = self.flight_row(flight_id)
        self.assertEqual(row["oil_added_qt"], 1.0)
        self.assertEqual(row["oil_added_by"], "Cfi")

    def test_end_session_can_still_replace_mid_flight_oil(self):
        flight_id = self.start_flight()
        c = self.login("cfi")
        c.post(f"/flight/log/{flight_id}/update_progress", data=dict(oil_added_qt="1", notes=""))
        c.post(f"/flight/log/{flight_id}/end", data=dict(
            hobbs_end="502.0", tach_end="481.5", oil_added_qt="2.5", paid="0"))
        row = self.flight_row(flight_id)
        self.assertEqual(row["oil_added_qt"], 2.5)
        self.assertEqual(row["oil_added_by"], "Cfi")

    # ----- Log a Flight (after the fact) ----------------------------------
    def test_log_new_tags_who_entered_oil(self):
        c = self.login("cfi")
        c.post("/flight/log/new", data=dict(
            asset_id=self.asset_id, student_id=self.student_id, cfi_id=self.cfi_id,
            flight_date="2026-09-27", hobbs_end="502.0", tach_end="481.5", oil_added_qt="1"))
        row = self.q1("SELECT * FROM flights WHERE asset_id = ?", (self.asset_id,))
        self.assertEqual(row["oil_added_qt"], 1.0)
        self.assertEqual(row["oil_added_by"], "Cfi")

    def test_log_new_without_oil_leaves_it_blank(self):
        c = self.login("cfi")
        c.post("/flight/log/new", data=dict(
            asset_id=self.asset_id, student_id=self.student_id, cfi_id=self.cfi_id,
            flight_date="2026-09-27", hobbs_end="502.0", tach_end="481.5"))
        row = self.q1("SELECT * FROM flights WHERE asset_id = ?", (self.asset_id,))
        self.assertIsNone(row["oil_added_qt"])
        self.assertIsNone(row["oil_added_by"])

    # ----- editing a logged flight -----------------------------------------
    def test_editing_a_flight_tags_who_entered_oil(self):
        flight_id = self.exec(
            "INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, hobbs_start, hobbs_end, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.cfi_id, self.student_id, self.asset_id, "2026-09-20", 500.0, 501.5, db.now_iso()))
        c = self.login("master")
        c.post(f"/flight/log/{flight_id}/edit", data=dict(
            asset_id=self.asset_id, student_id=self.student_id, cfi_id=self.cfi_id,
            flight_date="2026-09-20", hobbs_start="500.0", hobbs_end="501.5", oil_added_qt="0.5"))
        row = self.flight_row(flight_id)
        self.assertEqual(row["oil_added_qt"], 0.5)
        self.assertEqual(row["oil_added_by"], "Master")

    # ----- shown on the plane's Maintenance page and Full Oil Report -------
    def test_shown_on_asset_maintenance_page(self):
        self.start_flight(ended_at=db.now_iso(), oil_added_qt=1.5, oil_added_by="Cfi")
        html = self.login("tech").get(f"/assets/{self.asset_id}").get_data(as_text=True)
        self.assertIn("Cfi", html)

    def test_shown_on_full_oil_report(self):
        self.start_flight(ended_at=db.now_iso(), oil_added_qt=1.5, oil_added_by="Cfi")
        html = self.login("tech").get(f"/assets/{self.asset_id}/oil").get_data(as_text=True)
        self.assertIn("Cfi", html)

    def test_shown_on_flight_detail_page(self):
        flight_id = self.start_flight(ended_at=db.now_iso(), oil_added_qt=1.5, oil_added_by="Cfi")
        html = self.login("cfi").get(f"/flight/log/{flight_id}").get_data(as_text=True)
        self.assertIn("logged by Cfi", html)
