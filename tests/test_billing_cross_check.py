"""Idea "Billing cross check": OpsHub tracks what a student owes two
separate ways - the Billing page's per-flight paid/unpaid checkbox, and
the student_ledger/students.balance running total (what Add Funds, flight
logging, and balance-hold actually use). Nothing keeps them in sync
automatically, so flight._billing_cross_check flags where they've drifted:
a stored balance that disagrees with its own ledger, an unpaid-flights
total that disagrees with what's owed, and a finished flight with no
ledger entry at all.
"""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db
import flight


class BillingCrossCheckTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?",
                                  (self.users["flight_student"]["id"],))["id"]

    def _finished_flight(self, **extra):
        conn = db.get_db()
        fid = seed_row(conn, "flights", asset_id=self.plane, student_id=self.student_id,
                       flight_date=date.today().isoformat(), hobbs_start=100.0, hobbs_end=101.0,
                       started_at=None, ended_at=db.now_iso(), solo=1, **extra)
        conn.commit()
        conn.close()
        return fid

    def test_clean_when_nothing_has_drifted(self):
        result = flight._billing_cross_check(db.get_db())
        self.assertEqual(result["balance_mismatches"], [])
        self.assertEqual(result["paid_flag_mismatches"], [])
        self.assertEqual(result["unlogged_charges"], [])
        html = self.login("master").get("/flight/billing/cross-check").get_data(as_text=True)
        self.assertIn("Everything checks out", html)

    def test_flags_a_finished_flight_with_no_ledger_entry(self):
        fid = self._finished_flight()
        result = flight._billing_cross_check(db.get_db())
        self.assertEqual(len(result["unlogged_charges"]), 1)
        self.assertEqual(result["unlogged_charges"][0]["id"], fid)
        html = self.login("master").get("/flight/billing/cross-check").get_data(as_text=True)
        self.assertIn("Finished flights never charged", html)
        self.assertNotIn("Everything checks out", html)

    def test_flags_a_balance_that_disagrees_with_its_own_ledger(self):
        # Hand-edit the balance without touching the ledger - exactly the
        # kind of drift this check exists to catch.
        self.exec("UPDATE students SET balance = -999 WHERE id = ?", (self.student_id,))
        result = flight._billing_cross_check(db.get_db())
        self.assertEqual(len(result["balance_mismatches"]), 1)
        m = result["balance_mismatches"][0]
        self.assertEqual(m["student_id"], self.student_id)
        self.assertEqual(m["stored_balance"], -999)
        self.assertEqual(m["ledger_total"], 0)

    def test_flags_a_flight_marked_paid_that_was_never_actually_paid(self):
        # A properly-logged, ledger-backed charge (so no "unlogged" flag),
        # but hand-marked paid without any matching payment - the Billing
        # page would show $0 unpaid for this student while they still owe
        # the flight's cost per the ledger.
        conn = db.get_db()
        with self.app.app_context():
            flight._deduct_flight_cost(conn, dict(id=self._finished_flight(paid=0), student_id=self.student_id,
                                                   hobbs_start=100.0, hobbs_end=101.0, tach_start=None, tach_end=None,
                                                   solo=1, ground_time_hours=0, instructor_clock_hours=None,
                                                   solo_hours=None, plane_rate=100, instructor_rate=0))
        conn.commit()
        self.exec("UPDATE flights SET paid = 1 WHERE student_id = ?", (self.student_id,))
        conn.close()
        result = flight._billing_cross_check(db.get_db())
        self.assertEqual(result["unlogged_charges"], [])
        self.assertEqual(len(result["paid_flag_mismatches"]), 1)
        m = result["paid_flag_mismatches"][0]
        self.assertEqual(m["unpaid_flights_total"], 0)
        self.assertEqual(m["ledger_owed"], 100.0)

    def test_non_billing_role_cannot_reach_the_page(self):
        r = self.login("tech").get("/flight/billing/cross-check", follow_redirects=True)
        self.assertNotIn("Billing Cross-Check", r.get_data(as_text=True))
