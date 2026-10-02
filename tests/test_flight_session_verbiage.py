"""Idea "Flight verbiage": a scheduled block can be started before the
actual flight (ground instruction) or ended after it (CFI time), so the
Start/End/Complete wording around it now says "session" instead of
"flight" - the flight-specific record/history pages (Active Flight, Flight
History, Log a Flight) keep their names."""
from datetime import datetime, timedelta
from harness import OpsHubTestCase, seed_row
import db


class FlightSessionVerbiageTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        conn = db.get_db()
        self.student_id = conn.execute("SELECT id FROM students WHERE user_id = ?",
                                       (self.users["flight_student"]["id"],)).fetchone()["id"]
        self.cfi_id = conn.execute("SELECT id FROM cfis WHERE user_id = ?",
                                   (self.users["cfi"]["id"],)).fetchone()["id"]
        conn.close()

    def test_starting_a_lesson_says_session_started(self):
        when = datetime.now()
        conn = db.get_db()
        sid = seed_row(conn, "scheduled_flights", asset_id=self.asset_id, student_id=self.student_id,
                       cfi_id=self.cfi_id, scheduled_date=when.strftime("%Y-%m-%d"),
                       scheduled_time=when.strftime("%H:%M"), status="scheduled")
        conn.commit()
        conn.close()
        c = self.login("cfi")
        r = c.post(f"/flight/schedule/{sid}/start", follow_redirects=True)
        self.assertIn("Session started", r.get_data(as_text=True))
        self.assertNotIn("Flight started", r.get_data(as_text=True))

    def test_ending_a_session_says_session_ended_and_logged(self):
        c = self.login("cfi")
        conn = db.get_db()
        flight_id = self.exec(
            """INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, hobbs_start, tach_start, solo,
               started_at, stopped_at, created_at)
               VALUES (?,?,?,?,?,?,0,?,?,?)""",
            (self.cfi_id, self.student_id, self.asset_id, db.now_iso()[:10], 100.0, 200.0,
             db.now_iso(), db.now_iso(), db.now_iso()))
        conn.close()
        r = c.post(f"/flight/log/{flight_id}/end",
                   data={"hobbs_end": "101.0", "tach_end": "201.0", "paid": "1", "payment_method": "cash"},
                   follow_redirects=True)
        self.assertIn("Session ended and logged", r.get_data(as_text=True))

    def test_active_flight_page_still_says_session_not_flight_on_its_buttons(self):
        c = self.login("cfi")
        self.exec(
            """INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, hobbs_start, tach_start, solo,
               started_at, created_at)
               VALUES (?,?,?,?,?,?,0,?,?)""",
            (self.cfi_id, self.student_id, self.asset_id, db.now_iso()[:10], 100.0, 200.0,
             db.now_iso(), db.now_iso()))
        body = c.get("/flight/log/active").get_data(as_text=True)
        self.assertIn("End Session", body)
        self.assertNotIn("End Flight", body)
        # The page itself (an in-progress list, flight or ground) keeps its name.
        self.assertIn("Active Flight", body)

    def test_schedule_form_complete_checkbox_says_session(self):
        c = self.login("cfi")
        body = c.get("/flight/schedule/new").get_data(as_text=True)
        # FLY-13: one name for logging a past session.
        self.assertIn("Log a past session instead of booking", body)
        self.assertNotIn("Session Already Complete?", body)
        self.assertNotIn("Flight Already Complete?", body)

    def test_next_lesson_card_start_button_says_start_session(self):
        when = datetime.now()
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=self.asset_id, student_id=self.student_id,
                cfi_id=self.cfi_id, scheduled_date=when.strftime("%Y-%m-%d"),
                scheduled_time=when.strftime("%H:%M"), status="scheduled")
        conn.commit()
        conn.close()
        c = self.login("cfi")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Start Session", body)
        self.assertNotIn("Start Flight", body)
