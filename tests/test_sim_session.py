"""QA finding ux-sim-session-plane-boxes: a simulator session runs on a
stopwatch, not meters - it has no real Hobbs/Tach and takes no oil, so it
logs a single "Sim Time" box (flights.recorded_hours, same column a
student's own plane uses - see test_own_plane_billing.py) instead. Covers
End Session (log_end, pre-filled from the session clock), the running
Active Flight card (Session clock label, "Sim session" badge, Add a note),
Log a Flight (_save_logged_flight), editing a logged flight (log_edit), and
Flight History showing Sim Time instead of dashes. Real planes and a
student's own plane are unaffected (see test_own_plane_billing.py and the
existing Hobbs/Tach tests)."""
from datetime import date, timedelta

import db
import flight
from harness import OpsHubTestCase, seed_row


class SimSessionTest(OpsHubTestCase):

    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.sim_asset = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, is_simulator, sim_rate, created_at, updated_at) "
            "VALUES (?,?,1,1,?,?,?)", ("REDBIRD", "Redbird FMX", 75.0, db.now_iso(), db.now_iso()))

    def start_flight(self, **kw):
        kw.setdefault("cfi_id", self.cfi)
        kw.setdefault("student_id", self.student)
        kw.setdefault("asset_id", self.sim_asset)
        kw.setdefault("flight_date", date.today().isoformat())
        kw.setdefault("solo", 0)
        kw.setdefault("started_at", db.now_iso())
        conn = db.get_db()
        fid = seed_row(conn, "flights", **kw)
        conn.commit()
        conn.close()
        return fid

    # ----- End Session (log_end) -------------------------------------------

    def test_end_session_requires_sim_time_not_hobbs_tach(self):
        fid = self.start_flight(stopped_at=db.now_iso())
        r = self.login("cfi").post(f"/flight/log/{fid}/end", data={"paid": "0"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT ended_at FROM flights WHERE id = ?", (fid,))
        self.assertIsNone(row["ended_at"])  # rejected - no Sim Time given

    def test_end_session_with_sim_time_logs_it_and_skips_asset_meters(self):
        fid = self.start_flight(stopped_at=db.now_iso())
        r = self.login("cfi").post(f"/flight/log/{fid}/end", data={"recorded_hours": "1.3", "paid": "0"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT ended_at, recorded_hours, hobbs_start, hobbs_end, tach_start, tach_end "
                       "FROM flights WHERE id = ?", (fid,))
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["recorded_hours"], 1.3)
        for col in ("hobbs_start", "hobbs_end", "tach_start", "tach_end"):
            self.assertIsNone(row[col])
        asset = self.q1("SELECT hobbs_hours, tach_hours FROM assets WHERE id = ?", (self.sim_asset,))
        self.assertIsNone(asset["hobbs_hours"])
        self.assertIsNone(asset["tach_hours"])

    def test_end_session_ignores_hobbs_tach_and_oil_sent_for_a_sim(self):
        # Even if something posts Hobbs/Tach/oil for a sim (e.g. a stale
        # form), none of it is saved - only recorded_hours is.
        fid = self.start_flight(stopped_at=db.now_iso())
        r = self.login("cfi").post(f"/flight/log/{fid}/end", data={
            "recorded_hours": "1.0", "hobbs_start": "10.0", "hobbs_end": "11.0",
            "tach_start": "10.0", "tach_end": "11.0", "oil_added_qt": "1.0", "paid": "0",
        })
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT hobbs_start, hobbs_end, tach_start, tach_end, oil_added_qt FROM flights WHERE id = ?", (fid,))
        for col in ("hobbs_start", "hobbs_end", "tach_start", "tach_end", "oil_added_qt"):
            self.assertIsNone(row[col])

    def test_end_session_ignores_solo_hours_sent_for_a_sim(self):
        fid = self.start_flight(stopped_at=db.now_iso())
        r = self.login("cfi").post(f"/flight/log/{fid}/end", data={
            "recorded_hours": "1.0", "solo_hours": "0.5", "paid": "0",
        })
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT solo_hours FROM flights WHERE id = ?", (fid,))
        self.assertIsNone(row["solo_hours"])

    def test_end_session_sim_bills_at_the_sim_rate(self):
        self.exec("UPDATE cfis SET rate_per_hour = 40 WHERE id = ?", (self.cfi,))
        fid = self.start_flight(stopped_at=db.now_iso())
        self.login("cfi").post(f"/flight/log/{fid}/end", data={"recorded_hours": "2.0", "paid": "0"})
        row = self.q1(flight._LOG_ROW_SQL + " WHERE f.id = ?", (fid,))
        cost = flight._row_with_cost(row)
        self.assertEqual(cost["plane_rate"], 75.0)
        self.assertEqual(cost["plane_cost"], 150.0)

    def test_end_session_sim_bills_at_the_students_sim_rate_override(self):
        self.exec("UPDATE students SET sim_rate_override = 60 WHERE id = ?", (self.student,))
        fid = self.start_flight(stopped_at=db.now_iso())
        self.login("cfi").post(f"/flight/log/{fid}/end", data={"recorded_hours": "1.0", "paid": "0"})
        row = self.q1(flight._LOG_ROW_SQL + " WHERE f.id = ?", (fid,))
        cost = flight._row_with_cost(row)
        self.assertEqual(cost["plane_rate"], 60.0)

    def test_end_session_note_on_a_sim_is_still_a_squawk(self):
        fid = self.start_flight(stopped_at=db.now_iso())
        r = self.login("cfi").post(f"/flight/log/{fid}/end", data={
            "recorded_hours": "1.0", "paid": "0", "notes": "Visual glitch on final approach",
        })
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT squawk FROM flights WHERE id = ?", (fid,))
        self.assertEqual(row["squawk"], 1)

    # ----- Active Flight board (log_active) ---------------------------------

    def test_sim_elapsed_hours_rounds_to_nearest_tenth(self):
        # 1:16 (76 min = 1.2667 hr) rounds to 1.3, not 1.2 (nearest, not
        # rounded up like billed hours - _round_up_hours would also give 1.3
        # here, so this pins the *rounding rule*, not just the result).
        row = dict(started_at="2026-01-01 10:00:00", stopped_at="2026-01-01 11:16:00", paused_seconds=0)
        self.assertEqual(flight._sim_elapsed_hours(row), 1.3)
        self.assertEqual(flight._sim_elapsed_label(row), "1:16")

    def test_sim_elapsed_hours_subtracts_paused_time(self):
        row = dict(started_at="2026-01-01 10:00:00", stopped_at="2026-01-01 11:16:00", paused_seconds=600)
        # 76 min - 10 min paused = 66 min = 1.1 hr
        self.assertEqual(flight._sim_elapsed_hours(row), 1.1)

    def test_sim_elapsed_hours_is_none_until_the_clock_stops(self):
        row = dict(started_at="2026-01-01 10:00:00", stopped_at=None, paused_seconds=0)
        self.assertIsNone(flight._sim_elapsed_hours(row))

    def test_active_board_shows_sim_badge_and_session_clock_label_while_running(self):
        self.start_flight()
        html = self.login("cfi").get("/flight/log/active").get_data(as_text=True)
        self.assertIn("Sim session", html)
        self.assertIn("Session clock", html)
        self.assertIn("End Session fills Sim Time in from this", html)
        self.assertNotIn("Add starting info (Hobbs/Tach, oil, note)", html)
        self.assertIn("Add a note", html)

    def test_active_board_end_session_box_is_prefilled_from_the_clock(self):
        started = db.now_iso()
        conn = db.get_db()
        started_dt = __import__("datetime").datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
        stopped = (started_dt + __import__("datetime").timedelta(hours=1, minutes=16)).strftime("%Y-%m-%d %H:%M:%S")
        conn.close()
        fid = self.start_flight(started_at=started, stopped_at=stopped)
        html = self.login("cfi").get("/flight/log/active").get_data(as_text=True)
        self.assertIn(f'id="recorded-hours-{fid}"', html)
        self.assertIn('value="1.3"', html)
        self.assertIn("Sim Time (hrs)", html)
        self.assertNotIn('name="hobbs_end"', html)
        self.assertNotIn("Solo Portion", html)
        self.assertNotIn("Oil Added", html)

    # ----- Log a Flight (log_new / _save_logged_flight) ---------------------

    def test_log_flight_page_shows_sim_time_not_hobbs_for_a_sim_booking(self):
        conn = db.get_db()
        day = date.today().isoformat()
        sched = seed_row(conn, "scheduled_flights", asset_id=self.sim_asset, student_id=self.student,
                         cfi_id=self.cfi, scheduled_date=day, scheduled_time="09:00",
                         duration_hours=1.0, status="scheduled")
        conn.commit()
        conn.close()
        html = self.login("cfi").get(
            f"/flight/log/new?asset_id={self.sim_asset}&student_id={self.student}&cfi_id={self.cfi}"
            f"&flight_date={day}&scheduled_flight_id={sched}").get_data(as_text=True)
        self.assertIn("sim_recorded_hours", html)
        self.assertIn('data-sim="1"', html)

    def test_log_flight_saves_sim_recorded_hours(self):
        conn = db.get_db()
        day = date.today().isoformat()
        sched = seed_row(conn, "scheduled_flights", asset_id=self.sim_asset, student_id=self.student,
                         cfi_id=self.cfi, scheduled_date=day, scheduled_time="09:00",
                         duration_hours=1.0, status="scheduled")
        conn.commit()
        conn.close()
        r = self.login("cfi").post("/flight/log/new", data={
            "asset_id": str(self.sim_asset), "student_id": str(self.student), "cfi_id": str(self.cfi),
            "flight_date": day, "scheduled_flight_id": str(sched), "sim_recorded_hours": "1.4",
            "oil_added_qt": "1.0", "solo_hours": "0.5", "paid": "",
        })
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT asset_id, recorded_hours, hobbs_start, oil_added_qt, solo_hours "
                       "FROM flights WHERE asset_id = ?", (self.sim_asset,))
        self.assertEqual(row["asset_id"], self.sim_asset)
        self.assertEqual(row["recorded_hours"], 1.4)
        self.assertIsNone(row["hobbs_start"])
        self.assertIsNone(row["oil_added_qt"])
        self.assertIsNone(row["solo_hours"])

    def test_log_flight_rejects_a_sim_with_no_sim_time(self):
        r = self.login("cfi").post("/flight/log/new", data={
            "asset_id": str(self.sim_asset), "student_id": str(self.student), "cfi_id": str(self.cfi),
            "flight_date": date.today().isoformat(), "paid": "",
        })
        self.assertEqual(r.status_code, 200)  # re-renders the form with an error, doesn't save
        self.assertIsNone(self.q1("SELECT id FROM flights WHERE asset_id = ?", (self.sim_asset,)))

    # ----- Editing a logged flight (log_edit) -------------------------------

    def test_editing_a_sim_flight_keeps_it_sim_time_based(self):
        fid = self.exec(
            "INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, solo, recorded_hours, ended_at, created_at) "
            "VALUES (?,?,?,?,0,?,?,?)",
            (self.cfi, self.student, self.sim_asset, date.today().isoformat(), 1.0, db.now_iso(), db.now_iso()))
        html = self.login("cfi").get(f"/flight/log/{fid}/edit").get_data(as_text=True)
        self.assertIn("sim_recorded_hours", html)
        self.assertNotIn('name="hobbs_start"', html)
        r = self.login("cfi").post(f"/flight/log/{fid}/edit", data={
            "student_id": str(self.student), "cfi_id": str(self.cfi), "asset_id": str(self.sim_asset),
            "flight_date": date.today().isoformat(), "sim_recorded_hours": "2.1", "paid": "",
        })
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT asset_id, recorded_hours FROM flights WHERE id = ?", (fid,))
        self.assertEqual(row["asset_id"], self.sim_asset)
        self.assertEqual(row["recorded_hours"], 2.1)

    # ----- Flight History -----------------------------------------------

    def test_flight_history_shows_sim_time_instead_of_dashes(self):
        self.exec(
            "INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, solo, recorded_hours, ended_at, created_at) "
            "VALUES (?,?,?,?,0,?,?,?)",
            (self.cfi, self.student, self.sim_asset, date.today().isoformat(), 1.6, db.now_iso(), db.now_iso()))
        html = self.login("cfi").get("/flight/log").get_data(as_text=True)
        self.assertIn("1.6", html)

    # ----- Real plane / own plane are unaffected ----------------------------

    def test_real_plane_end_session_still_requires_hobbs_tach(self):
        real_asset = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (real_asset,))
        fid = self.start_flight(asset_id=real_asset, stopped_at=db.now_iso())
        r = self.login("cfi").post(f"/flight/log/{fid}/end", data={"recorded_hours": "1.5", "paid": "0"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT ended_at FROM flights WHERE id = ?", (fid,))
        self.assertIsNone(row["ended_at"])  # recorded_hours doesn't apply to a real plane
