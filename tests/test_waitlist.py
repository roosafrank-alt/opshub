"""Cancellation waitlist (QA feat-cancellation-waitlist): students join with
days / times of day (optionally a plane or instructor); cancelling a booking
offers the slot to every matching student at once; the offer link opens
Schedule Flight pre-filled, or says "already taken".
"""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row, SIDE_EFFECTS
import db


class WaitlistTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        conn = db.get_db()
        self.other_student = seed_row(conn, "students", name="Other Student", active=1)
        conn.commit()
        conn.close()
        self.day = date.today() + timedelta(days=2)

    def book(self, student=None, time="14:00", day=None):
        conn = db.get_db()
        sid = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=student or self.other_student,
                       cfi_id=self.cfi, scheduled_date=(day or self.day).isoformat(), scheduled_time=time,
                       duration_hours=1.5, status="scheduled")
        conn.commit()
        conn.close()
        return sid

    def join(self, days=None, periods=("afternoon",), **extra):
        c = self.login("flight_student")
        data = dict(days=[str(d) for d in (days if days is not None else [self.day.weekday()])],
                    periods=list(periods))
        data.update(extra)
        return c.post("/flight/waitlist/join", data=data)

    def offers(self):
        return self.q("SELECT * FROM waitlist_offers ORDER BY id")

    def test_student_joins_and_sees_entry(self):
        self.assertEqual(self.join().status_code, 302)
        w = self.q1("SELECT * FROM flight_waitlist")
        self.assertEqual((w["student_id"], w["days"], w["periods"]), (self.student, str(self.day.weekday()), "afternoon"))
        html = self.login("flight_student").get("/flight/waitlist").get_data(as_text=True)
        self.assertIn("Afternoon", html)

    def test_join_needs_a_day_and_time(self):
        self.join(days=[])
        self.join(periods=())
        self.assertEqual(self.q("SELECT id FROM flight_waitlist"), [])

    def test_cancel_offers_slot_to_matching_student(self):
        self.join()
        sid = self.book()
        SIDE_EFFECTS.clear()
        self.login("cfi").post(f"/flight/schedule/{sid}/cancel", data={"reason": "test"})
        o = self.offers()
        self.assertEqual(len(o), 1)
        self.assertEqual((o[0]["student_id"], o[0]["scheduled_time"]), (self.student, "14:00"))
        # Tapping the offer opens Schedule Flight with the slot filled in.
        r = self.login("flight_student").get(f"/flight/waitlist/offer/{o[0]['id']}")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/flight/schedule/new", r.location)
        self.assertIn("time=14:00", r.location)
        self.assertIn(f"asset_id={self.plane}", r.location)

    def test_non_matching_entries_get_nothing(self):
        self.join(periods=("morning",))
        self.join(days=[(self.day.weekday() + 1) % 7])
        other_plane = self.make_asset("N999")
        self.join(asset_id=str(other_plane))
        sid = self.book()
        self.login("cfi").post(f"/flight/schedule/{sid}/cancel", data={"reason": "test"})
        self.assertEqual(self.offers(), [])

    def test_offer_already_taken(self):
        self.join()
        sid = self.book()
        self.login("cfi").post(f"/flight/schedule/{sid}/cancel", data={"reason": "test"})
        oid = self.offers()[0]["id"]
        self.book(time="14:30")  # someone else grabbed it first
        r = self.login("flight_student").get(f"/flight/waitlist/offer/{oid}")
        self.assertIn("/flight/waitlist", r.location)
        self.assertNotIn("schedule/new", r.location)

    def test_other_student_cannot_use_offer(self):
        self.join()
        sid = self.book()
        self.login("cfi").post(f"/flight/schedule/{sid}/cancel", data={"reason": "test"})
        oid = self.offers()[0]["id"]
        self.exec("UPDATE waitlist_offers SET student_id = ? WHERE id = ?", (self.other_student, oid))
        self.assertEqual(self.login("flight_student").get(f"/flight/waitlist/offer/{oid}").status_code, 404)

    def test_filled_count_for_staff(self):
        self.join()
        sid = self.book()
        self.login("cfi").post(f"/flight/schedule/{sid}/cancel", data={"reason": "test"})
        self.book(student=self.student)  # the waitlisted student booked it
        html = self.login("cfi").get("/flight/waitlist").get_data(as_text=True)
        self.assertIn("1 filled from waitlist", html)

    def test_student_removes_entry(self):
        self.join()
        wid = self.q1("SELECT id FROM flight_waitlist")["id"]
        self.login("flight_student").post(f"/flight/waitlist/{wid}/remove")
        self.assertEqual(self.q1("SELECT active FROM flight_waitlist")["active"], 0)
