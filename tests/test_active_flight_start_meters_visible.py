"""Idea "session": Hobbs/Tach Start are locked (can't be typed over by the
student or CFI - see test_flight_start_meters_locked.py), but the Active
Flight page's "Add starting info" panel used to hide them entirely once
they were on file, so there was no way to check them against the plane's
gauges mid-flight. They now show, read-only, above Oil Added whenever
they're already set."""
from harness import OpsHubTestCase
import db


class ActiveFlightStartMetersVisibleTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("cfi")
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?",
                                   (self.users["flight_student"]["id"],))["id"]
        self.asset_id = self.make_asset("N123")

    def start_flight(self, hobbs_start=None, tach_start=None):
        return self.exec(
            "INSERT INTO flights (asset_id, cfi_id, student_id, started_at, paused_seconds, flight_date, "
            "hobbs_start, tach_start) VALUES (?,?,?,?,0,?,?,?)",
            (self.asset_id, self.cfi_id, self.student_id, db.now_iso(), db.now_iso()[:10],
             hobbs_start, tach_start))

    def test_hobbs_and_tach_start_show_read_only_above_oil_added_once_on_file(self):
        self.start_flight(hobbs_start=1233.0, tach_start=1200.5)
        html = self.client.get("/flight/log/active").get_data(as_text=True)
        oil_pos = html.index("Oil Added")
        hobbs_pos = html.index("1233.0")
        tach_pos = html.index("1200.5")
        self.assertLess(hobbs_pos, oil_pos)
        self.assertLess(tach_pos, oil_pos)
        self.assertIn('value="1233.0" disabled', html)
        self.assertIn('value="1200.5" disabled', html)
        self.assertNotIn('name="hobbs_start"', html)
        self.assertNotIn('name="tach_start"', html)

    def test_hobbs_and_tach_start_stay_editable_when_not_yet_on_file(self):
        self.start_flight(hobbs_start=None, tach_start=None)
        html = self.client.get("/flight/log/active").get_data(as_text=True)
        self.assertIn('name="hobbs_start"', html)
        self.assertIn('name="tach_start"', html)
