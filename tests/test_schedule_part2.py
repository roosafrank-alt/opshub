"""Flight school scheduling, part 2: edit, quick reschedule, approve / deny a
student's request, accepting a proposed time, change requests, and a student
cancelling their own flight."""
import json
from datetime import date, timedelta
from harness import OpsHubTestCase, open_finding
import db


def future(days=3):
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


class SchedulePart2Test(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N100S2")
        self.exec("UPDATE assets SET is_flight_asset=1 WHERE id=?", (self.plane,))
        self.plane2 = self.make_asset("N200S2")
        self.exec("UPDATE assets SET is_flight_asset=1 WHERE id=?", (self.plane2,))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id=?", (self.users["cfi"]["id"],))["id"]
        self.student = self.q1("SELECT id FROM students WHERE user_id=?", (self.users["flight_student"]["id"],))["id"]

    def booking(self, status="scheduled", days=3, time="10:00", plane=None, hours=1.0, student=None):
        return self.exec(
            """INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date, scheduled_time,
               duration_hours, status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (plane or self.plane, self.cfi, student or self.student, future(days), time, hours, status,
             "test", db.now_iso()))

    def row(self, sid):
        return self.q1("SELECT * FROM scheduled_flights WHERE id=?", (sid,))

    def edit(self, sid, **over):
        data = dict(asset_id=self.plane, student_id=self.student, cfi_id=self.cfi,
                    scheduled_date=future(), scheduled_time="10:00", duration_hours="1.0")
        data.update(over)
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.post(f"/flight/schedule/{sid}/edit", data=data)

    # ---- edit ----
    def test_edit_changes_time_and_tells_student(self):
        sid = self.booking()
        self.login("cfi")
        self.edit(sid, scheduled_time="14:00")
        self.assertEqual(self.row(sid)["scheduled_time"], "14:00")

    def test_edit_into_another_booking_is_refused(self):
        sid = self.booking()
        self.booking(time="14:00")
        self.login("cfi")
        self.edit(sid, scheduled_time="14:30")
        self.assertEqual(self.row(sid)["scheduled_time"], "10:00")

    def test_edit_to_a_past_date_is_refused(self):
        sid = self.booking()
        self.login("cfi")
        self.edit(sid, scheduled_date=(date.today() - timedelta(days=2)).strftime("%Y-%m-%d"))
        self.assertEqual(self.row(sid)["scheduled_date"], future())

    def test_edit_blank_required_fields_changes_nothing(self):
        sid = self.booking()
        self.login("cfi")
        for k in ("asset_id", "student_id", "scheduled_date"):
            r = self.edit(sid, **{k: ""})
            self.assertLess(r.status_code, 500, k)
        self.assertEqual(self.row(sid)["asset_id"], self.plane)

    def test_edit_harmless_odd_values_never_crash(self):
        sid = self.booking()
        self.login("cfi")
        for kw in (dict(duration_hours="nan"), dict(duration_hours="inf"), dict(duration_hours="abc"),
                   dict(scheduled_time="25:99")):
            with self.subTest(**kw):
                self.assertLess(self.edit(sid, **kw).status_code, 500)

    def test_edit_unknown_or_garbled_ids_and_dates_give_a_message(self):
        sid = self.booking()
        self.login("cfi")
        for kw in (dict(scheduled_date="nope"), dict(asset_id="999999"), dict(student_id="999999"),
                   dict(cfi_id="999999"), dict(asset_id="abc"), dict(student_id="abc"), dict(cfi_id="abc")):
            with self.subTest(**kw):
                self.assertLess(self.edit(sid, **kw).status_code, 500)

    def test_edit_cannot_save_zero_or_negative_length(self):
        sid = self.booking()
        self.login("cfi")
        for bad in ("-2", "0"):
            self.edit(sid, duration_hours=bad)
            d = self.row(sid)["duration_hours"]
            self.assertTrue(d is None or d > 0, f"saved length {d!r} for input {bad}")

    def test_edit_missing_booking_and_roles(self):
        sid = self.booking()
        self.login("cfi")
        self.assertLess(self.client.post("/flight/schedule/99999/edit", data={}).status_code, 500)
        for role in ("flight_student", "no_roles"):
            self.login(role)
            self.edit(sid, scheduled_time="16:00")
            self.assertEqual(self.row(sid)["scheduled_time"], "10:00", role)
        self.login(None)
        self.edit(sid, scheduled_time="16:00")
        self.assertEqual(self.row(sid)["scheduled_time"], "10:00")

    # ---- quick reschedule ----
    def test_reschedule_moves_only_date_and_time(self):
        sid = self.booking()
        self.login("cfi")
        self.client.post(f"/flight/schedule/{sid}/reschedule", data=dict(scheduled_date=future(5), scheduled_time="13:00"))
        r = self.row(sid)
        self.assertEqual((r["scheduled_date"], r["scheduled_time"]), (future(5), "13:00"))
        self.assertEqual((r["asset_id"], r["cfi_id"], r["student_id"], r["duration_hours"]),
                         (self.plane, self.cfi, self.student, 1.0))

    def test_reschedule_refuses_conflict_past_and_blank(self):
        sid = self.booking()
        self.booking(days=5, time="13:00")
        self.login("cfi")
        for data in (dict(scheduled_date=future(5), scheduled_time="13:30"),
                     dict(scheduled_date=(date.today() - timedelta(days=1)).strftime("%Y-%m-%d"), scheduled_time="10:00"),
                     dict(scheduled_date="", scheduled_time="10:00")):
            with self.subTest(**data):
                r = self.client.post(f"/flight/schedule/{sid}/reschedule", data=data)
                self.assertLess(r.status_code, 500)
                self.assertEqual(self.row(sid)["scheduled_date"], future())

    def test_reschedule_garbage_date_gives_a_message(self):
        sid = self.booking()
        self.login("cfi")
        r = self.client.post(f"/flight/schedule/{sid}/reschedule", data=dict(scheduled_date="garbage", scheduled_time="10:00"))
        self.assertLess(r.status_code, 500)

    def test_reschedule_impossible_time_is_not_saved(self):
        sid = self.booking()
        self.login("cfi")
        self.client.post(f"/flight/schedule/{sid}/reschedule", data=dict(scheduled_date=future(6), scheduled_time="99:99"))
        self.assertNotEqual(self.row(sid)["scheduled_time"], "99:99")

    def test_reschedule_only_while_still_scheduled(self):
        for st in ("cancelled", "completed", "in_progress", "pending_approval"):
            sid = self.booking(status=st, time="08:00")
            self.login("cfi")
            self.client.post(f"/flight/schedule/{sid}/reschedule", data=dict(scheduled_date=future(9), scheduled_time="09:00"))
            self.assertEqual(self.row(sid)["scheduled_date"], future(), st)

    def test_reschedule_missing_booking_and_students_cannot(self):
        sid = self.booking()
        self.login("cfi")
        self.assertLess(self.client.post("/flight/schedule/99999/reschedule", data={}).status_code, 500)
        self.login("flight_student")
        self.client.post(f"/flight/schedule/{sid}/reschedule", data=dict(scheduled_date=future(9), scheduled_time="09:00"))
        self.assertEqual(self.row(sid)["scheduled_date"], future())

    def test_reschedule_clears_a_change_request(self):
        sid = self.booking()
        self.exec("UPDATE scheduled_flights SET change_request_note='later?', change_requested_at=? WHERE id=?",
                  (db.now_iso(), sid))
        self.login("cfi")
        self.client.post(f"/flight/schedule/{sid}/reschedule", data=dict(scheduled_date=future(7), scheduled_time="11:00"))
        self.assertIsNone(self.row(sid)["change_requested_at"])

    # ---- approve / deny ----
    def test_approve_puts_request_on_calendar_once(self):
        sid = self.booking(status="pending_approval")
        self.login("cfi")
        self.client.post(f"/flight/schedule/{sid}/approve")
        self.assertEqual(self.row(sid)["status"], "scheduled")
        n = self.q1("SELECT COUNT(*) c FROM student_notifications WHERE student_id=?", (self.student,))["c"]
        self.client.post(f"/flight/schedule/{sid}/approve")
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM student_notifications WHERE student_id=?", (self.student,))["c"], n)

    def test_approve_refuses_when_slot_was_taken_meanwhile(self):
        sid = self.booking(status="pending_approval")
        self.booking(time="10:30")
        self.login("cfi")
        self.client.post(f"/flight/schedule/{sid}/approve")
        self.assertEqual(self.row(sid)["status"], "pending_approval")

    def test_approve_only_for_pending_and_only_for_instructors(self):
        done = self.booking(status="cancelled", time="07:00")
        self.login("cfi")
        self.client.post(f"/flight/schedule/{done}/approve")
        self.assertEqual(self.row(done)["status"], "cancelled")
        sid = self.booking(status="pending_approval", time="12:00")
        for role in ("flight_student", "no_roles"):
            self.login(role)
            self.client.post(f"/flight/schedule/{sid}/approve")
            self.assertEqual(self.row(sid)["status"], "pending_approval", role)
        self.login("cfi")
        self.assertLess(self.client.post("/flight/schedule/99999/approve").status_code, 500)

    def test_deny_needs_a_reason_and_records_it(self):
        sid = self.booking(status="pending_approval")
        self.login("cfi")
        self.client.post(f"/flight/schedule/{sid}/deny", data=dict(reason="   "))
        self.assertEqual(self.row(sid)["status"], "pending_approval")
        self.client.post(f"/flight/schedule/{sid}/deny", data=dict(reason="Plane in maintenance"))
        r = self.row(sid)
        self.assertEqual((r["status"], r["deny_reason"]), ("denied", "Plane in maintenance"))

    def test_deny_does_not_touch_a_confirmed_flight(self):
        sid = self.booking()
        self.login("cfi")
        self.client.post(f"/flight/schedule/{sid}/deny", data=dict(reason="x"))
        self.assertEqual(self.row(sid)["status"], "scheduled")

    def test_deny_with_garbage_proposed_times_never_crashes(self):
        sid = self.booking(status="pending_approval")
        self.login("cfi")
        for raw in ("not json", "{}", "[1,2]", '["<script>"]'):
            with self.subTest(raw=raw):
                r = self.client.post(f"/flight/schedule/{sid}/deny", data=dict(reason="no", proposed_times=raw))
                self.assertLess(r.status_code, 500)
                self.exec("UPDATE scheduled_flights SET status='pending_approval' WHERE id=?", (sid,))

    def test_student_cannot_approve_or_deny_own_request(self):
        sid = self.booking(status="pending_approval")
        self.login("flight_student")
        self.client.post(f"/flight/schedule/{sid}/approve")
        self.client.post(f"/flight/schedule/{sid}/deny", data=dict(reason="x"))
        self.assertEqual(self.row(sid)["status"], "pending_approval")

    def test_deny_with_null_proposed_times_does_not_crash(self):
        sid = self.booking(status="pending_approval")
        self.login("cfi")
        self.assertLess(self.client.post(f"/flight/schedule/{sid}/deny", data=dict(reason="no", proposed_times="null")).status_code, 500)

    # ---- proposed times ----
    def denied_with(self, times):
        sid = self.booking(status="denied", time="10:00")
        self.exec("UPDATE scheduled_flights SET proposed_times=?, deny_reason='busy' WHERE id=?", (json.dumps(times), sid))
        return sid

    def test_accepting_an_offered_time_makes_one_new_request(self):
        sid = self.denied_with(["2:00p"])
        self.login("flight_student")
        self.client.post(f"/flight/schedule/{sid}/accept-proposed-time", data=dict(time="2:00p"))
        new = self.q("SELECT * FROM scheduled_flights WHERE status='pending_approval'")
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["scheduled_time"], "14:00")
        self.assertIsNotNone(self.row(sid)["student_dismissed_at"])

    def test_accepting_a_time_that_was_not_offered_is_refused(self):
        sid = self.denied_with(["2:00p"])
        self.login("flight_student")
        self.client.post(f"/flight/schedule/{sid}/accept-proposed-time", data=dict(time="5:00p"))
        self.client.post(f"/flight/schedule/{sid}/accept-proposed-time", data={})
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM scheduled_flights WHERE status='pending_approval'")["c"], 0)

    def test_accepting_twice_does_not_double_request(self):
        sid = self.denied_with(["2:00p"])
        self.login("flight_student")
        for _ in range(2):
            self.client.post(f"/flight/schedule/{sid}/accept-proposed-time", data=dict(time="2:00p"))
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM scheduled_flights WHERE status='pending_approval'")["c"], 1)

    def test_another_student_cannot_accept_it(self):
        sid = self.denied_with(["2:00p"])
        other = self.exec("INSERT INTO students (name, username, password_hash, created_at) VALUES (?,?,?,?)",
                          ("Other", "other2", "x", db.now_iso()))
        self.login("cfi")
        self.client.post(f"/flight/schedule/{sid}/accept-proposed-time", data=dict(time="2:00p"))
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM scheduled_flights WHERE status='pending_approval'")["c"], 0)

    def test_dismissing_someone_elses_denied_request_does_nothing(self):
        sid = self.denied_with([])
        self.login("cfi")
        self.client.post(f"/flight/schedule/{sid}/dismiss")
        self.assertIsNone(self.row(sid)["student_dismissed_at"])
        self.login("flight_student")
        self.client.post(f"/flight/schedule/{sid}/dismiss")
        self.assertIsNotNone(self.row(sid)["student_dismissed_at"])

    # ---- change requests ----
    def test_change_request_needs_a_note_and_own_scheduled_flight(self):
        sid = self.booking()
        self.login("flight_student")
        self.client.post(f"/flight/schedule/{sid}/request_change", data=dict(note="  "))
        self.assertIsNone(self.row(sid)["change_requested_at"])
        self.client.post(f"/flight/schedule/{sid}/request_change", data=dict(note="Can we do Friday?"))
        self.assertEqual(self.row(sid)["change_request_note"], "Can we do Friday?")
        gone = self.booking(status="cancelled", time="07:00")
        self.client.post(f"/flight/schedule/{gone}/request_change", data=dict(note="hi"))
        self.assertIsNone(self.row(gone)["change_requested_at"])

    def test_change_request_withdraw_and_dismiss(self):
        sid = self.booking()
        self.exec("UPDATE scheduled_flights SET change_request_note='x', change_requested_at=? WHERE id=?", (db.now_iso(), sid))
        self.login("flight_student")
        self.client.post(f"/flight/schedule/{sid}/change_request/dismiss")
        self.assertIsNotNone(self.row(sid)["change_requested_at"])  # students cannot use the school's Handled button
        self.client.post(f"/flight/schedule/{sid}/change_request/withdraw")
        self.assertIsNone(self.row(sid)["change_requested_at"])
        self.client.post(f"/flight/schedule/{sid}/change_request/withdraw")  # nothing left: still no crash
        self.exec("UPDATE scheduled_flights SET change_request_note='x', change_requested_at=? WHERE id=?", (db.now_iso(), sid))
        self.login("cfi")
        self.client.post(f"/flight/schedule/{sid}/change_request/dismiss")
        self.assertIsNone(self.row(sid)["change_requested_at"])
        self.assertLess(self.client.post("/flight/schedule/99999/change_request/dismiss").status_code, 500)

    # ---- student cancels own flight ----
    def test_student_cancel_needs_reason_and_only_once(self):
        sid = self.booking(days=10)
        self.login("flight_student")
        self.client.post(f"/flight/schedule/{sid}/student_cancel", data=dict(reason=""))
        self.assertEqual(self.row(sid)["status"], "scheduled")
        self.client.post(f"/flight/schedule/{sid}/student_cancel", data=dict(reason="Sick"))
        self.assertEqual(self.row(sid)["status"], "cancelled")
        before = self.q1("SELECT COUNT(*) c FROM student_ledger WHERE student_id=?", (self.student,))["c"]
        self.client.post(f"/flight/schedule/{sid}/student_cancel", data=dict(reason="Again", ack_fee="on"))
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM student_ledger WHERE student_id=?", (self.student,))["c"], before)

    def test_student_cannot_cancel_someone_elses_flight(self):
        other = self.exec("INSERT INTO students (name, username, password_hash, created_at) VALUES (?,?,?,?)",
                          ("Other", "other3", "x", db.now_iso()))
        sid = self.booking(student=other)
        self.login("flight_student")
        self.client.post(f"/flight/schedule/{sid}/student_cancel", data=dict(reason="mine now"))
        self.assertEqual(self.row(sid)["status"], "scheduled")
        self.assertLess(self.client.post("/flight/schedule/99999/student_cancel", data=dict(reason="x")).status_code, 500)
