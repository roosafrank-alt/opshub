"""Logbook times of a school flight are locked to the billed flight: students (and admins) can't
pad them, sub-times can't exceed the total, and a correction on the flight flows into approved entries."""
from harness import OpsHubTestCase
import db
import pilotlog


class LogbookTimesLockedTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        plane = self.make_asset("N123")
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.fid = self.exec("""INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, solo, hobbs_start, hobbs_end, created_at)
                                VALUES (?,?,?,?,0,10.0,11.5,'x')""", (cfi, self.sid, plane, "2026-10-20"))
        conn = db.get_db()
        pilotlog.ensure_entry(conn, self.fid)
        conn.commit(); conn.close()
        self.eid = self.q1("SELECT id FROM pilot_logbook WHERE flight_id = ?", (self.fid,))["id"]
        self.exec("UPDATE users SET academy_access = 1 WHERE username IN ('flight_student')")

    def entry(self):
        return self.q1("SELECT * FROM pilot_logbook WHERE id = ?", (self.eid,))

    def test_times_come_from_the_flight(self):
        e = self.entry()
        self.assertEqual((e["total"], e["dual"]), (1.5, 1.5))

    def test_student_cannot_pad_total_or_sub_times(self):
        c = self.login("flight_student")
        c.post(f"/academy/logbook/{self.eid}", data={"total": "99", "dual": "99", "solo": "50", "pic": "40", "xc": "30",
                                                      "night": "20", "actual_inst": "1.0", "sim_inst": "5.0"})
        e = self.entry()
        self.assertEqual((e["total"], e["dual"], e["solo"]), (1.5, 1.5, 0.0))
        self.assertLessEqual(e["pic"], 1.5); self.assertLessEqual(e["xc"], 1.5); self.assertLessEqual(e["night"], 1.5)
        self.assertLessEqual((e["actual_inst"] or 0) + (e["sim_inst"] or 0), 1.5)

    def test_admin_cannot_type_the_times_either(self):
        c = self.login("flight_admin")
        c.post(f"/academy/logbook/{self.eid}", data={"total": "99", "dual": "99", "solo": "0"})
        self.assertEqual(self.entry()["total"], 1.5)

    def test_correcting_the_flight_updates_even_an_approved_entry(self):
        c = self.login("flight_student")
        c.post(f"/academy/logbook/{self.eid}", data={"action": "approve", "xc": "1.4"})
        self.assertEqual(self.entry()["status"], "complete")
        self.exec("UPDATE flights SET hobbs_end = 11.0 WHERE id = ?", (self.fid,))
        conn = db.get_db(); pilotlog.ensure_entry(conn, self.fid); conn.commit(); conn.close()
        e = self.entry()
        self.assertEqual(e["total"], 1.0)
        self.assertLessEqual(e["xc"], 1.0)
        self.assertEqual(e["status"], "complete")

    def test_page_says_it_is_locked(self):
        self.assertIn("Locked", self.login("flight_student").get(f"/academy/logbook/{self.eid}").get_data(as_text=True))
