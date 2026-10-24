"""Idea 'if end session is clicked before end flight': until End Flight has confirmed the ending
Hobbs/Tach, End Session shows a reminder pop-up instead of stopping the clock."""
from harness import OpsHubTestCase
import db


class EndSessionNeedsEndFlightTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("cfi")
        cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        stu = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.fid = self.exec(
            "INSERT INTO flights (asset_id, cfi_id, student_id, started_at, paused_seconds, flight_date) "
            "VALUES (?,?,?,?,0,?)",
            (self.make_asset("N123"), cfi_id, stu, db.now_iso(), db.now_iso()[:10]))

    def test_end_session_asks_for_end_flight_first(self):
        r = self.client.get("/flight/log/active")
        self.assertIn(b"end-first-modal-%d" % self.fid, r.data)
        self.assertIn(b"The flight must be ended first", r.data)

    def test_end_session_is_direct_once_end_flight_confirmed(self):
        self.exec("UPDATE flights SET end_confirmed_at = ?, hobbs_end = 1, tach_end = 1 WHERE id = ?", (db.now_iso(), self.fid))
        r = self.client.get("/flight/log/active")
        self.assertNotIn(b"end-first-modal-", r.data)
        self.assertIn(b"/flight/log/%d/stop" % self.fid, r.data)
