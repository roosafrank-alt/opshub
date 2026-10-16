"""Running late (active flight): the CFI enters minutes late; the NEXT booking on
the same plane (student and instructor) is told N min late + new ETA, and the
optional note goes to that booking's instructor only, never the student."""
from datetime import date

from harness import OpsHubTestCase, seed_row
import db


class RunningLateTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.next_cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi_billing"]["id"],))["id"]
        self.next_student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        conn = db.get_db()
        self.pilot_student = seed_row(conn, "students", name="Pilot Student", active=1)
        today = date.today().isoformat()
        own = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.pilot_student, cfi_id=self.cfi,
                       scheduled_date=today, scheduled_time="00:01", duration_hours=1, status="scheduled")
        self.next_sf = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.next_student,
                                cfi_id=self.next_cfi, scheduled_date=today, scheduled_time="23:00",
                                duration_hours=1, status="scheduled")
        self.fid = seed_row(conn, "flights", asset_id=self.plane, cfi_id=self.cfi, student_id=self.pilot_student,
                            started_at=db.now_iso(), paused_seconds=0, flight_date=today, scheduled_flight_id=own)
        conn.commit()
        conn.close()

    def pushes(self, role):
        return [r["body"] for r in self.q("SELECT body FROM push_pending WHERE user_id = ?", (self.users[role]["id"],))]

    def test_note_goes_to_next_instructor_only(self):
        c = self.login("cfi")
        r = c.post(f"/flight/log/{self.fid}/running_late", data={"late_minutes": "20", "late_note": "Fuel stop, sorry"})
        self.assertEqual(r.status_code, 302)
        instr = self.pushes("cfi_billing")
        self.assertEqual(len(instr), 1)
        self.assertIn("20 minutes late", instr[0])
        self.assertIn("Fuel stop, sorry", instr[0])
        stud = self.pushes("flight_student")
        self.assertEqual(len(stud), 1)
        self.assertIn("20 minutes late", stud[0])
        self.assertNotIn("Fuel stop", stud[0])
        feed = self.q("SELECT message FROM student_notifications WHERE student_id = ?", (self.next_student,))
        self.assertEqual(len(feed), 1)
        self.assertNotIn("Fuel stop", feed[0]["message"])
        self.assertIsNotNone(self.q1("SELECT eta_at FROM flights WHERE id = ?", (self.fid,))["eta_at"])

    def test_sender_still_sees_note(self):
        c = self.login("cfi")
        c.post(f"/flight/log/{self.fid}/running_late", data={"late_minutes": "20", "late_note": "Fuel stop, sorry"})
        html = c.get("/flight/log/active").get_data(as_text=True)
        self.assertIn("running-late-sent", html)
        self.assertIn("Note to next instructor: Fuel stop, sorry", html)

    def test_needs_minutes(self):
        self.login("cfi").post(f"/flight/log/{self.fid}/running_late", data={"late_minutes": ""})
        self.assertEqual(self.pushes("cfi_billing"), [])
