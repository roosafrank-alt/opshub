"""Idea 'CFI break scheduling revision 2': a CFI's time off (cfi_time_off,
already used to block new bookings - see _cfi_time_off_conflict) now also
shows up on the schedule calendars themselves "as a booked spot", via
synthetic rows injected by _time_off_calendar_rows into the shared
_schedule_rows() fetch (see flight.py). Its note is visible to admins and
that same CFI, never to a student - the same _note_visibility rule used for
a real booking's note."""
import contextlib
from datetime import date, timedelta

from flask import session as flask_session

from harness import OpsHubTestCase, seed_row
import db
import flight


class CfiBreakCalendarTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.day = date.today() + timedelta(days=5)

    def make_time_off(self, **kw):
        conn = db.get_db()
        kw.setdefault("cfi_id", self.cfi)
        kw.setdefault("off_date", self.day.isoformat())
        kw.setdefault("note", "Dentist appointment")
        kw.setdefault("recurs_weekly", 0)
        rid = seed_row(conn, "cfi_time_off", **kw)
        conn.commit()
        conn.close()
        return rid

    @contextlib.contextmanager
    def request_ctx(self, role=None):
        """A request context for calling flight.py helpers directly (several
        read session, e.g. _note_visibility) - mirrors OpsHubTestCase.login()
        but for direct function calls rather than an HTTP round-trip."""
        with self.app.test_request_context():
            if role:
                u = self.users[role]
                flask_session["is_master_admin"] = bool(u["is_master_admin"])
                flask_session["shop_role"] = u["shop_role"]
                flask_session["flight_role"] = u["flight_role"]
                if u["flight_role"] == "cfi":
                    cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (u["id"],))
                    if cfi:
                        flask_session["cfi_id"] = cfi["id"]
                if u["flight_role"] == "student":
                    stu = self.q1("SELECT id FROM students WHERE user_id = ?", (u["id"],))
                    if stu:
                        flask_session["student_id"] = stu["id"]
            yield

    # ----- _time_off_calendar_rows itself ----------------------------------

    def test_one_off_break_appears_on_its_date_only(self):
        self.make_time_off(off_date=self.day.isoformat(), start_time="09:00", end_time="11:00")
        with self.request_ctx():
            conn = db.get_db()
            rows = flight._time_off_calendar_rows(conn, self.day.isoformat(), self.day.isoformat())
            conn.close()
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertTrue(r["is_time_off"])
            self.assertEqual(r["scheduled_time"], "09:00")
            self.assertEqual(r["duration_hours"], 2.0)
            self.assertEqual(r["plane_tag"], "Break")
            self.assertLess(r["id"], 0)

            conn = db.get_db()
            before = flight._time_off_calendar_rows(conn, (self.day - timedelta(days=1)).isoformat(),
                                                     (self.day - timedelta(days=1)).isoformat())
            conn.close()
            self.assertEqual(before, [])

    def test_whole_day_off_has_no_start_time(self):
        self.make_time_off(start_time=None, end_time=None)
        with self.request_ctx():
            conn = db.get_db()
            rows = flight._time_off_calendar_rows(conn, self.day.isoformat(), self.day.isoformat())
            conn.close()
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["scheduled_time"])
            self.assertIsNone(rows[0]["duration_hours"])

    def test_recurring_weekly_break_expands_across_range(self):
        anchor = date(2026, 3, 2)  # a Monday
        self.make_time_off(off_date=anchor.isoformat(), recurs_weekly=1, start_time="07:00", end_time="08:00")
        with self.request_ctx():
            conn = db.get_db()
            rows = flight._time_off_calendar_rows(conn, anchor.isoformat(), (anchor + timedelta(days=21)).isoformat())
            conn.close()
            mondays = {anchor + timedelta(days=7 * i) for i in range(4)}
            self.assertEqual({r["scheduled_date"] for r in rows}, {d.isoformat() for d in mondays})
            # Doesn't apply before the anchor date.
            conn = db.get_db()
            earlier = flight._time_off_calendar_rows(conn, (anchor - timedelta(days=14)).isoformat(),
                                                      (anchor - timedelta(days=7)).isoformat())
            conn.close()
            self.assertEqual(earlier, [])

    def test_no_pilot_badge_leaks_onto_a_break_row(self):
        self.make_time_off()
        with self.request_ctx():
            conn = db.get_db()
            rows = flight._time_off_calendar_rows(conn, self.day.isoformat(), self.day.isoformat())
            conn.close()
            self.assertIsNone(flight.pilot_badge_info(rows[0]))

    # ----- fed into the real calendar builders ------------------------------

    def test_break_appears_in_day_view(self):
        self.make_time_off(start_time="09:00", end_time="10:30")
        with self.request_ctx():
            conn = db.get_db()
            rows = flight._build_schedule_day(conn, self.day.isoformat())
            conn.close()
            self.assertTrue(any(r["is_time_off"] for r in rows))

    def test_break_appears_in_month_view(self):
        self.make_time_off()
        with self.request_ctx():
            conn = db.get_db()
            month = flight._build_schedule_month(conn, self.day.year, self.day.month)
            conn.close()
            flights = month["by_day"][self.day.day]
            self.assertTrue(any(r["is_time_off"] for r in flights))

    def test_break_appears_in_year_list_view(self):
        self.make_time_off()
        with self.request_ctx():
            conn = db.get_db()
            months, _anchor_id = flight._build_schedule_year_list(conn, self.day.year)
            conn.close()
            flights = months[self.day.month - 1]["flights"]
            self.assertTrue(any(r["is_time_off"] and r["scheduled_date"] == self.day.isoformat() for r in flights))

    def test_break_does_not_leak_into_plane_availability(self):
        self.make_time_off()
        with self.request_ctx():
            conn = db.get_db()
            avail = flight._build_availability_day(conn, self.day.isoformat())
            conn.close()
            # Availability is plane-keyed only; nothing there should carry
            # is_time_off, and the helper should never even be consulted for it.
            dumped = str(avail)
            self.assertNotIn("is_time_off", dumped)

    def test_filtering_by_plane_excludes_breaks(self):
        self.make_time_off()
        with self.request_ctx():
            conn = db.get_db()
            rows = flight._schedule_rows(conn, self.day.isoformat(), self.day.isoformat(),
                                          plane_id=self.plane, include_time_off=True)
            conn.close()
            self.assertFalse(any(r.get("is_time_off") for r in rows))

    # ----- note visibility (admin/that CFI only, never a student) ----------

    def test_note_visible_to_admin(self):
        self.make_time_off(note="Doctor's appointment")
        with self.request_ctx("master"):
            conn = db.get_db()
            rows = flight._time_off_calendar_rows(conn, self.day.isoformat(), self.day.isoformat())
            conn.close()
            self.assertTrue(rows[0]["notes_visible"])

    def test_note_visible_to_that_cfi(self):
        self.make_time_off(note="Doctor's appointment")
        with self.request_ctx("cfi"):
            conn = db.get_db()
            rows = flight._time_off_calendar_rows(conn, self.day.isoformat(), self.day.isoformat())
            conn.close()
            self.assertTrue(rows[0]["notes_visible"])

    def test_note_not_visible_to_a_student(self):
        self.make_time_off(note="Doctor's appointment")
        with self.request_ctx("flight_student"):
            conn = db.get_db()
            rows = flight._time_off_calendar_rows(conn, self.day.isoformat(), self.day.isoformat())
            conn.close()
            self.assertFalse(rows[0]["notes_visible"])

    # ----- rendered pages don't crash on a break row ------------------------

    def test_schedule_day_page_renders_with_a_break(self):
        self.make_time_off(start_time="09:00", end_time="10:30")
        r = self.login("cfi").get(f"/flight/schedule?view=day&date={self.day.isoformat()}")
        self.assertEqual(200, r.status_code)
        html = r.get_data(as_text=True)
        self.assertIn("Break", html)
        self.assertIn("schedule-time-off-block", html)

    def test_schedule_month_page_renders_with_a_break(self):
        self.make_time_off()
        r = self.login("cfi").get(f"/flight/schedule?view=month&year={self.day.year}&month={self.day.month}")
        self.assertEqual(200, r.status_code)
        self.assertIn("schedule-time-off-block", r.get_data(as_text=True))

    def test_schedule_week_page_renders_with_a_break(self):
        self.make_time_off(start_time="09:00", end_time="10:30")
        r = self.login("cfi").get(f"/flight/schedule?view=week&date={self.day.isoformat()}")
        self.assertEqual(200, r.status_code)

    def test_schedule_quarter_page_renders_with_a_break(self):
        self.make_time_off()
        r = self.login("cfi").get(f"/flight/schedule?view=quarter&year={self.day.year}&month={self.day.month}")
        self.assertEqual(200, r.status_code)

    def test_schedule_year_page_renders_with_a_break(self):
        self.make_time_off()
        r = self.login("cfi").get(f"/flight/schedule?view=year&year={self.day.year}")
        self.assertEqual(200, r.status_code)

    def test_availability_page_unaffected_by_a_break(self):
        self.make_time_off()
        r = self.login("cfi").get(f"/flight/schedule/availability?date={self.day.isoformat()}")
        self.assertEqual(200, r.status_code)
