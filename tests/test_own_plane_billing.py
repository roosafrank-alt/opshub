"""Idea 'Student's own plane' revision 2: when a booking/flight uses a
student's own plane (the is_owner_placeholder asset behind the "Student's
own plane" toggle - see _get_or_create_own_plane_asset in flight.py):
  - no aircraft cost at all, only instructor time, billed at the CFI's
    Non-School Plane Rate (cfis.external_rate) instead of their usual rate
  - a generic, shared "Student's Own Plane" Schedule color (one setting,
    not per-student - see _own_plane_schedule_color/own_plane_color_edit)
  - Hobbs/Tach are replaced by one "Recorded Time" box when completing the
    flight (flights.recorded_hours), at End Session, Log Flight, and editing
    an already-logged flight alike.
"""
from datetime import date, timedelta

import db
import flight
from harness import OpsHubTestCase, seed_row


class OwnPlaneCostTest(OpsHubTestCase):
    """_row_with_cost / _flight_cost / _flight_hours - the billing math."""

    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        conn = db.get_db()
        self.own_asset = flight._get_or_create_own_plane_asset(conn, self.student, "N999OWN")
        conn.commit()
        conn.close()

    def make_flight(self, **kw):
        conn = db.get_db()
        kw.setdefault("student_id", self.student)
        kw.setdefault("cfi_id", self.cfi)
        kw.setdefault("asset_id", self.own_asset)
        kw.setdefault("flight_date", date.today().isoformat())
        kw.setdefault("solo", 0)
        fid = seed_row(conn, "flights", **kw)
        conn.commit()
        conn.close()
        row = self.q1(flight._LOG_ROW_SQL + " WHERE f.id = ?", (fid,))
        return flight._row_with_cost(row)

    def test_own_plane_has_no_aircraft_cost_even_with_a_plane_rate_override(self):
        self.exec("UPDATE students SET plane_rate_override = 120 WHERE id = ?", (self.student,))
        self.exec("UPDATE cfis SET rate_per_hour = 50 WHERE id = ?", (self.cfi,))
        cost = self.make_flight(recorded_hours=2.0)
        self.assertEqual(cost["plane_rate"], 0)
        self.assertEqual(cost["plane_cost"], 0)

    def test_own_plane_instructor_bills_at_cfi_external_rate(self):
        self.exec("UPDATE cfis SET rate_per_hour = 50, external_rate = 75 WHERE id = ?", (self.cfi,))
        cost = self.make_flight(recorded_hours=2.0)
        self.assertEqual(cost["instructor_rate"], 75)
        self.assertEqual(cost["instructor_cost"], 150)
        self.assertEqual(cost["total"], 150)

    def test_own_plane_falls_back_to_normal_rate_when_no_external_rate_set(self):
        self.exec("UPDATE cfis SET rate_per_hour = 50, external_rate = NULL WHERE id = ?", (self.cfi,))
        cost = self.make_flight(recorded_hours=2.0)
        self.assertEqual(cost["instructor_rate"], 50)

    def test_student_rate_override_still_wins_over_external_rate(self):
        self.exec("UPDATE cfis SET rate_per_hour = 50, external_rate = 75 WHERE id = ?", (self.cfi,))
        self.exec("UPDATE students SET rate_override = 40 WHERE id = ?", (self.student,))
        cost = self.make_flight(recorded_hours=2.0)
        self.assertEqual(cost["instructor_rate"], 40)

    def test_real_plane_flight_is_unaffected_by_external_rate(self):
        real_asset = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (real_asset,))
        self.exec("UPDATE students SET plane_rate_override = 120 WHERE id = ?", (self.student,))
        self.exec("UPDATE cfis SET rate_per_hour = 50, external_rate = 75 WHERE id = ?", (self.cfi,))
        cost = self.make_flight(asset_id=real_asset, hobbs_start=100.0, hobbs_end=102.0)
        self.assertEqual(cost["plane_rate"], 120)
        self.assertEqual(cost["instructor_rate"], 50)

    def test_flight_hours_prefers_recorded_hours_over_hobbs_tach(self):
        row = dict(recorded_hours=1.7, hobbs_start=100.0, hobbs_end=105.0, tach_start=None, tach_end=None)
        self.assertEqual(flight._flight_hours(row), 1.7)

    def test_flight_hours_falls_back_to_hobbs_when_no_recorded_hours(self):
        row = dict(recorded_hours=None, hobbs_start=100.0, hobbs_end=102.5, tach_start=None, tach_end=None)
        self.assertEqual(flight._flight_hours(row), 2.5)


