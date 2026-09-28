"""Idea "phone view upcoming" (revision): Frank wants four flights sharing a
scheduled time to always line up left-to-right in the same fixed plane order
(N20267S, N22689, AATD Redbird, N5569P), not whatever order the database
query happens to return - see DASHBOARD_PLANE_ORDER / _group_by_time in
flight.py."""
from datetime import date, timedelta

from harness import OpsHubTestCase
import db


class DashboardSameTimePlaneOrderTest(OpsHubTestCase):
    def test_same_time_flights_render_in_franks_fixed_plane_order(self):
        # Inserted in a scrambled order, deliberately not matching the fixed
        # list, so a passing test can't be an accident of insertion order.
        tags = ["N5569P", "AATD Redbird", "N20267S", "N22689"]
        assets = {}
        for tag in tags:
            assets[tag] = self.exec(
                "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)", (tag, "Plane", 1, 100.0, 90.0, db.now_iso(), db.now_iso()))
        cfi_id = self.q1("SELECT id FROM cfis WHERE username = 'cfi'")["id"]
        student_id = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]
        tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        for tag in tags:
            self.exec(
                "INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date, scheduled_time, "
                "duration_hours, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (assets[tag], cfi_id, student_id, tomorrow, "14:00", 1.0, "scheduled", db.now_iso()))
        c = self.login("cfi")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        positions = {tag: body.index(f">{tag}<") for tag in tags}
        ordered = sorted(tags, key=lambda t: positions[t])
        self.assertEqual(ordered, ["N20267S", "N22689", "AATD Redbird", "N5569P"])
