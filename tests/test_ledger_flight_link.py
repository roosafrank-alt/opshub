"""Idea 'View': a flight's row in a student's account ledger links to that flight's full details."""
from harness import OpsHubTestCase
import db


class LedgerFlightLinkTest(OpsHubTestCase):
    def test_flight_number_links_to_flight_details(self):
        self.login("master")
        stu = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        sid = self.exec("INSERT INTO scheduled_flights (asset_id, student_id, scheduled_date, status, flight_no) "
                        "VALUES (?,?,?,?,?)", (self.make_asset("N123"), stu, db.now_iso()[:10], "completed", 140))
        fid = self.exec("INSERT INTO flights (asset_id, student_id, started_at, paused_seconds, flight_date, scheduled_flight_id) "
                        "VALUES (?,?,?,0,?,?)", (self.make_asset("N456"), stu, db.now_iso(), db.now_iso()[:10], sid))
        self.exec("INSERT INTO student_ledger (student_id, entry_type, amount, flight_id, note, created_at) VALUES (?,?,?,?,?,?)",
                  (stu, "flight_deduction", -10, fid, "Flight logged", db.now_iso()))
        r = self.client.get(f"/flight/students/{stu}/edit")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'href="/flight/log/%d"' % fid, r.data)
        self.assertIn(b"Flight #140", r.data)
