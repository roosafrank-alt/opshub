"""Flight school flight sessions: start from a booking (/flight/schedule/<id>/start),
stop, end (Hobbs/Tach), discard, double-presses, who may do what, bad input."""
from datetime import datetime, date, timedelta
from harness import OpsHubTestCase, open_finding
import db


class FlightSessionTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N300FS")
        self.exec("UPDATE assets SET is_flight_asset=1, hobbs_hours=100.0, tach_hours=90.0 WHERE id=?", (self.plane,))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id=?", (self.users["cfi"]["id"],))["id"]
        self.student = self.q1("SELECT id FROM students WHERE user_id=?", (self.users["flight_student"]["id"],))["id"]

    def booking(self, solo=False, minutes_from_now=0, status="scheduled"):
        t = datetime.now() + timedelta(minutes=minutes_from_now)
        return self.exec(
            "INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date, scheduled_time, "
            "duration_hours, status, solo) VALUES (?,?,?,?,?,?,?,?)",
            (self.plane, None if solo else self.cfi, self.student, t.strftime("%Y-%m-%d"),
             t.strftime("%H:%M"), 1.0, status, 1 if solo else 0))

    def start(self, sid=None, **kw):
        sid = sid or self.booking()
        r = self.client.post(f"/flight/schedule/{sid}/start", data=kw)
        return sid, r

    def flights(self):
        return self.q("SELECT * FROM flights")

    def started_flight(self):
        sid, _ = self.start()
        return self.flights()[0]["id"], sid

    def end(self, fid, **over):
        data = dict(hobbs_end="101.5", tach_end="91.3", paid="0")
        data.update(over)
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.post(f"/flight/log/{fid}/end", data=data)

    # ----- starting --------------------------------------------------------
    def test_start_creates_flight_from_plane_readings(self):
        self.login("cfi")
        sid, _ = self.start()
        fl = self.flights()
        self.assertEqual(len(fl), 1)
        self.assertEqual(fl[0]["hobbs_start"], 100.0)
        self.assertEqual(fl[0]["tach_start"], 90.0)
        self.assertIsNotNone(fl[0]["started_at"])
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id=?", (sid,))["status"], "in_progress")

    def test_double_start_makes_only_one_flight(self):
        self.login("cfi")
        sid, _ = self.start()
        self.client.post(f"/flight/schedule/{sid}/start")
        self.assertEqual(len(self.flights()), 1)

    def test_start_missing_or_cancelled_booking_is_not_an_error_page(self):
        self.login("cfi")
        self.assertLess(self.client.post("/flight/schedule/99999/start").status_code, 500)
        sid = self.booking(status="cancelled")
        self.assertLess(self.client.post(f"/flight/schedule/{sid}/start").status_code, 500)
        self.assertEqual(len(self.flights()), 0)

    def test_too_early_start_is_refused_without_move_to_now(self):
        self.login("cfi")
        sid = self.booking(minutes_from_now=240)
        self.client.post(f"/flight/schedule/{sid}/start")
        self.assertEqual(len(self.flights()), 0)

    def test_move_to_now_starts_early_booking(self):
        self.login("cfi")
        sid = self.booking(minutes_from_now=240)
        self.client.post(f"/flight/schedule/{sid}/start", data={"move_to_now": "1"})
        self.assertEqual(len(self.flights()), 1)

    def test_roles_that_may_not_start(self):
        sid = self.booking()
        for role in ("flight_student", "tech", "no_roles"):
            with self.subTest(role=role):
                self.login(role)
                self.client.post(f"/flight/schedule/{sid}/start")
                self.assertEqual(len(self.flights()), 0)

    def test_student_may_start_own_solo_flight_only(self):
        sid = self.booking(solo=True)
        self.login("flight_student")
        self.client.post(f"/flight/schedule/{sid}/start")
        self.assertEqual(len(self.flights()), 1)

    # ----- stopping and ending --------------------------------------------
    def test_end_logs_flight_and_advances_plane_meters(self):
        self.login("cfi")
        fid, sid = self.started_flight()
        self.client.post(f"/flight/log/{fid}/stop")
        self.end(fid)
        f = self.q1("SELECT * FROM flights WHERE id=?", (fid,))
        self.assertIsNotNone(f["ended_at"])
        self.assertEqual(f["hobbs_end"], 101.5)
        a = self.q1("SELECT hobbs_hours, tach_hours FROM assets WHERE id=?", (self.plane,))
        self.assertEqual((a["hobbs_hours"], a["tach_hours"]), (101.5, 91.3))
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id=?", (sid,))["status"], "completed")

    def test_end_needs_hobbs_tach_and_paid_choice(self):
        self.login("cfi")
        fid, _ = self.started_flight()
        for kw in (dict(hobbs_end=""), dict(tach_end=""), dict(paid=None), dict(paid="2"),
                   dict(hobbs_end="abc"), dict(hobbs_end="99"), dict(tach_end="80")):
            with self.subTest(**kw):
                r = self.end(fid, **kw)
                self.assertLess(r.status_code, 500)
                self.assertIsNone(self.q1("SELECT ended_at FROM flights WHERE id=?", (fid,))["ended_at"])

    def test_end_rejects_nan_and_infinite_readings(self):
        self.login("cfi")
        fid, _ = self.started_flight()
        for kw in (dict(hobbs_end="nan"), dict(tach_end="nan"), dict(hobbs_end="inf"), dict(tach_end="inf")):
            with self.subTest(**kw):
                self.end(fid, **kw)
                self.assertIsNone(self.q1("SELECT ended_at FROM flights WHERE id=?", (fid,))["ended_at"])
                a = self.q1("SELECT hobbs_hours, tach_hours FROM assets WHERE id=?", (self.plane,))
                self.assertEqual((a["hobbs_hours"], a["tach_hours"]), (100.0, 90.0))

    def test_ending_twice_does_not_double_charge(self):
        self.login("cfi")
        fid, _ = self.started_flight()
        self.end(fid)
        n = self.q1("SELECT COUNT(*) c FROM student_ledger")["c"] if self._has("student_ledger") else None
        self.end(fid, hobbs_end="105", tach_end="95")
        self.assertEqual(self.q1("SELECT hobbs_end FROM flights WHERE id=?", (fid,))["hobbs_end"], 101.5)
        if n is not None:
            self.assertEqual(self.q1("SELECT COUNT(*) c FROM student_ledger")["c"], n)

    def _has(self, table):
        return bool(self.q1("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)))

    def test_end_forbidden_for_other_roles(self):
        self.login("cfi")
        fid, _ = self.started_flight()
        for role in ("flight_student", "tech", "no_roles"):
            with self.subTest(role=role):
                self.login(role)
                self.end(fid)
                self.assertIsNone(self.q1("SELECT ended_at FROM flights WHERE id=?", (fid,))["ended_at"])

    def test_end_missing_flight_is_not_an_error_page(self):
        self.login("cfi")
        self.assertLess(self.end(99999).status_code, 500)
        self.assertLess(self.client.post("/flight/log/99999/stop").status_code, 500)
        self.assertLess(self.client.post("/flight/log/99999/discard").status_code, 500)

    def test_student_solo_end_always_logs_unpaid(self):
        sid = self.booking(solo=True)
        self.login("flight_student")
        self.client.post(f"/flight/schedule/{sid}/start")
        fid = self.flights()[0]["id"]
        self.end(fid, paid="1", payment_method="Cash", payment_amount="500")
        f = self.q1("SELECT * FROM flights WHERE id=?", (fid,))
        self.assertIsNotNone(f["ended_at"])
        self.assertEqual(f["paid"], 0)

    # ----- discard ---------------------------------------------------------
    def test_discard_removes_flight_and_restores_booking(self):
        self.login("cfi")
        fid, sid = self.started_flight()
        self.client.post(f"/flight/log/{fid}/discard")
        self.assertEqual(len(self.flights()), 0)
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id=?", (sid,))["status"], "scheduled")

    def test_discard_after_end_is_refused(self):
        self.login("cfi")
        fid, _ = self.started_flight()
        self.end(fid)
        self.client.post(f"/flight/log/{fid}/discard")
        self.assertEqual(len(self.flights()), 1)

    def test_discard_forbidden_for_other_roles(self):
        self.login("cfi")
        fid, _ = self.started_flight()
        for role in ("flight_student", "tech", "no_roles"):
            self.login(role)
            self.client.post(f"/flight/log/{fid}/discard")
        self.assertEqual(len(self.flights()), 1)

    # ----- stop / restart / pause -----------------------------------------
    def test_stop_twice_keeps_first_stop_time(self):
        self.login("cfi")
        fid, _ = self.started_flight()
        self.client.post(f"/flight/log/{fid}/stop")
        first = self.q1("SELECT stopped_at FROM flights WHERE id=?", (fid,))["stopped_at"]
        self.client.post(f"/flight/log/{fid}/stop")
        self.assertEqual(self.q1("SELECT stopped_at FROM flights WHERE id=?", (fid,))["stopped_at"], first)

    def test_restart_clock_clears_stop(self):
        self.login("cfi")
        fid, _ = self.started_flight()
        self.client.post(f"/flight/log/{fid}/stop")
        self.client.post(f"/flight/log/{fid}/restart_clock")
        self.assertIsNone(self.q1("SELECT stopped_at FROM flights WHERE id=?", (fid,))["stopped_at"])

    def test_pause_resume_pairs(self):
        self.login("cfi")
        fid, _ = self.started_flight()
        self.client.post(f"/flight/log/{fid}/pause")
        self.assertIsNotNone(self.q1("SELECT paused_at FROM flights WHERE id=?", (fid,))["paused_at"])
        self.client.post(f"/flight/log/{fid}/resume")
        self.assertIsNone(self.q1("SELECT paused_at FROM flights WHERE id=?", (fid,))["paused_at"])
        self.assertLess(self.client.post(f"/flight/log/{fid}/resume").status_code, 500)

    def test_active_board_and_history_render_for_every_role(self):
        self.login("cfi")
        fid, _ = self.started_flight()
        self.end(fid)
        for role in ("master", "cfi", "flight_student"):
            self.login(role)
            for url in ("/flight/log/active", "/flight/log", f"/flight/log/{fid}"):
                with self.subTest(role=role, url=url):
                    self.assertLess(self.client.get(url).status_code, 500)
        self.login("cfi")
        self.assertLess(self.client.get("/flight/log/99999").status_code, 500)
