"""Approving a request that has no instructor yet pops up the instructors who
are free then (schedule_available_cfis), and the one picked gets booked."""
from harness import seed_row
import db
from test_fly_preview import FlyBase


class PickInstructorTest(FlyBase):
    def free_ids(self, sid, who="cfi"):
        r = self.login(who).get(f"/flight/schedule/{sid}/available-cfis")
        self.assertEqual(r.status_code, 200)
        return {c["id"] for c in r.get_json()["cfis"]}

    def test_lists_instructors_who_are_free(self):
        sid = self.book(status="pending_approval", cfi=None)
        self.assertTrue({self.cfi, self.cfi2} <= self.free_ids(sid))

    def test_leaves_out_a_booked_instructor_and_one_on_time_off(self):
        other_plane = self.make_asset("N456")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (other_plane,))
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=other_plane, student_id=self.student, cfi_id=self.cfi,
                 scheduled_date=self.day.isoformat(), scheduled_time="09:00", duration_hours=1.0, status="scheduled")
        conn.commit()
        conn.close()
        sid = self.book(status="pending_approval", cfi=None, time="09:30")
        ids = self.free_ids(sid)
        self.assertNotIn(self.cfi, ids)
        self.assertIn(self.cfi2, ids)
        conn = db.get_db()
        seed_row(conn, "cfi_time_off", cfi_id=self.cfi2, off_date=self.day.isoformat(), created_at=db.now_iso())
        conn.commit()
        conn.close()
        self.assertNotIn(self.cfi2, self.free_ids(sid))

    def test_approve_books_the_picked_instructor(self):
        sid = self.book(status="pending_approval", cfi=None)
        self.login("cfi").post(f"/flight/schedule/{sid}/approve", data={"cfi_id": str(self.cfi2)})
        row = self.sched(sid)
        self.assertEqual((row["status"], row["cfi_id"]), ("scheduled", self.cfi2))

    def test_approve_without_a_pick_still_books_the_approver(self):
        sid = self.book(status="pending_approval", cfi=None)
        self.login("cfi").post(f"/flight/schedule/{sid}/approve")
        self.assertEqual(self.sched(sid)["cfi_id"], self.cfi)

    def test_approve_refuses_a_bogus_instructor(self):
        sid = self.book(status="pending_approval", cfi=None)
        self.login("cfi").post(f"/flight/schedule/{sid}/approve", data={"cfi_id": "99999"})
        self.assertEqual(self.sched(sid)["status"], "pending_approval")

    def test_a_student_cannot_list_instructors(self):
        sid = self.book(status="pending_approval", cfi=None)
        r = self.login("flight_student").get(f"/flight/schedule/{sid}/available-cfis")
        self.assertNotEqual(r.status_code, 200)

    def test_calendar_modal_has_the_picker(self):
        sid = self.book(status="pending_approval", cfi=None)
        html = self.login("cfi").get(f"/flight/schedule?view=day&date={self.day}").get_data(as_text=True)
        self.assertIn('id="pickCfiModal"', html)
