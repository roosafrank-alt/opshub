"""Preview build, Fly with Kate! scheduling and flying fixes (FLY-xx ids in
preview/spec.json): one small test per bug fix or behaviour change."""
import json
from datetime import date, datetime, timedelta

from harness import OpsHubTestCase, seed_row
import db


class FlyBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.cfi2 = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi_billing"]["id"],))["id"]
        self.day = date.today() + timedelta(days=3)

    def book(self, status="scheduled", day=None, time="09:00", cfi="default", **extra):
        conn = db.get_db()
        vals = dict(asset_id=self.plane, student_id=self.student,
                    cfi_id=self.cfi if cfi == "default" else cfi,
                    scheduled_date=(day or self.day).isoformat(), scheduled_time=time,
                    duration_hours=1.5, status=status)
        vals.update(extra)
        sid = seed_row(conn, "scheduled_flights", **vals)
        conn.commit()
        conn.close()
        return sid

    def sched(self, sid):
        return self.q1("SELECT * FROM scheduled_flights WHERE id = ?", (sid,))

    def notices(self):
        return [r["message"] for r in self.q("SELECT message FROM student_notifications ORDER BY id")]

    def running_flight(self, **extra):
        vals = dict(asset_id=self.plane, cfi_id=self.cfi, student_id=self.student, started_at=db.now_iso(),
                    paused_seconds=0, flight_date=db.now_iso()[:10], created_at=db.now_iso())
        vals.update(extra)
        conn = db.get_db()
        fid = seed_row(conn, "flights", **vals)
        conn.commit()
        conn.close()
        return fid


