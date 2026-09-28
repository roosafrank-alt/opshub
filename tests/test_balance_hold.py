"""QA feat-owed-balance-on-bookings: when a student's owed balance crosses
the school's Balance Hold limit, their upcoming bookings go on hold ('Pending
- Balance Hold') until they pay back down, or until 5 days before a held
lesson if they don't - at which point that slot opens up to everyone. See
flight.check_balance_hold / check_balance_hold_releases.
"""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db
import flight


class BalanceHoldSettingsTest(OpsHubTestCase):
    def test_defaults_to_off(self):
        html = self.login("master").get("/flight/settings/balance-hold").get_data(as_text=True)
        self.assertIsNone(flight._balance_hold_limit(db.get_db()))
        self.assertIn('placeholder="Off"', html)

    def test_admin_sets_the_limit(self):
        c = self.login("master")
        c.post("/flight/settings/balance-hold", data={"limit": "100"})
        self.assertEqual(flight._balance_hold_limit(db.get_db()), 100.0)

    def test_non_admin_cannot_reach_the_settings_page(self):
        r = self.login("cfi").get("/flight/settings/balance-hold", follow_redirects=True)
        self.assertNotIn("Balance Hold", r.get_data(as_text=True))


class BalanceHoldHoldAndReleaseTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?",
                                  (self.users["flight_student"]["id"],))["id"]
        self.login("master").post("/flight/settings/balance-hold", data={"limit": "100"})

    def _book(self, when):
        conn = db.get_db()
        sched = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student_id,
                         scheduled_date=when.isoformat(), scheduled_time="09:00",
                         duration_hours=1.0, status="scheduled", created_by="Front Desk")
        conn.commit()
        conn.close()
        return sched

    def _owe(self, amount):
        conn = db.get_db()
        with self.app.app_context():
            flight._ledger_entry(conn, self.student_id, "funds_added", -amount, note="test charge")
        conn.commit()
        conn.close()

    def _pay(self, amount):
        conn = db.get_db()
        with self.app.app_context():
            flight._ledger_entry(conn, self.student_id, "funds_added", amount, note="test payment")
        conn.commit()
        conn.close()

    def test_crossing_the_limit_holds_upcoming_bookings(self):
        sched = self._book(date.today() + timedelta(days=10))
        self._owe(150)
        row = self.q1("SELECT status, hold_previous_status, held_release_date FROM scheduled_flights WHERE id = ?", (sched,))
        self.assertEqual(row["status"], "balance_hold")
        self.assertEqual(row["hold_previous_status"], "scheduled")
        self.assertEqual(row["held_release_date"], (date.today() + timedelta(days=5)).isoformat())

    def test_paying_back_down_releases_the_hold(self):
        sched = self._book(date.today() + timedelta(days=10))
        self._owe(150)
        self._pay(150)
        row = self.q1("SELECT status FROM scheduled_flights WHERE id = ?", (sched,))
        self.assertEqual(row["status"], "scheduled")

    def test_staying_under_the_limit_does_not_hold(self):
        sched = self._book(date.today() + timedelta(days=10))
        self._owe(50)
        row = self.q1("SELECT status FROM scheduled_flights WHERE id = ?", (sched,))
        self.assertEqual(row["status"], "scheduled")

    def test_release_check_cancels_an_unpaid_flight_past_its_5_day_mark(self):
        sched = self._book(date.today() + timedelta(days=3))
        self._owe(150)
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id = ?", (sched,))["status"], "balance_hold")
        conn = db.get_db()
        with self.app.app_context():
            flight.check_balance_hold_releases(conn)
        conn.close()
        row = self.q1("SELECT status, cancel_reason FROM scheduled_flights WHERE id = ?", (sched,))
        self.assertEqual(row["status"], "cancelled")
        self.assertIn("Balance hold", row["cancel_reason"])

    def test_release_check_leaves_a_still_far_out_flight_held(self):
        sched = self._book(date.today() + timedelta(days=20))
        self._owe(150)
        conn = db.get_db()
        with self.app.app_context():
            flight.check_balance_hold_releases(conn)
        conn.close()
        self.assertEqual(self.q1("SELECT status FROM scheduled_flights WHERE id = ?", (sched,))["status"], "balance_hold")

    def test_dashboard_banner_shows_amount_and_release_date(self):
        self._book(date.today() + timedelta(days=10))
        self._owe(150)
        html = self.login("flight_student").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("balance owed", html)
        # The owed amount is a link to the Account page (idea "header"), not
        # plain text, so check the link and the amount separately.
        self.assertIn('href="/account?from=flight"', html)
        self.assertIn(">$150.00</a>", html)
        self.assertIn("$50.00 over the school", html)
        self.assertIn("Pending - Balance Hold", html)

    def test_no_banner_when_not_held(self):
        self._book(date.today() + timedelta(days=10))
        html = self.login("flight_student").get("/flight/dashboard").get_data(as_text=True)
        self.assertNotIn("balance owed", html)


class BalanceHoldVisibilityTest(OpsHubTestCase):
    """CFIs/admins see who owes what on any booking; a student never sees
    another student's amount, even non-clickable, on the shared dashboard
    chip (flight/_dashboard_live.html's flight_chip macro)."""

    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?",
                                  (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE students SET balance = -75 WHERE id = ?", (self.student_id,))
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student_id,
                 scheduled_date=(date.today() + timedelta(days=1)).isoformat(), scheduled_time="09:00",
                 duration_hours=1.0, status="scheduled", created_by="Front Desk")
        conn.commit()
        conn.close()

    def test_cfi_sees_the_owed_badge_on_the_dashboard(self):
        html = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        # cfi's dashboard is school-wide, so today's/upcoming chips can include
        # other students' bookings - $75 owed should render for staff.
        self.assertIn("owed", html)
