"""Idea "hobbs and tach time": a flight school plane's starting Hobbs/Tach
can't be typed in by a student or instructor. The very first flight logged
for a plane takes its starting readings from the plane's own Hobbs/Tach on
file (the Maintenance side's profile for that aircraft); every flight after
that chains off the previous flight's ending readings instead, since ending
a flight already advances the plane's on-file readings to match (see
_save_logged_flight/log_end in flight.py). Billing still runs off Hobbs
(hobbs_end - hobbs_start) and maintenance tracking still reads the plane's
live Tach, both unchanged."""
from harness import OpsHubTestCase
import db


class FlightStartMetersLockedTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("N81PA", "Piper PA-28-181 Archer III", 1, 500.0, 480.0, db.now_iso(), db.now_iso()))
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE username = 'cfi'")["id"]
        self.student_id = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]

    def _log_a_flight(self, c, **form_overrides):
        form = dict(asset_id=self.asset_id, student_id=self.student_id, cfi_id=self.cfi_id,
                    flight_date="2026-09-27", hobbs_end="502.0", tach_end="481.5")
        form.update(form_overrides)
        return c.post("/flight/log/new", data=form)

    def test_logging_a_flight_ignores_a_posted_starting_hobbs_and_tach(self):
        # A CFI can't just type over Hobbs/Tach Start even by posting
        # directly to the form - it always comes from the plane's own
        # reading on file.
        c = self.login("cfi")
        self._log_a_flight(c, hobbs_start="999.9", tach_start="999.9")
        row = self.q1("SELECT * FROM flights WHERE asset_id = ?", (self.asset_id,))
        self.assertEqual(row["hobbs_start"], 500.0)
        self.assertEqual(row["tach_start"], 480.0)

    def test_a_second_flight_chains_off_the_first_ones_ending_readings(self):
        c = self.login("cfi")
        self._log_a_flight(c, hobbs_end="502.0", tach_end="481.5")
        self._log_a_flight(c, flight_date="2026-09-28", hobbs_end="504.5", tach_end="483.0")
        second = self.q1("SELECT * FROM flights WHERE flight_date = '2026-09-28'")
        self.assertEqual(second["hobbs_start"], 502.0)
        self.assertEqual(second["tach_start"], 481.5)

    def test_starting_a_scheduled_flight_takes_readings_from_the_plane(self):
        scheduled_id = self.exec(
            "INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date, scheduled_time, "
            "duration_hours, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (self.asset_id, self.cfi_id, self.student_id, "2026-09-27", "", 1.0, "scheduled", db.now_iso()))
        c = self.login("cfi")
        r = c.post(f"/flight/schedule/{scheduled_id}/start", data={"hobbs_start": "1.0", "tach_start": "1.0"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM flights WHERE scheduled_flight_id = ?", (scheduled_id,))
        self.assertEqual(row["hobbs_start"], 500.0)
        self.assertEqual(row["tach_start"], 480.0)

    def test_hobbs_start_and_tach_start_fields_are_not_editable_on_log_new(self):
        c = self.login("cfi")
        body = c.get("/flight/schedule/new?complete=1").get_data(as_text=True)
        self.assertIn('id="hobbs-start" class="form-control" disabled', body)
        self.assertIn('id="tach-start" class="form-control" disabled', body)
        self.assertNotIn('name="hobbs_start"', body)
        self.assertNotIn('name="tach_start"', body)

    def test_billing_still_uses_hobbs_and_maintenance_still_sees_the_new_tach(self):
        c = self.login("cfi")
        self._log_a_flight(c, hobbs_end="502.0", tach_end="481.5")
        plane = self.q1("SELECT * FROM assets WHERE id = ?", (self.asset_id,))
        # Maintenance's live meters both advanced to the flight's ending readings.
        self.assertEqual(plane["hobbs_hours"], 502.0)
        self.assertEqual(plane["tach_hours"], 481.5)
        flight = self.q1("SELECT * FROM flights WHERE asset_id = ?", (self.asset_id,))
        self.assertEqual(flight["hobbs_end"] - flight["hobbs_start"], 2.0)
