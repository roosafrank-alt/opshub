"""QA fix ux-own-plane-oil-box: a student's own plane (the is_owner_placeholder
asset) has no Maintenance page and no oil history, so the Oil Added box never
belonged there in the first place - it's gone from the running-session card,
End Session, Log a Flight, and Schedule Flight's "Session Already Complete?"
log section. The running card's "Add starting info" heading becomes "Add a
note" once Hobbs/Tach and Oil are both gone from it. Even if a stale value
still reaches the server (e.g. the schedule form only hides the box with
CSS rather than removing it), _save_logged_flight must not save it."""
from datetime import date

import db
import flight
from harness import OpsHubTestCase, seed_row


class OwnPlaneOilBoxTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        conn = db.get_db()
        self.own_asset = flight._get_or_create_own_plane_asset(conn, self.student, "N999OWN")
        conn.commit()
        conn.close()

    def start_flight(self, asset_id, **extra):
        cols = dict(cfi_id=self.cfi, student_id=self.student, asset_id=asset_id,
                    flight_date=date.today().isoformat(), solo=0, started_at=db.now_iso(),
                    created_at=db.now_iso())
        cols.update(extra)
        conn = db.get_db()
        fid = seed_row(conn, "flights", **cols)
        conn.commit()
        conn.close()
        return fid

    # --- Running-session "Add starting info" card ---

    def test_running_card_hides_oil_and_renames_to_add_a_note_for_own_plane(self):
        fid = self.start_flight(self.own_asset)
        html = self.login("cfi").get("/flight/log/active").get_data(as_text=True)
        self.assertIn("Add a note", html)
        self.assertNotIn("Add starting info (Hobbs/Tach, oil, note)", html)
        self.assertNotIn('name="oil_added_qt"', html)

    def test_running_card_still_shows_oil_for_a_real_plane(self):
        real_asset = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, created_at, updated_at) VALUES (?,?,1,?,?)",
            ("N123", "N123", db.now_iso(), db.now_iso()))
        fid = self.start_flight(real_asset)
        html = self.login("cfi").get("/flight/log/active").get_data(as_text=True)
        self.assertIn("Add starting info (Hobbs/Tach, oil, note)", html)
        self.assertIn('name="oil_added_qt"', html)

    # --- End Session ---

    def test_end_session_hides_oil_box_for_own_plane(self):
        fid = self.start_flight(self.own_asset, stopped_at=db.now_iso())
        html = self.login("cfi").get("/flight/log/active").get_data(as_text=True)
        self.assertNotIn('name="oil_added_qt"', html)
        self.assertIn("Recorded Time", html)

    def test_end_session_ignores_a_posted_oil_value_for_own_plane(self):
        fid = self.start_flight(self.own_asset, stopped_at=db.now_iso())
        r = self.login("cfi").post(f"/flight/log/{fid}/end", data={
            "recorded_hours": "1.5", "paid": "0", "oil_added_qt": "2.0",
        })
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT oil_added_qt FROM flights WHERE id = ?", (fid,))
        self.assertIsNone(row["oil_added_qt"])

    # --- Log a Flight ---

    def test_log_flight_page_hides_oil_box_for_an_own_plane_booking(self):
        conn = db.get_db()
        day = date.today().isoformat()
        sched = seed_row(conn, "scheduled_flights", asset_id=self.own_asset, student_id=self.student,
                         cfi_id=self.cfi, scheduled_date=day, scheduled_time="09:00",
                         duration_hours=1.5, status="scheduled")
        conn.commit()
        conn.close()
        html = self.login("cfi").get(
            f"/flight/log/new?asset_id={self.own_asset}&student_id={self.student}&cfi_id={self.cfi}"
            f"&flight_date={day}&scheduled_flight_id={sched}").get_data(as_text=True)
        self.assertIn("Student's own plane", html)
        self.assertNotIn('name="oil_added_qt"', html)

    def test_log_flight_saves_no_oil_for_own_plane_even_if_posted(self):
        r = self.login("cfi").post("/flight/log/new", data={
            "own_plane": "1", "student_id": str(self.student), "cfi_id": str(self.cfi),
            "flight_date": date.today().isoformat(), "recorded_hours": "1.2", "paid": "",
            "oil_added_qt": "1.5",
        })
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT oil_added_qt FROM flights WHERE student_id = ? AND asset_id = ?",
                       (self.student, self.own_asset))
        self.assertIsNone(row["oil_added_qt"])

    # --- Schedule Flight, "Session Already Complete?" (the booking's log
    # section) - same _save_logged_flight backend as Log a Flight. ---

    def test_schedule_already_complete_saves_no_oil_for_own_plane_even_if_posted(self):
        r = self.login("cfi").post("/flight/schedule/new", data={
            "already_complete": "1", "own_plane": "1", "student_id": str(self.student), "cfi_id": str(self.cfi),
            "scheduled_date": date.today().isoformat(), "recorded_hours": "1.0", "paid": "",
            "oil_added_qt": "3.0",
        })
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT oil_added_qt FROM flights WHERE student_id = ? AND asset_id = ?",
                       (self.student, self.own_asset))
        self.assertIsNotNone(row)
        self.assertIsNone(row["oil_added_qt"])
