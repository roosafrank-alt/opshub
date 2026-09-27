"""Idea "approving flights": a CFI/admin should be able to approve, deny,
or propose new times for a pending flight request from the Dashboard's
Pending Approval section AND from every Schedule view (Day, Month, List) -
not just one or the other. The Schedule Detail popup is shared everywhere,
so a pending booking just needs a data-approve-url attribute (mirroring the
existing data-deny-url) wherever it's rendered; the Dashboard's own Deny
modal grew an optional quick-pick-times field so it can also propose times,
matching what the calendar's popup already did the other way.
"""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class CfiApproveEverywhereTest(OpsHubTestCase):
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

    def _approve_url_present(self, html):
        self.assertIn(f"/flight/schedule/{self.sched}/approve", html)

    def test_day_view_carries_the_approve_url(self):
        html = self.login("cfi").get(f"/flight/schedule?view=day&date={self.day.isoformat()}").get_data(as_text=True)
        self._approve_url_present(html)

    def test_month_view_carries_the_approve_url(self):
        html = self.login("cfi").get(f"/flight/schedule?view=month&date={self.day.isoformat()}").get_data(as_text=True)
        self._approve_url_present(html)

    def test_list_view_carries_the_approve_url(self):
        html = self.login("cfi").get(f"/flight/schedule?view=list&date={self.day.isoformat()}").get_data(as_text=True)
        self._approve_url_present(html)

    def test_a_student_does_not_get_the_approve_url(self):
        html = self.login("flight_student").get(f"/flight/schedule?view=day&date={self.day.isoformat()}").get_data(as_text=True)
        self.assertNotIn("/approve", html)

    def test_denying_with_no_proposed_times_still_works(self):
        r = self.login("cfi").post(f"/flight/schedule/{self.sched}/deny",
                                   data={"reason": "Plane's down for maintenance that day."})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT status, deny_reason, proposed_times FROM scheduled_flights WHERE id=?", (self.sched,))
        self.assertEqual(row["status"], "denied")
        self.assertIsNone(row["proposed_times"])

    def test_dashboard_deny_modal_can_send_proposed_times(self):
        r = self.login("cfi").post(f"/flight/schedule/{self.sched}/deny",
                                   data={"reason": "Conflicts. Try one of these instead.",
                                         "proposed_times": '["8:00a", "2:00p"]'},
                                   headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        row = self.q1("SELECT proposed_times FROM scheduled_flights WHERE id=?", (self.sched,))
        self.assertIn("8:00a", row["proposed_times"])

    def test_dashboard_pending_card_still_has_approve_and_deny(self):
        html = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn(f"/flight/schedule/{self.sched}/approve", html)
        self.assertIn(f"/flight/schedule/{self.sched}/deny", html)
        self.assertIn("Deny / Propose New Times", html)

    def test_approving_from_the_calendar_uses_the_same_route_as_the_dashboard(self):
        r = self.login("cfi").post(f"/flight/schedule/{self.sched}/approve")
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT status FROM scheduled_flights WHERE id=?", (self.sched,))
        self.assertEqual(row["status"], "scheduled")
