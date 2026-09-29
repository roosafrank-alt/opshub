"""Idea "note during session": a note saved on a running session goes to
Maintenance as a squawk right away, editing it updates the squawk, and the
active-flight page tells the instructor it was sent."""
from datetime import date

import db
from harness import OpsHubTestCase


class NoteDuringSessionTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.asset = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, created_at, updated_at) VALUES (?,?,1,?,?)",
            ("N123NS", "Test Cessna", db.now_iso(), db.now_iso()))
        self.fid = self.exec(
            "INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, solo, started_at, created_at) "
            "VALUES (?,?,?,?,0,?,?)",
            (self.cfi, self.student, self.asset, date.today().isoformat(), db.now_iso(), db.now_iso()))

    def save(self, note):
        return self.login("cfi").post(f"/flight/log/{self.fid}/update_progress", data={"notes": note})

    def test_saved_note_reaches_maintenance_before_session_ends(self):
        self.save("Nose strut looks low")
        self.assertEqual(self.q1("SELECT squawk FROM flights WHERE id = ?", (self.fid,))["squawk"], 1)
        body = self.login("shop_admin").get("/squawks").get_data(as_text=True)
        self.assertIn("Nose strut looks low", body)

    def test_editing_note_updates_the_squawk(self):
        self.save("Nose strut looks low")
        self.save("Nose strut is low, needs service")
        body = self.login("shop_admin").get("/squawks").get_data(as_text=True)
        self.assertIn("Nose strut is low, needs service", body)
        self.assertNotIn("Nose strut looks low", body)

    def test_instructor_sees_sent_label(self):
        self.save("Nose strut looks low")
        body = self.login("cfi").get("/flight/log/active").get_data(as_text=True)
        self.assertIn("Note sent to Maintenance", body)

    def test_clearing_unacknowledged_note_removes_squawk(self):
        self.save("Oops wrong plane")
        self.save("")
        self.assertEqual(self.q1("SELECT squawk FROM flights WHERE id = ?", (self.fid,))["squawk"], 0)
