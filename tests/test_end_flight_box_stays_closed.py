"""QA fix: for a student with no ACS course, after Confirm Hobbs and Tach the End Flight box stays
closed and a green 'Flight ended' line with Edit shows above End Session."""
from harness import OpsHubTestCase
import db


class EndFlightBoxStaysClosedTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("cfi")
        cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        stu = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.fid = self.exec(
            "INSERT INTO flights (asset_id, cfi_id, student_id, started_at, paused_seconds, flight_date, "
            "hobbs_start, tach_start) VALUES (?,?,?,?,0,?,1000.0,850.0)",
            (self.make_asset("N123"), cfi_id, stu, db.now_iso(), db.now_iso()[:10]))

    def test_closed_after_confirm_with_green_line(self):
        self.client.post(f"/flight/log/{self.fid}/end_flight_started")
        r = self.client.post(f"/flight/log/{self.fid}/end_flight",
                             data={"hobbs_end": "1001.2", "tach_end": "851.0"}, follow_redirects=True)
        self.assertNotIn(b'data-auto-open="1"', r.data)
        self.assertIn(b"Flight ended &middot; Hobbs 1001.2 &middot; Tach 851.0", r.data)
        r = self.client.get("/flight/log/active")
        self.assertNotIn(b'data-auto-open="1"', r.data)

    def test_no_green_line_before_confirm(self):
        r = self.client.get("/flight/log/active")
        self.assertNotIn(b"Flight ended &middot;", r.data)