class SignupGoneTest(FlyBase):
    def test_signup_page_is_gone(self):  # FLY-01
        self.assertEqual(self.client.get("/flight/signup").status_code, 404)
        r = self.client.post("/flight/signup", data={"name": "X", "username": "x", "password": "y"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.q("SELECT id FROM users WHERE username = 'x'"), [])


class WeatherTest(FlyBase):
    def test_starts_unticked_with_select_all_and_confirm(self):  # FLY-02
        self.book(day=self.day)
        html = self.login("cfi").get(f"/flight/schedule/weather?date={self.day}").get_data(as_text=True)
        self.assertIn("Select all", html)
        self.assertIn("data-confirm=", html)
        box = html[html.index('name="ids"'):].split(">")[0]
        self.assertNotIn("checked", box)

    def test_message_counts_what_was_offered(self):  # FLY-28
        sid = self.book(day=self.day)
        c = self.login("cfi")
        c.post("/flight/schedule/weather", data=dict(date=self.day.isoformat(), do="cancel", ids=[str(sid)]))
        with c.session_transaction() as s:
            msgs = [m for _cat, m in s.get("_flashes", [])]
        self.assertTrue(any("1 student texted (1 with new times to pick, 0 with none open" in m for m in msgs), msgs)


class CancelTest(FlyBase):
    def test_cancel_needs_a_reason_and_records_who(self):  # FLY-03, FLY-04
        sid = self.book()
        c = self.login("cfi")
        c.post(f"/flight/schedule/{sid}/cancel")
        self.assertEqual(self.sched(sid)["status"], "scheduled")  # no reason, nothing happens
        c.post(f"/flight/schedule/{sid}/cancel", data={"reason": "Plane down"})
        row = self.sched(sid)
        self.assertEqual((row["status"], row["cancel_reason"], row["cancelled_by"]), ("cancelled", "Plane down", "Cfi"))
        self.assertTrue(row["cancelled_at"])
        self.assertTrue(any("was cancelled by Cfi: Plane down" in n for n in self.notices()), self.notices())

    def test_cannot_cancel_a_flight_that_already_started(self):  # FLY-03
        sid = self.book(status="in_progress")
        self.login("cfi").post(f"/flight/schedule/{sid}/cancel", data={"reason": "oops"})
        self.assertEqual(self.sched(sid)["status"], "in_progress")

    def test_balance_hold_can_be_cancelled(self):  # FLY-03
        sid = self.book(status="balance_hold")
        self.login("cfi").post(f"/flight/schedule/{sid}/cancel", data={"reason": "student asked"})
        self.assertEqual(self.sched(sid)["status"], "cancelled")

    def test_dashboard_shows_who_cancelled(self):  # FLY-03
        sid = self.book()
        self.login("master").post(f"/flight/schedule/{sid}/cancel", data={"reason": "Weather ahead"})
        html = self.login("master").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("cancelled by Master", html)


class RescheduleEditTest(FlyBase):
    def test_reschedule_tells_student_and_stays_in_view(self):  # FLY-04, FLY-33
        sid = self.book(time="09:00")
        new_day = (self.day + timedelta(days=1)).isoformat()
        c = self.login("cfi")
        r = c.post(f"/flight/schedule/{sid}/reschedule", data={"scheduled_date": new_day, "scheduled_time": "11:00"},
                   headers={"Referer": f"http://localhost/flight/schedule?view=week&date={self.day}"})
        loc = r.headers["Location"]
        self.assertIn("view=week", loc)
        self.assertIn(f"date={new_day}", loc)
        self.assertIn(f"new={sid}", loc)
        self.assertTrue(any("was moved to" in n and "by Cfi" in n for n in self.notices()), self.notices())

    def test_edit_that_changes_plane_tells_student(self):  # FLY-04
        sid = self.book()
        other = self.make_asset("N456")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (other,))
        c = self.login("cfi")
        r = c.post(f"/flight/schedule/{sid}/edit", data=dict(
            asset_id=str(other), student_id=str(self.student), cfi_id=str(self.cfi),
            scheduled_date=self.day.isoformat(), scheduled_time="09:00", duration_hours="1.5"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"new={sid}", r.headers["Location"])
        self.assertTrue(any("plane N456" in n for n in self.notices()), self.notices())

    def test_edit_that_changes_nothing_stays_quiet(self):  # FLY-04
        sid = self.book()
        self.login("cfi").post(f"/flight/schedule/{sid}/edit", data=dict(
            asset_id=str(self.plane), student_id=str(self.student), cfi_id=str(self.cfi),
            scheduled_date=self.day.isoformat(), scheduled_time="09:00", duration_hours="1.5", notes="bring headset"))
        self.assertEqual(self.notices(), [])


class StartEndTest(FlyBase):
    def test_whoever_started_it_can_pause_and_end(self):  # FLY-05
        fid = self.running_flight(started_by_cfi_id=self.cfi2)
        c = self.login("cfi_billing")
        c.post(f"/flight/log/{fid}/pause")
        self.assertIsNotNone(self.q1("SELECT paused_at FROM flights WHERE id = ?", (fid,))["paused_at"])
        c.post(f"/flight/log/{fid}/stop")
        self.assertIsNotNone(self.q1("SELECT stopped_at FROM flights WHERE id = ?", (fid,))["stopped_at"])

    def test_other_instructor_still_cannot(self):  # FLY-05
        fid = self.running_flight()
        self.login("cfi_billing").post(f"/flight/log/{fid}/pause")
        self.assertIsNone(self.q1("SELECT paused_at FROM flights WHERE id = ?", (fid,))["paused_at"])

    def test_admin_can_always_override(self):  # FLY-05
        fid = self.running_flight(started_by_cfi_id=self.cfi2)
        self.login("master").post(f"/flight/log/{fid}/pause")
        self.assertIsNotNone(self.q1("SELECT paused_at FROM flights WHERE id = ?", (fid,))["paused_at"])

    def test_start_records_who_pressed_it(self):  # FLY-05
        sid = self.book(time=datetime.now().strftime("%H:%M"), day=date.today())
        self.login("cfi_billing").post(f"/flight/schedule/{sid}/start")
        f = self.q1("SELECT * FROM flights WHERE scheduled_flight_id = ?", (sid,))
        self.assertEqual(f["started_by_cfi_id"], self.cfi2)

    def test_student_ending_own_solo_flight_is_always_unpaid(self):  # FLY-07
        self.exec("UPDATE assets SET hobbs_hours = 100, tach_hours = 100 WHERE id = ?", (self.plane,))
        fid = self.running_flight(cfi_id=None, solo=1, hobbs_start=100.0, tach_start=100.0,
                                  stopped_at=db.now_iso())
        c = self.login("flight_student")
        html = c.get("/flight/log/active").get_data(as_text=True)
        self.assertNotIn('name="payment_amount"', html)
        self.assertNotIn('name="paid" id="paid-1', html)
        self.assertIn("Email me a receipt", html)
        c.post(f"/flight/log/{fid}/end", data=dict(hobbs_end="101.0", tach_end="101.0", paid="1",
                                                   payment_amount="999", payment_method="Cash"))
        f = self.q1("SELECT * FROM flights WHERE id = ?", (fid,))
        self.assertTrue(f["ended_at"])
        self.assertEqual(f["paid"], 0)
        self.assertEqual(self.q("SELECT id FROM student_ledger WHERE entry_type = 'payment'"), [])

    def test_student_sees_no_live_map(self):  # FLY-08
        self.running_flight(cfi_id=None, solo=1)
        html = self.login("flight_student").get("/flight/log/active").get_data(as_text=True)
        self.assertNotIn("Live Aircraft Map", html)
        html = self.login("cfi").get("/flight/log/active").get_data(as_text=True)
        self.assertIn("Live Aircraft Map", html)

    def test_discard_puts_the_booking_back_and_charges_nothing(self):  # FLY-09
        sid = self.book(status="in_progress")
        fid = self.running_flight(scheduled_flight_id=sid)
        self.login("cfi").post(f"/flight/log/{fid}/discard")
        self.assertIsNone(self.q1("SELECT id FROM flights WHERE id = ?", (fid,)))
        self.assertEqual(self.sched(sid)["status"], "scheduled")
        self.assertEqual(self.q("SELECT id FROM student_ledger"), [])

    def test_discard_refused_for_someone_who_cannot_end_it(self):  # FLY-09
        fid = self.running_flight()
        self.login("cfi_billing").post(f"/flight/log/{fid}/discard")
        self.assertIsNotNone(self.q1("SELECT id FROM flights WHERE id = ?", (fid,)))


class PaymentTest(FlyBase):
    def logged_flight(self):
        return self.running_flight(cfi_id=self.cfi, hobbs_start=100.0, hobbs_end=101.0, tach_start=100.0, tach_end=101.0,
                                   ended_at=db.now_iso(), instructor_clock_hours=1.0, paid=0)

    def test_mark_paid_adds_payment_and_unpaid_removes_it(self):  # FLY-11
        self.exec("UPDATE students SET plane_rate_override = 100 WHERE id = ?", (self.student,))
        fid = self.logged_flight()
        c = self.login("cfi_billing")
        c.post(f"/flight/log/{fid}/toggle_paid", data={"payment_amount": "50.00", "payment_method": "Cash"})
        f = self.q1("SELECT * FROM flights WHERE id = ?", (fid,))
        self.assertEqual((f["paid"], f["payment_method"], f["payment_amount"]), (1, "Cash", 50.0))
        led = self.q("SELECT * FROM student_ledger WHERE entry_type = 'payment' AND flight_id = ?", (fid,))
        self.assertEqual([r["amount"] for r in led], [50.0])
        self.assertEqual(self.q1("SELECT balance FROM students WHERE id = ?", (self.student,))["balance"], -50.0)   # $100 flight charge - $50 paid
        c.post(f"/flight/log/{fid}/toggle_paid")
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id = ?", (fid,))["paid"], 0)
        net = self.q1("SELECT COALESCE(SUM(amount), 0) AS n FROM student_ledger WHERE entry_type IN ('payment', 'payment_reversal') AND flight_id = ?", (fid,))["n"]
        self.assertEqual(net, 0)   # the reversal line cancels the payment but history is kept
        self.assertEqual(self.q1("SELECT balance FROM students WHERE id = ?", (self.student,))["balance"], -100.0)   # only the payment is taken back; the charge stays

    def test_mark_paid_defaults_to_the_flight_total(self):  # FLY-11
        self.exec("UPDATE students SET plane_rate_override = 100 WHERE id = ?", (self.student,))
        fid = self.logged_flight()
        html = self.login("cfi_billing").get("/flight/log").get_data(as_text=True)
        self.assertIn("mark-paid-btn", html)
        self.assertIn('data-total="', html)
        self.login("cfi_billing").post(f"/flight/log/{fid}/toggle_paid")
        self.assertGreater(self.q1("SELECT payment_amount FROM flights WHERE id = ?", (fid,))["payment_amount"], 0)

    def test_edit_flight_has_no_payment_boxes_and_keeps_paid(self):  # FLY-10
        fid = self.logged_flight()
        self.exec("UPDATE flights SET paid = 1 WHERE id = ?", (fid,))
        c = self.login("cfi")
        html = c.get(f"/flight/log/{fid}/edit").get_data(as_text=True)
        self.assertNotIn("Amount Collected", html)
        self.assertIn("To record a payment use Billing", html)
        c.post(f"/flight/log/{fid}/edit", data=dict(asset_id=str(self.plane), student_id=str(self.student),
                                                    cfi_id=str(self.cfi), flight_date=db.now_iso()[:10],
                                                    hobbs_end="101.0", tach_end="101.0"))
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id = ?", (fid,))["paid"], 1)


class ApproveDenyTest(FlyBase):
    def test_approve_assign_later_leaves_instructor_blank(self):  # FLY-16
        sid = self.book(status="pending_approval", cfi=None)
        self.login("cfi").post(f"/flight/schedule/{sid}/approve?assign=later")
        row = self.sched(sid)
        self.assertEqual((row["status"], row["cfi_id"], row["needs_review"]), ("scheduled", None, 1))

    def test_approve_default_still_assigns_the_approver(self):  # FLY-16
        sid = self.book(status="pending_approval", cfi=None)
        self.login("cfi").post(f"/flight/schedule/{sid}/approve")
        self.assertEqual(self.sched(sid)["cfi_id"], self.cfi)

    def test_approve_from_calendar_stays_on_calendar(self):  # FLY-39
        sid = self.book(status="pending_approval")
        r = self.login("cfi").post(f"/flight/schedule/{sid}/approve",
                                   headers={"Referer": f"http://localhost/flight/schedule?view=day&date={self.day}"})
        self.assertIn("/flight/schedule?", r.headers["Location"])
        self.assertIn("view=day", r.headers["Location"])
        self.assertIn(f"new={sid}", r.headers["Location"])

    def test_approve_from_dashboard_still_goes_to_dashboard(self):  # FLY-39
        sid = self.book(status="pending_approval")
        r = self.login("cfi").post(f"/flight/schedule/{sid}/approve", headers={"Referer": "http://localhost/flight/dashboard"})
        self.assertTrue(r.headers["Location"].endswith("/flight/dashboard"))

    def test_deny_from_calendar_stays_on_calendar(self):  # FLY-39
        sid = self.book(status="pending_approval")
        r = self.login("cfi").post(f"/flight/schedule/{sid}/deny", data={"reason": "No"},
                                   headers={"Referer": f"http://localhost/flight/schedule?view=week&date={self.day}"})
        self.assertIn("view=week", r.headers["Location"])

    def test_deny_with_only_times_has_no_invented_reason(self):  # FLY-32
        sid = self.book(status="pending_approval")
        html = self.login("cfi").get("/flight/schedule").get_data(as_text=True)
        self.assertNotIn("Conflicts with another booking", html)

    def test_student_note_reaches_the_approving_cfi(self):  # FLY-06
        c = self.login("flight_student")
        c.post("/flight/schedule/new", data=dict(asset_id=str(self.plane), scheduled_date=self.day.isoformat(),
                                                 scheduled_time="10:00", duration_hours="1.5", notes="can we do landings?"))
        row = self.q1("SELECT * FROM scheduled_flights WHERE status = 'pending_approval'")
        self.assertEqual((row["private_notes"], row["notes"]), ("can we do landings?", None))
        html = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("can we do landings?", html)
        form = self.login("flight_student").get("/flight/schedule/new").get_data(as_text=True)
        self.assertIn("Note to the school", form)
        self.assertNotIn("Private Note", form)


class DashboardTest(FlyBase):
    def test_acknowledge_in_place_returns_json(self):  # FLY-17
        sid = self.book(cfi=None, needs_review=1, review_reason="No instructor assigned yet")
        r = self.login("cfi").post(f"/flight/schedule/{sid}/review/acknowledge", headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertTrue(r.json["ok"])
        self.assertIn("Alerts", r.json["link_text"] + r.json["link"].title())
        self.assertIsNotNone(self.sched(sid)["needs_review_acknowledged_at"])

    def test_acknowledge_without_js_still_goes_to_alerts(self):  # FLY-17
        sid = self.book(cfi=None, needs_review=1, review_reason="No instructor assigned yet")
        r = self.login("cfi").post(f"/flight/schedule/{sid}/review/acknowledge")
        self.assertIn("/flight/alerts", r.headers["Location"])

    def test_recent_flights_rows_open_the_flight(self):  # FLY-22
        fid = self.running_flight(hobbs_start=1.0, hobbs_end=2.0, ended_at=db.now_iso())
        html = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn(f"/flight/log/{fid}", html)
        self.assertIn("Flight History &raquo;", html)

    def test_student_gets_request_button_and_open_upcoming(self):  # FLY-23
        html = self.login("flight_student").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Request a Flight", html)
        self.assertIn("Nothing booked for you coming up", html)
        self.book(day=self.day)
        html = self.login("flight_student").get("/flight/dashboard").get_data(as_text=True)
        toggle = html[html.index('data-collapse="upcoming"') - 200:html.index('data-collapse="upcoming"')]
        self.assertIn("open", toggle)

    def test_all_mine_swaps_in_place(self):  # FLY-29
        c = self.login("cfi")
        html = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn('data-dash-scope="mine"', html)
        c.get("/flight/dashboard/live?scope=mine")
        self.assertIn("Showing only your flights", c.get("/flight/dashboard/live").get_data(as_text=True))

    def test_withdraw_change_request(self):  # FLY-35
        sid = self.book(change_request_note="Can we move it?", change_requested_at=db.now_iso())
        html = self.login("flight_student").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Withdraw", html)
        r = self.login("flight_student").post(f"/flight/schedule/{sid}/change_request/withdraw",
                                              headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertTrue(r.json["ok"])
        self.assertIsNone(self.sched(sid)["change_requested_at"])
        # someone else's request can't be withdrawn
        sid2 = self.book(change_request_note="x", change_requested_at=db.now_iso(), student_id=self.make_other_student())
        r = self.login("flight_student").post(f"/flight/schedule/{sid2}/change_request/withdraw",
                                              headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertFalse(r.json["ok"])

    def make_other_student(self):
        conn = db.get_db()
        sid = seed_row(conn, "students", name="Other Student", active=1)
        conn.commit()
        conn.close()
        return sid


class WordingTest(FlyBase):
    def test_one_name_for_logging_a_past_session(self):  # FLY-13
        html = self.login("cfi").get("/flight/log").get_data(as_text=True)   # FLY-30: no longer a dashboard tile
        self.assertIn("Log a Past Session", html)
        self.assertNotIn("Log a Flight", html)
        dash = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        self.assertNotIn("Log a Past Session", dash)
        form = self.login("cfi").get("/flight/schedule/new?complete=1").get_data(as_text=True)
        self.assertIn("Log a past session instead of booking", form)
        self.assertIn("Log a Past Session", form)
        err = self.login("cfi").post("/flight/schedule/new", data=dict(
            asset_id=str(self.plane), student_id=str(self.student), scheduled_date="2020-01-01", scheduled_time="09:00"))
        self.assertIn("Log a Past Session", err.get_data(as_text=True))
        active = self.login("cfi").get("/flight/log/active").get_data(as_text=True)
        self.assertIn("Log a Past Session", active)

    def test_modal_shows_balance_hold_in_words(self):  # FLY-14
        html = self.login("cfi").get("/flight/schedule").get_data(as_text=True)
        self.assertIn("Pending - Balance Hold (student owes more than the school", html)
        self.assertIn("1.5 hrs (default)", html)

    def test_history_footer_and_older_flights(self):  # FLY-24, FLY-25
        for i in range(103):
            self.running_flight(cfi_id=self.cfi, hobbs_start=1.0, hobbs_end=2.0, ended_at=db.now_iso())
        c = self.login("cfi")
        html = c.get("/flight/log").get_data(as_text=True)
        self.assertNotIn("coming in a later phase", html)
        self.assertIn("Show older flights", html)
        self.assertEqual(html.count('class="history-row"'), 100)
        html = c.get("/flight/log?limit=200").get_data(as_text=True)
        self.assertNotIn("Show older flights", html)
        self.assertEqual(html.count('class="history-row"'), 103)

    def test_flight_detail_times_and_booking_link(self):  # FLY-26
        sid = self.book(day=date.today())
        fid = self.running_flight(scheduled_flight_id=sid, started_at="2026-10-02 14:03:17",
                                  ended_at="2026-10-02 15:41:02", instructor_clock_hours=1.63)
        html = self.login("cfi").get(f"/flight/log/{fid}").get_data(as_text=True)
        self.assertIn("02-10-2026 2:03 PM", html)
        self.assertIn("1 hr 38 min (1.63 hrs)", html)
        self.assertIn("view=day", html)
        self.assertNotIn("2026-10-02 14:03:17", html)

    def test_waitlist_wording_and_requested_offer_hidden(self):  # FLY-34
        gone = self.book(status="cancelled", day=self.day, time="09:00")
        conn = db.get_db()
        wid = seed_row(conn, "flight_waitlist", student_id=self.student, days="0", periods="morning", active=1)
        oid = seed_row(conn, "waitlist_offers", waitlist_id=wid, student_id=self.student, cancelled_flight_id=gone,
                       asset_id=self.plane, scheduled_date=self.day.isoformat(), scheduled_time="09:00",
                       duration_hours=1.5)
        conn.commit()
        conn.close()
        c = self.login("flight_student")
        html = c.get("/flight/waitlist").get_data(as_text=True)
        self.assertIn("first request a CFI approves gets the slot", html)
        self.assertIn("Grab it", html)
        self.book(status="pending_approval", day=self.day, time="09:00")
        self.assertNotIn("Grab it", c.get("/flight/waitlist").get_data(as_text=True))

    def test_key_matches_the_calendar(self):  # FLY-36
        html = self.login("cfi").get("/flight/schedule").get_data(as_text=True)
        self.assertNotIn('a "+N more" link to that day', html)
        self.assertIn("Dashed see-through box", html)
        self.assertIn("Orange dashed outline", html)

    def test_schedule_toolbar_has_a_more_menu(self):  # FLY-20
        html = self.login("cfi").get("/flight/schedule").get_data(as_text=True)
        more = html[html.index("<i class=\"bi bi-three-dots\"></i> More"):]
        self.assertIn("Waitlist", more[:900])
        self.assertIn("Weather cancellation", more[:1800])
        self.assertIn("border-danger", more[:1800])


class TimeOffTest(FlyBase):
    def test_warns_about_booked_lessons_then_flags_them(self):  # FLY-18
        sid = self.book(day=self.day, time="09:00")
        c = self.login("cfi")
        data = dict(off_date=self.day.isoformat(), note="vacation", view_date=self.day.isoformat())
        r = c.post("/flight/cfis/schedule", data=data)
        html = r.get_data(as_text=True)
        self.assertIn("already booked then", html)
        self.assertIn("Add time off anyway", html)
        self.assertEqual(self.q("SELECT id FROM cfi_time_off"), [])
        self.assertEqual(self.sched(sid)["needs_review"] or 0, 0)
        c.post("/flight/cfis/schedule", data=dict(data, confirm_overlap="1"))
        self.assertEqual(len(self.q("SELECT id FROM cfi_time_off")), 1)
        row = self.sched(sid)
        self.assertEqual(row["needs_review"], 1)
        self.assertIn("time off", row["review_reason"])

    def test_no_warning_when_nothing_is_booked(self):  # FLY-18
        r = self.login("cfi").post("/flight/cfis/schedule", data=dict(off_date=self.day.isoformat()))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.q("SELECT id FROM cfi_time_off")), 1)

    def test_deactivated_instructor_cannot_change_time_off(self):  # FLY-38
        c = self.login("cfi")
        self.exec("UPDATE users SET active = 0 WHERE id = ?", (self.users["cfi"]["id"],))
        c.post("/flight/cfis/schedule", data=dict(off_date=self.day.isoformat()))
        self.assertEqual(self.q("SELECT id FROM cfi_time_off"), [])
        off = self.exec("INSERT INTO cfi_time_off (cfi_id, off_date) VALUES (?, ?)", (self.cfi, self.day.isoformat()))
        c.post(f"/flight/cfis/schedule/time-off/{off}/delete")
        self.assertEqual(len(self.q("SELECT id FROM cfi_time_off")), 1)


class BookNextAfterLoggingTest(FlyBase):
    def test_book_next_box_after_logging_a_booked_flight(self):  # FLY-40
        self.exec("UPDATE assets SET hobbs_hours = 100, tach_hours = 100 WHERE id = ?", (self.plane,))
        sid = self.book(day=date.today(), time="14:00")
        c = self.login("cfi")
        r = c.post("/flight/log/new", data=dict(asset_id=str(self.plane), student_id=str(self.student),
                                                cfi_id=str(self.cfi), flight_date=date.today().isoformat(),
                                                scheduled_flight_id=str(sid), hobbs_end="101.0", tach_end="101.0"))
        self.assertIn("logged_flight_id=", r.headers["Location"])
        html = c.get(r.headers["Location"]).get_data(as_text=True)
        nxt = date.today() + timedelta(days=7)
        self.assertIn("Book <strong>", html)
        self.assertIn(nxt.strftime("%d-%m-%Y") + " 2:00 PM", html)


class FormCancelTest(FlyBase):
    def test_cancel_goes_back_to_where_the_form_was_opened(self):  # FLY-31
        c = self.login("cfi")
        html = c.get("/flight/schedule/new", headers={"Referer": "http://localhost/flight/log"}).get_data(as_text=True)
        self.assertIn('name="return_to" value="/flight/log"', html)
        self.assertIn('href="/flight/log" class="btn btn-outline-secondary"', html)
        html = c.get("/flight/schedule/new").get_data(as_text=True)
        self.assertIn('value=""', html)


class SchoolDayTest(FlyBase):
    def test_one_window_everywhere(self):  # FLY-37
        c = self.login("master")
        html = c.get("/flight/schedule/availability").get_data(as_text=True)
        self.assertIn("6 AM - 9 PM", html)
        self.assertIn("6:00 AM", html)
        r = c.post("/flight/settings/school", data=dict(day_start_hour="7", day_end_hour="19", window_hours="24",
                                                        fee_amount="0", limit="0"))
        self.assertEqual(r.status_code, 302)
        html = c.get("/flight/schedule/availability").get_data(as_text=True)
        self.assertIn("7 AM - 7 PM", html)
        self.assertNotIn("6:00 AM", html)
        self.assertIn("7 AM - 7 PM", c.get("/flight/cfis/schedule").get_data(as_text=True))
        c.get("/flight/schedule?view=day")
        day = c.get("/flight/schedule?view=day").get_data(as_text=True)
        self.assertIn('data-start-min="420"', day)
        self.assertIn('data-end-min="1140"', day)

    def test_bad_window_is_refused(self):  # FLY-37
        c = self.login("master")
        c.post("/flight/settings/school", data=dict(day_start_hour="12", day_end_hour="9", window_hours="24",
                                                    fee_amount="0", limit="0"))
        self.assertIsNone(self.q1("SELECT value FROM app_settings WHERE key = 'school_day_start_hour'"))