class OwnPlaneColorTest(OpsHubTestCase):
    """The one shared Schedule color for every own-plane booking."""

    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]

    def test_defaults_to_none(self):
        conn = db.get_db()
        self.assertIsNone(flight._own_plane_schedule_color(conn))
        conn.close()

    def test_master_admin_can_set_the_color(self):
        color = flight.SCHEDULE_COLORS[0]
        r = self.login("master").post("/flight/planes/own-plane/color", data={"color": color})
        self.assertEqual(r.status_code, 302)
        conn = db.get_db()
        self.assertEqual(flight._own_plane_schedule_color(conn), color)
        conn.close()

    def test_color_already_used_by_a_plane_is_rejected(self):
        asset = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1, schedule_color = ? WHERE id = ?",
                  (flight.SCHEDULE_COLORS[0], asset))
        r = self.login("master").post("/flight/planes/own-plane/color", data={"color": flight.SCHEDULE_COLORS[0]})
        self.assertEqual(r.status_code, 302)
        conn = db.get_db()
        self.assertIsNone(flight._own_plane_schedule_color(conn))
        conn.close()

    def test_non_admin_cannot_reach_the_route(self):
        r = self.login("cfi").post("/flight/planes/own-plane/color", data={"color": flight.SCHEDULE_COLORS[0]})
        self.assertNotEqual(r.status_code, 200)

    def test_own_plane_booking_shows_the_generic_color_on_the_calendar(self):
        color = flight.SCHEDULE_COLORS[1]
        self.exec("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('own_plane_schedule_color', ?)", (color,))
        conn = db.get_db()
        own_asset = flight._get_or_create_own_plane_asset(conn, self.student)
        day = (date.today() + timedelta(days=2)).isoformat()
        seed_row(conn, "scheduled_flights", asset_id=own_asset, student_id=self.student, cfi_id=self.cfi,
                 scheduled_date=day, scheduled_time="09:00", duration_hours=1.5, status="scheduled")
        conn.commit()
        with self.app.test_request_context():
            rows = flight._schedule_rows(conn, day, day)
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_color"], color)

    def test_real_plane_booking_is_unaffected_by_the_own_plane_color(self):
        color = flight.SCHEDULE_COLORS[1]
        self.exec("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('own_plane_schedule_color', ?)", (color,))
        real_asset = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (real_asset,))
        conn = db.get_db()
        day = (date.today() + timedelta(days=2)).isoformat()
        seed_row(conn, "scheduled_flights", asset_id=real_asset, student_id=self.student, cfi_id=self.cfi,
                 scheduled_date=day, scheduled_time="09:00", duration_hours=1.5, status="scheduled")
        conn.commit()
        with self.app.test_request_context():
            rows = flight._schedule_rows(conn, day, day)
        conn.close()
        self.assertNotEqual(rows[0]["display_color"], color)

    def test_planes_page_shows_the_own_plane_card(self):
        html = self.login("master").get("/flight/planes").get_data(as_text=True)
        self.assertIn("Student's Own Plane", html)
        self.assertIn("own-plane/color", html)


