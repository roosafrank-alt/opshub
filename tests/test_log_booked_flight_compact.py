"""QA finding ux-log-booked-flight-compact: opening Log a Flight from a
booking pre-filled the plane/student/instructor/date fields, but a CFI on a
phone still had to scroll past all of them (and everything below) to reach
Hobbs End and then Log Flight. The booking now shows as one compact line
with a Change button, Hobbs/Tach come right under it, and Log Flight stays
pinned to the bottom of the screen (see log_new() in flight.py and
templates/flight/log_new.html)."""
from harness import OpsHubTestCase
import db


class LogBookedFlightCompactTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("N81PA", "Piper PA-28-181 Archer III", 1, db.now_iso(), db.now_iso()))
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE username = 'cfi'")["id"]
        self.student_id = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]

    def booking_url(self, **overrides):
        params = dict(asset_id=self.asset_id, student_id=self.student_id, cfi_id=self.cfi_id,
                      solo="", flight_date="2026-09-27", scheduled_flight_id="1")
        params.update(overrides)
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"/flight/log/new?{qs}"

    def test_opened_from_a_booking_shows_compact_summary(self):
        c = self.login("cfi")
        body = c.get(self.booking_url()).get_data(as_text=True)
        self.assertIn("N81PA", body)
        self.assertIn("Flight Student", body)
        self.assertIn("with Cfi", body)
        self.assertIn("09/27/2026", body)
        self.assertIn("dual", body)
        self.assertIn("Change", body)

    def test_booking_fields_are_hidden_by_default(self):
        c = self.login("cfi")
        body = c.get(self.booking_url()).get_data(as_text=True)
        self.assertIn('id="booking-fields" class="d-none"', body)

    def test_hobbs_comes_before_the_change_button_reveals_pickers(self):
        c = self.login("cfi")
        body = c.get(self.booking_url()).get_data(as_text=True)
        self.assertLess(body.index("booking-fields"), body.index("Hobbs Start"))

    def test_log_flight_button_is_pinned_on_a_booking(self):
        c = self.login("cfi")
        body = c.get(self.booking_url()).get_data(as_text=True)
        self.assertIn("log-flight-sticky", body)

    def test_plain_log_a_flight_unaffected(self):
        c = self.login("cfi")
        body = c.get("/flight/schedule/new?complete=1").get_data(as_text=True)
        self.assertNotIn("log-flight-sticky", body)

    def test_editing_a_logged_flight_unaffected(self):
        flight_id = self.exec(
            """INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, hobbs_start, hobbs_end, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (self.cfi_id, self.student_id, self.asset_id, "2026-09-20", 100.0, 101.5, db.now_iso()))
        c = self.login("cfi")
        body = c.get(f"/flight/log/{flight_id}/edit").get_data(as_text=True)
        self.assertIn('id="booking-fields" class=""', body)
        self.assertNotIn("log-flight-sticky", body)

    def test_booking_summary_shows_solo(self):
        c = self.login("cfi")
        body = c.get(self.booking_url(solo="1")).get_data(as_text=True)
        self.assertIn("solo", body)
