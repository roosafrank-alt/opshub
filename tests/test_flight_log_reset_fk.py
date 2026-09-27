"""Idea "reset failed": Frank reported Reset Flight Log crashed with "Reset
didn't run - nothing was deleted. (FOREIGN KEY constraint failed)". Cause:
_wipe_flight_log un-links student_ledger.flight_id before deleting a flight,
but never did the same for pilot_logbook.flight_id (a hard, UNIQUE foreign
key to flights - see pilotlog.ensure_entry, called every time a flight is
logged), so deleting a flight with a pilot logbook entry violated the
constraint and rolled the whole reset back."""
from datetime import date

from harness import OpsHubTestCase, seed_row
import db


class FlightLogResetKeepsPilotLogbookTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.asset = self.make_asset("N555PL")
        self.flight_id = self.exec(
            "INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, solo, created_at) VALUES (?,?,?,?,0,?)",
            (self.cfi, self.student, self.asset, date.today().isoformat(), db.now_iso()))
        conn = db.get_db()
        self.entry_id = seed_row(conn, "pilot_logbook", flight_id=self.flight_id, student_id=self.student,
                                  cfi_id=self.cfi, asset_id=self.asset, entry_date=date.today().isoformat())
        conn.commit()
        conn.close()

    def test_reset_flight_log_does_not_crash_with_a_pilot_logbook_entry(self):
        c = self.login("master")
        r = c.post("/admin/reset/flights", follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertNotIn("FOREIGN KEY constraint failed", html)
        self.assertIn("Flight log cleared.", html)
        self.assertIsNone(self.q1("SELECT id FROM flights WHERE id = ?", (self.flight_id,)))

    def test_pilot_logbook_entry_survives_unlinked_not_deleted(self):
        c = self.login("master")
        c.post("/admin/reset/flights")
        entry = self.q1("SELECT * FROM pilot_logbook WHERE id = ?", (self.entry_id,))
        self.assertIsNotNone(entry)
        self.assertIsNone(entry["flight_id"])