class OwnPlaneCompletionTest(OpsHubTestCase):
    """Completing a flight (End Session / Log Flight / editing a logged
    flight) for a student's own plane: no Hobbs/Tach, one Recorded Time box
    instead, and the plane's real meters are never touched."""

    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        conn = db.get_db()
        self.own_asset = flight._get_or_create_own_plane_asset(conn, self.student, "N999OWN")
        conn.commit()
        conn.close()

    def start_flight(self):
        return self.exec(
            "INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, solo, started_at, stopped_at, created_at) "
            "VALUES (?,?,?,?,0,?,?,?)",
            (self.cfi, self.student, self.own_asset, date.today().isoformat(),
             db.now_iso(), db.now_iso(), db.now_iso()))

    def test_end_flight_requires_recorded_hours_not_hobbs(self):
        fid = self.start_flight()
        c = self.login("cfi")
        r = c.post(f"/flight/log/{fid}/end", data={"paid": "0"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT ended_at, recorded_hours FROM flights WHERE id = ?", (fid,))
        self.assertIsNone(row["ended_at"])  # rejected - no recorded_hours given

    def test_end_flight_with_recorded_hours_logs_it_and_skips_asset_meters(self):
        fid = self.start_flight()
        c = self.login("cfi")
        r = c.post(f"/flight/log/{fid}/end", data={"recorded_hours": "1.5", "paid": "0"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT ended_at, recorded_hours, hobbs_start, hobbs_end FROM flights WHERE id = ?", (fid,))
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["recorded_hours"], 1.5)
        self.assertIsNone(row["hobbs_start"])
        self.assertIsNone(row["hobbs_end"])
        asset = self.q1("SELECT hobbs_hours, tach_hours FROM assets WHERE id = ?", (self.own_asset,))
        self.assertIsNone(asset["hobbs_hours"])
        self.assertIsNone(asset["tach_hours"])

    def test_log_flight_page_shows_recorded_time_not_hobbs_for_an_own_plane_booking(self):
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
        self.assertIn("recorded_hours", html)
        self.assertIn("Student's own plane", html)
        self.assertNotIn('name="hobbs_start"', html)

    def test_log_flight_saves_recorded_hours_for_own_plane(self):
        conn = db.get_db()
        day = date.today().isoformat()
        sched = seed_row(conn, "scheduled_flights", asset_id=self.own_asset, student_id=self.student,
                         cfi_id=self.cfi, scheduled_date=day, scheduled_time="09:00",
                         duration_hours=1.5, status="scheduled")
        conn.commit()
        conn.close()
        r = self.login("cfi").post("/flight/log/new", data={
            "own_plane": "1", "student_id": str(self.student), "cfi_id": str(self.cfi),
            "flight_date": day, "scheduled_flight_id": str(sched), "recorded_hours": "1.2", "paid": "",
        })
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT asset_id, recorded_hours, hobbs_start FROM flights WHERE student_id = ?", (self.student,))
        self.assertEqual(row["asset_id"], self.own_asset)
        self.assertEqual(row["recorded_hours"], 1.2)
        self.assertIsNone(row["hobbs_start"])

    def test_editing_an_own_plane_flight_keeps_it_recorded_hours_based(self):
        fid = self.exec(
            "INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, solo, recorded_hours, ended_at, created_at) "
            "VALUES (?,?,?,?,0,?,?,?)",
            (self.cfi, self.student, self.own_asset, date.today().isoformat(), 1.0, db.now_iso(), db.now_iso()))
        html = self.login("cfi").get(f"/flight/log/{fid}/edit").get_data(as_text=True)
        self.assertIn("recorded_hours", html)
        self.assertNotIn('name="hobbs_start"', html)
        r = self.login("cfi").post(f"/flight/log/{fid}/edit", data={
            "student_id": str(self.student), "cfi_id": str(self.cfi), "own_plane": "1",
            "flight_date": date.today().isoformat(), "recorded_hours": "2.3", "paid": "",
        })
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT asset_id, recorded_hours FROM flights WHERE id = ?", (fid,))
        self.assertEqual(row["asset_id"], self.own_asset)
        self.assertEqual(row["recorded_hours"], 2.3)
