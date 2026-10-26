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

    def test_each_plane_keeps_its_own_spot_with_blanks(self):
        # Only N22689 is booked: N20267S's spot must still be there (empty),
        # then N22689's chip, then the empty Ground spot.
        asset = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", ("N22689", "Plane", 1, 100.0, 90.0, db.now_iso(), db.now_iso()))
        cfi_id = self.q1("SELECT id FROM cfis WHERE username = 'cfi'")["id"]
        student_id = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]
        tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.exec(
            "INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date, scheduled_time, "
            "duration_hours, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (asset, cfi_id, student_id, tomorrow, "14:00", 1.0, "scheduled", db.now_iso()))
        body = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        i = body.index('class="flight-chip-lanes"')
        row = body[i:i + 60000]
        lanes = row.split('<div class="flight-chip-lane">')[1:4]
        self.assertNotIn(">N22689<", lanes[0])
        self.assertIn(">N22689<", lanes[1])
        self.assertNotIn(">N22689<", lanes[2])

    def test_a_newly_added_plane_gets_its_own_spot_right_of_ground(self):
        tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        cfi_id = self.q1("SELECT id FROM cfis WHERE username = 'cfi'")["id"]
        student_id = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]
        for tag in ("N5569P", "N99999"):
            asset = self.exec(
                "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)", (tag, "Plane", 1, 100.0, 90.0, db.now_iso(), db.now_iso()))
            self.exec(
                "INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date, scheduled_time, "
                "duration_hours, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (asset, cfi_id, student_id, tomorrow, "14:00", 1.0, "scheduled", db.now_iso()))
        body = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        i = body.index('class="flight-chip-lanes"')
        lanes = body[i:i + 60000].split('<div class="flight-chip-lane">')[1:5]
        self.assertEqual(len(lanes), 4)
        self.assertIn(">N5569P<", lanes[2])
        self.assertNotIn(">N99999<", lanes[2])
        self.assertIn(">N99999<", lanes[3])
