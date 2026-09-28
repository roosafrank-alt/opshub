"""Idea "todays view": the Today's Schedule chip rows gave every plane in
the fleet a fixed grid column, so a lone flight on a plane that wasn't
first alphabetically still rendered pushed over, with a blank gap in front
of it. Chips now just pack left, right after their time label, with no
grid-column positioning and no plane_lane concept at all."""
from harness import OpsHubTestCase
import db


class DashboardTodayChipsNoGapTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        # Two flight-school planes, alphabetically N first then Z, so a lone
        # flight on the second one would have been pushed into column 2
        # under the old fixed-lane grid.
        self.plane_a = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", ("N100AA", "Cessna 172", 1, 1000.0, 900.0, db.now_iso(), db.now_iso()))
        self.plane_z = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", ("N999ZZ", "Cessna 172", 1, 1000.0, 900.0, db.now_iso(), db.now_iso()))
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE username = 'cfi'")["id"]
        self.student_id = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]

    def test_a_lone_flight_on_the_second_plane_has_no_grid_column_style(self):
        self.exec(
            "INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date, scheduled_time, "
            "duration_hours, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (self.plane_z, self.cfi_id, self.student_id, db.now_iso()[:10], "14:00", 1.5, "scheduled", db.now_iso()))
        c = self.login("cfi")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("N999ZZ", body)
        self.assertNotIn("grid-column", body)

    def test_flight_chip_grid_is_a_flex_row_not_a_fixed_column_grid(self):
        c = self.login("cfi")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertNotIn("grid-template-columns", body)
