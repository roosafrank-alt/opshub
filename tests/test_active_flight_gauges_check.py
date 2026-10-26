"""Idea "on an active flight page": a check box to confirm the Hobbs and Tach
start match the gauges, and a note for when they are off."""
from harness import OpsHubTestCase
import db


class ActiveFlightGaugesCheckTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("cfi")
        cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        student_id = self.q1("SELECT id FROM students WHERE user_id = ?",
                             (self.users["flight_student"]["id"],))["id"]
        asset_id = self.make_asset("N123")
        self.fid = self.exec(
            "INSERT INTO flights (asset_id, cfi_id, student_id, started_at, paused_seconds, flight_date, "
            "hobbs_start, tach_start) VALUES (?,?,?,?,0,?,1233.0,1200.5)",
            (asset_id, cfi_id, student_id, db.now_iso(), db.now_iso()[:10]))

    def row(self):
        return self.q1("SELECT gauges_checked_at, gauges_note FROM flights WHERE id = ?", (self.fid,))

    def test_box_and_note_show_unchecked(self):
        html = self.client.get("/flight/log/active").get_data(as_text=True)
        self.assertIn('name="gauges_ok"', html)
        self.assertIn('name="gauges_note"', html)
        self.assertNotIn('name="gauges_ok" value="1" id="gauges_ok_%d" checked' % self.fid, html)

    def test_ticking_saves_and_shows_checked(self):
        self.client.post("/flight/log/%d/update_progress" % self.fid, data={"gauges_ok": "1"})
        r = self.row()
        self.assertTrue(r["gauges_checked_at"])
        self.assertFalse(r["gauges_note"])
        html = self.client.get("/flight/log/active").get_data(as_text=True)
        self.assertIn('id="gauges_ok_%d" checked' % self.fid, html)

    def test_note_saves_and_untick_clears_check(self):
        self.client.post("/flight/log/%d/update_progress" % self.fid, data={"gauges_ok": "1"})
        self.client.post("/flight/log/%d/update_progress" % self.fid, data={"gauges_note": "Hobbs reads 1233.4"})
        r = self.row()
        self.assertIsNone(r["gauges_checked_at"])
        self.assertEqual(r["gauges_note"], "Hobbs reads 1233.4")
