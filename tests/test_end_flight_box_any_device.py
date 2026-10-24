"""Idea 'view after flight is ended': once End Flight has been opened, the box reopens on any
device (the state is stored on the flight) until the ACS planning step is saved."""
from harness import OpsHubTestCase
import db


class EndFlightAnyDeviceTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("cfi")
        cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        stu = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.fid = self.exec(
            "INSERT INTO flights (asset_id, cfi_id, student_id, started_at, paused_seconds, flight_date) "
            "VALUES (?,?,?,?,0,?)",
            (self.make_asset("N123"), cfi_id, stu, db.now_iso(), db.now_iso()[:10]))

    def test_box_reopens_after_it_was_started(self):
        r = self.client.get("/flight/log/active")
        self.assertIn(b"end-flight-modal-%d" % self.fid, r.data)
        self.assertNotIn(b'data-auto-open="1"', r.data)
        self.assertEqual(self.client.post(f"/flight/log/{self.fid}/end_flight_started").status_code, 204)
        self.assertIsNotNone(self.q1("SELECT end_started_at FROM flights WHERE id = ?", (self.fid,))["end_started_at"])
        r = self.client.get("/flight/log/active")
        self.assertIn(b'data-auto-open="1"', r.data)

    def test_box_stays_closed_once_planning_saved(self):
        self.client.post(f"/flight/log/{self.fid}/end_flight_started")
        self.exec("UPDATE flights SET acs_planned_at = ? WHERE id = ?", (db.now_iso(), self.fid))
        r = self.client.get("/flight/log/active")
        self.assertNotIn(b'data-auto-open="1"', r.data)
