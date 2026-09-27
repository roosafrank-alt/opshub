"""Idea 'Floating timer' (multiple revisions): the floating active-flight
widget (templates/flight/_active_flight_widget.html) must act directly on
the real flight it follows, not keep its own independent clock - Pause
posts to flight.log_pause/log_resume and End posts to flight.log_stop, the
same routes the Active Flight page's own buttons use. This was reported
broken twice (pause not reaching the flight, then end not reaching it);
these tests pin down that all three now touch the real flights row."""
from harness import OpsHubTestCase
import db


class ActiveFlightWidgetTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("cfi")
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?",
                                   (self.users["flight_student"]["id"],))["id"]
        self.asset_id = self.make_asset("N123")

    def start_flight(self):
        return self.exec(
            "INSERT INTO flights (asset_id, cfi_id, student_id, started_at, paused_seconds, flight_date) "
            "VALUES (?,?,?,?,0,?)",
            (self.asset_id, self.cfi_id, self.student_id, db.now_iso(), db.now_iso()[:10]))

    def test_pause_pauses_the_real_flight(self):
        fid = self.start_flight()
        self.client.post(f"/flight/log/{fid}/pause")
        f = self.q1("SELECT paused_at FROM flights WHERE id = ?", (fid,))
        self.assertIsNotNone(f["paused_at"])

    def test_resume_clears_the_pause_on_the_real_flight(self):
        fid = self.start_flight()
        self.client.post(f"/flight/log/{fid}/pause")
        self.client.post(f"/flight/log/{fid}/resume")
        f = self.q1("SELECT paused_at, paused_seconds FROM flights WHERE id = ?", (fid,))
        self.assertIsNone(f["paused_at"])
        self.assertGreaterEqual(f["paused_seconds"], 0)

    def test_end_stops_the_real_flights_clock(self):
        fid = self.start_flight()
        self.client.post(f"/flight/log/{fid}/stop")
        f = self.q1("SELECT stopped_at, ended_at FROM flights WHERE id = ?", (fid,))
        self.assertIsNotNone(f["stopped_at"])
        self.assertIsNone(f["ended_at"])  # still needs Hobbs/paid on Active Flight to fully end

    def test_widget_only_shows_for_a_flight_still_running(self):
        fid = self.start_flight()
        r = self.client.get("/flight/log/active")
        self.assertIn(b"active-flight-widget", r.data)
        self.client.post(f"/flight/log/{fid}/stop")
        self.exec("UPDATE flights SET ended_at = ? WHERE id = ?", (db.now_iso(), fid))
        r = self.client.get("/flight/log/active")
        self.assertNotIn(b"active-flight-widget", r.data)
