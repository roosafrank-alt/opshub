"""Flight school scheduling: booking a flight (/schedule/new), conflicts,
bad input, student self-service requests, and cancelling."""
from datetime import date, timedelta
from harness import OpsHubTestCase, open_finding
import db


def future(days=3):
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


class ScheduleBookingTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N100SB")
        self.exec("UPDATE assets SET is_flight_asset=1 WHERE id=?", (self.plane,))
        self.plane2 = self.make_asset("N200SB")
        self.exec("UPDATE assets SET is_flight_asset=1 WHERE id=?", (self.plane2,))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id=?", (self.users["cfi"]["id"],))["id"]
        self.student = self.q1("SELECT id FROM students WHERE user_id=?", (self.users["flight_student"]["id"],))["id"]

    def n(self):
        return self.q1("SELECT COUNT(*) c FROM scheduled_flights")["c"]

    def book(self, **over):
        data = dict(asset_id=self.plane, student_id=self.student, cfi_id=self.cfi,
                    scheduled_date=future(), scheduled_time="10:00", duration_hours="1.5")
        data.update(over)
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.post("/flight/schedule/new", data=data)

    def test_cfi_booking_goes_straight_onto_calendar(self):
        self.login("cfi")
        self.book()
        row = self.q1("SELECT * FROM scheduled_flights")
        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(row["asset_id"], self.plane)

    def test_same_plane_same_time_is_refused(self):
        self.login("cfi")
        self.book()
        self.book(student_id=self.student, cfi_id=None, scheduled_time="10:30")
        self.assertEqual(self.n(), 1)

    def test_same_instructor_overlapping_on_other_plane_is_refused(self):
        self.login("cfi")
        self.book()
        self.book(asset_id=self.plane2, scheduled_time="10:30")
        self.assertEqual(self.n(), 1)

    def test_back_to_back_is_allowed(self):
        self.login("cfi")
        self.book()
        self.book(scheduled_time="11:30")
        self.assertEqual(self.n(), 2)

    def test_different_day_is_allowed(self):
        self.login("cfi")
        self.book()
        self.book(scheduled_date=future(4))
        self.assertEqual(self.n(), 2)

    def test_missing_plane_student_or_date_books_nothing(self):
        self.login("cfi")
        for missing in ("asset_id", "student_id", "scheduled_date"):
            r = self.book(**{missing: ""})
            self.assertNotEqual(r.status_code, 500, missing)
        self.assertEqual(self.n(), 0)

    def test_past_date_is_refused(self):
        self.login("cfi")
        self.book(scheduled_date=(date.today() - timedelta(days=5)).strftime("%Y-%m-%d"))
        self.assertEqual(self.n(), 0)

    def test_odd_but_harmless_input_never_crashes(self):
        self.login("cfi")
        for kw in (dict(scheduled_time="25:99"), dict(duration_hours="-3"),
                   dict(repeat="weekly", repeat_until="garbage")):
            with self.subTest(**kw):
                self.assertLess(self.book(**kw).status_code, 500)

    @open_finding("qa-schedule-bad-input-crash")
    def test_bad_values_give_a_message_not_an_error_page(self):
        self.login("cfi")
        for kw in (dict(scheduled_date="not-a-date"), dict(duration_hours="nan"), dict(duration_hours="inf"),
                   dict(asset_id="999999"), dict(student_id="999999"), dict(cfi_id="999999"),
                   dict(asset_id="abc")):
            with self.subTest(**kw):
                self.assertLess(self.book(**kw).status_code, 500)

    @open_finding("qa-schedule-deleted-plane")
    def test_deleted_plane_cannot_be_booked(self):
        self.exec("UPDATE assets SET deleted_at=? WHERE id=?", (db.now_iso(), self.plane))
        self.login("cfi")
        self.assertLess(self.book().status_code, 500)
        self.assertEqual(self.n(), 0)

    def test_student_request_is_pending_not_on_calendar(self):
        self.login("flight_student")
        self.book(student_id=None, cfi_id=self.cfi)
        row = self.q1("SELECT * FROM scheduled_flights")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending_approval")
        self.assertEqual(row["student_id"], self.student)

    def test_student_cannot_book_as_someone_else(self):
        other = self.exec("INSERT INTO students (name, username, password_hash, created_at) VALUES (?,?,?,?)",
                          ("Other", "other", "x", db.now_iso()))
        self.login("flight_student")
        self.book(student_id=other)
        for r in self.q("SELECT student_id FROM scheduled_flights"):
            self.assertEqual(r["student_id"], self.student)

    def test_logged_out_cannot_book(self):
        self.login(None)
        self.book()
        self.assertEqual(self.n(), 0)

    def test_cancel_frees_the_slot(self):
        self.login("cfi")
        self.book()
        sid = self.q1("SELECT id FROM scheduled_flights")["id"]
        self.client.post(f"/flight/schedule/{sid}/cancel", data=dict(reason="Weather"))
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id=?", (sid,))["status"], "cancelled")
        self.book()
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM scheduled_flights WHERE status='scheduled'")["c"], 1)

    def test_cancel_missing_booking_is_not_a_crash(self):
        self.login("cfi")
        self.assertLess(self.client.post("/flight/schedule/99999/cancel").status_code, 500)
        self.assertLess(self.client.post("/flight/schedule/99999/edit", data={}).status_code, 500)

    def test_cancel_twice_is_harmless(self):
        self.login("cfi")
        self.book()
        sid = self.q1("SELECT id FROM scheduled_flights")["id"]
        self.client.post(f"/flight/schedule/{sid}/cancel", data=dict(reason="Weather"))
        self.assertLess(self.client.post(f"/flight/schedule/{sid}/cancel", data=dict(reason="Weather")).status_code, 500)

    def test_student_cannot_cancel_or_edit_another_students_booking(self):
        self.login("cfi")
        other = self.exec("INSERT INTO students (name, username, password_hash, created_at) VALUES (?,?,?,?)",
                          ("Other", "other", "x", db.now_iso()))
        self.book(student_id=other)
        sid = self.q1("SELECT id FROM scheduled_flights")["id"]
        self.login("flight_student")
        self.client.post(f"/flight/schedule/{sid}/student_cancel")
        self.client.post(f"/flight/schedule/{sid}/cancel", data=dict(reason="Weather"))
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id=?", (sid,))["status"], "scheduled")

    def test_weekly_repeat_books_each_week(self):
        self.login("cfi")
        self.book(repeat="weekly", repeat_until=future(24))
        self.assertGreaterEqual(self.n(), 3)
        dates = [r["scheduled_date"] for r in self.q("SELECT scheduled_date FROM scheduled_flights")]
        self.assertEqual(len(dates), len(set(dates)))

    def test_time_off_blocks_the_instructor(self):
        self.exec("INSERT INTO cfi_time_off (cfi_id, off_date) VALUES (?,?)", (self.cfi, future()))
        self.login("cfi")
        self.book()
        self.assertEqual(self.n(), 0)
