"""Idea 'Schedule conflict': a CFI/admin denying a student's conflicting
flight request can propose specific times instead (Propose New Times on the
Schedule Detail popup - see proposeTimesModal/schedule_deny). This tests the
follow-up revision: the student's "Your Requests" card renders those
proposed times as one-click buttons (schedule_accept_proposed_time) instead
of the student having to retype a brand new request by hand.
"""
import json
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class ProposeTimesTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.day = date.today() + timedelta(days=3)
        conn = db.get_db()
        self.sched = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student,
                              cfi_id=self.cfi, scheduled_date=self.day.isoformat(), scheduled_time="09:00",
                              duration_hours=1.5, status="pending_approval", created_by="Flight Student")
        conn.commit()
        conn.close()

    def deny_with_times(self, times, note=""):
        return self.login("cfi").post(f"/flight/schedule/{self.sched}/deny", data={
            "reason": "Conflicts with another booking. Times that would work instead: " + ", ".join(times)
                      + (" - " + note if note else ""),
            "proposed_times": json.dumps(times),
        })

    def test_deny_stores_proposed_times_as_json(self):
        self.deny_with_times(["8:00a", "2:00p"])
        row = self.q1("SELECT status, proposed_times FROM scheduled_flights WHERE id=?", (self.sched,))
        self.assertEqual(row["status"], "denied")
        self.assertEqual(json.loads(row["proposed_times"]), ["8:00a", "2:00p"])

    def test_garbled_or_missing_proposed_times_does_not_crash(self):
        r = self.login("cfi").post(f"/flight/schedule/{self.sched}/deny",
                                   data={"reason": "No times work this week.", "proposed_times": "not json"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT status, proposed_times FROM scheduled_flights WHERE id=?", (self.sched,))
        self.assertEqual((row["status"], row["proposed_times"]), ("denied", None))

    def test_student_dashboard_shows_accept_buttons_for_proposed_times(self):
        self.deny_with_times(["8:00a", "9:30a"])
        html = self.login("flight_student").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("accept-proposed-time-btn", html)
        self.assertIn("8:00a", html)
        self.assertIn("9:30a", html)

    def test_student_accepts_a_proposed_time_creates_new_pending_request(self):
        self.deny_with_times(["8:00a", "2:00p"])
        c = self.login("flight_student")
        r = c.post(f"/flight/schedule/{self.sched}/accept-proposed-time", data={"time": "2:00p"},
                  headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json["ok"], r.json)
        rows = self.q("SELECT * FROM scheduled_flights WHERE student_id=? ORDER BY id", (self.student,))
        self.assertEqual(len(rows), 2)
        original, new = rows
        self.assertEqual(original["status"], "denied")
        self.assertIsNotNone(original["student_dismissed_at"])
        self.assertEqual((new["status"], new["scheduled_time"], new["scheduled_date"], new["asset_id"], new["cfi_id"]),
                         ("pending_approval", "14:00", self.day.isoformat(), self.plane, self.cfi))

    def test_cannot_accept_a_time_that_was_not_offered(self):
        self.deny_with_times(["8:00a"])
        c = self.login("flight_student")
        r = c.post(f"/flight/schedule/{self.sched}/accept-proposed-time", data={"time": "3:30p"},
                  headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertFalse(r.json["ok"])
        self.assertEqual(self.q("SELECT id FROM scheduled_flights WHERE student_id=?", (self.student,)).__len__(), 1)

    def test_another_student_cannot_accept_someone_elses_proposed_time(self):
        self.deny_with_times(["8:00a"])
        conn = db.get_db()
        other_user = seed_row(conn, "users", username="otherstu", name="Other Student", flight_role="student",
                              password_hash=self.users["flight_student"]["password_hash"], active=1,
                              created_at=db.now_iso())
        other_student = seed_row(conn, "students", name="Other Student", active=1, user_id=other_user)
        conn.commit()
        conn.close()
        with self.client.session_transaction() as s:
            pass
        c = self.client
        with c.session_transaction() as s:
            s["user_id"] = other_user
            s["student_id"] = other_student
            s["flight_role"] = "student"
        r = c.post(f"/flight/schedule/{self.sched}/accept-proposed-time", data={"time": "8:00a"},
                  headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertFalse(r.json["ok"])

    def test_cannot_accept_from_a_still_pending_or_already_actioned_request(self):
        # Never denied - nothing to accept.
        c = self.login("flight_student")
        r = c.post(f"/flight/schedule/{self.sched}/accept-proposed-time", data={"time": "8:00a"},
                  headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertFalse(r.json["ok"])
        # Denied but no times were proposed (a plain deny).
        self.login("cfi").post(f"/flight/schedule/{self.sched}/deny", data={"reason": "No times work this week."})
        r = c.post(f"/flight/schedule/{self.sched}/accept-proposed-time", data={"time": "8:00a"},
                  headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertFalse(r.json["ok"])
