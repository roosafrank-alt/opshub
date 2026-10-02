"""SCHOOL-01: marking a flight paid moves the student's ledger too.

Before this, Billing's Mark Paid only flipped flights.paid, so a student
who had paid still showed "$450.00 owed" everywhere and the Billing
Cross-Check page lit up. flight_payments.py is the one shared helper every
mark-paid path uses: Mark Paid records the payment, Mark Unpaid reverses
its own line, Add Funds can apply money straight to unpaid flights, and
nothing is ever counted twice. The checks below use the Cross-Check logic
itself (flight._billing_cross_check) as the referee.
"""
from datetime import date

from harness import OpsHubTestCase, seed_row
import db
import flight
import flight_payments


class FlightPaymentsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?",
                                  (self.users["flight_student"]["id"],))["id"]
        # $100/hr plane rate, solo flights of 1 hr = $100 each.
        self.exec("UPDATE students SET plane_rate_override = 100 WHERE id = ?", (self.student_id,))

    def _flight(self, hobbs_start=100.0, hobbs_end=101.0, when=None):
        conn = db.get_db()
        fid = seed_row(conn, "flights", asset_id=self.plane, student_id=self.student_id,
                       flight_date=when or date.today().isoformat(), hobbs_start=hobbs_start, hobbs_end=hobbs_end,
                       started_at=None, ended_at=db.now_iso(), solo=1, paid=0, credit_applied=0, payment_amount=None)
        conn.commit()
        with self.app.app_context():
            flight._deduct_flight_cost(conn, flight_payments.load_flight(conn, fid))
        conn.commit()
        conn.close()
        return fid

    def _balance(self):
        return self.q1("SELECT balance FROM students WHERE id = ?", (self.student_id,))["balance"]

    def _clean(self):
        conn = db.get_db()
        with self.app.app_context():
            r = flight._billing_cross_check(conn)
        conn.close()
        return not (r["balance_mismatches"] or r["paid_flag_mismatches"] or r["unlogged_charges"])

    def test_mark_paid_records_payment_and_cross_check_stays_clean(self):
        fid = self._flight()
        self.assertEqual(self._balance(), -100.0)
        c = self.login("master")
        r = c.post(f"/flight/log/{fid}/toggle_paid", data={"paid_method": "Cash"}, follow_redirects=True)
        body = r.get_data(as_text=True)
        self.assertIn("paid for", body)
        self.assertIn("(Cash)", body)
        self.assertEqual(self.q1("SELECT paid, payment_method FROM flights WHERE id = ?", (fid,))["paid"], 1)
        self.assertEqual(self.q1("SELECT payment_method FROM flights WHERE id = ?", (fid,))["payment_method"], "Cash")
        self.assertEqual(self._balance(), 0.0)
        led = self.q("SELECT entry_type, amount FROM student_ledger WHERE flight_id = ? ORDER BY id", (fid,))
        self.assertEqual([(l["entry_type"], l["amount"]) for l in led], [("flight_deduction", -100.0), ("payment", 100.0)])
        self.assertTrue(self._clean())

    def test_mark_paid_twice_does_not_double_count(self):
        fid = self._flight()
        conn = db.get_db()
        with self.app.app_context():
            flight_payments.record_flight_payment(conn, fid, method="Cash")
            again = flight_payments.record_flight_payment(conn, fid, method="Cash")
        conn.commit()
        conn.close()
        self.assertTrue(again["already_paid"])
        self.assertEqual(again["recorded"], 0.0)
        self.assertEqual(self._balance(), 0.0)
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM student_ledger WHERE flight_id = ? AND entry_type = 'payment'", (fid,))["c"], 1)
        self.assertTrue(self._clean())

    def test_mark_unpaid_reverses_only_its_own_line(self):
        fid = self._flight()
        c = self.login("master")
        c.post(f"/flight/log/{fid}/toggle_paid", data={"paid_method": "Cash"})
        r = c.post(f"/flight/log/{fid}/toggle_paid", follow_redirects=True)
        self.assertIn("unpaid for", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id = ?", (fid,))["paid"], 0)
        self.assertEqual(self._balance(), -100.0)
        rev = self.q1("SELECT amount FROM student_ledger WHERE flight_id = ? AND entry_type = 'payment_reversal'", (fid,))
        self.assertEqual(rev["amount"], -100.0)
        self.assertTrue(self._clean())
        # ...and paying again after that records exactly one more payment.
        c.post(f"/flight/log/{fid}/toggle_paid", data={"paid_method": "Check"})
        self.assertEqual(self._balance(), 0.0)
        self.assertTrue(self._clean())

    def test_flight_paid_at_session_end_is_not_charged_again_on_mark_unpaid_then_paid(self):
        # A flight that was paid on the spot at End Session already has its
        # 'payment' ledger line - the helper sees it and never doubles up.
        fid = self._flight()
        conn = db.get_db()
        with self.app.app_context():
            flight._ledger_entry(conn, self.student_id, "payment", 100.0, note="Paid at flight (Cash)", flight_id=fid)
        conn.execute("UPDATE flights SET paid = 1, payment_method = 'Cash', payment_amount = 100 WHERE id = ?", (fid,))
        conn.commit()
        conn.close()
        self.assertEqual(self._balance(), 0.0)
        c = self.login("master")
        c.post(f"/flight/log/{fid}/toggle_paid")           # unpaid: -100 reversal
        self.assertEqual(self._balance(), -100.0)
        c.post(f"/flight/log/{fid}/toggle_paid", data={"paid_method": "Cash"})  # paid again: +100
        self.assertEqual(self._balance(), 0.0)
        self.assertTrue(self._clean())

    def test_mark_selected_paid_pays_each_flight_once(self):
        f1, f2 = self._flight(100, 101), self._flight(101, 102)
        c = self.login("master")
        r = c.post("/flight/billing/mark-selected-paid", data={"ids": f"{f1},{f2},{f1}", "paid_method": "Venmo/Zelle"},
                   follow_redirects=True)
        self.assertIn("Marked 2 flights ($200.00) paid (Venmo/Zelle)", r.get_data(as_text=True))
        self.assertEqual(self._balance(), 0.0)
        self.assertTrue(self._clean())

    def test_add_funds_applies_money_to_ticked_flights_and_keeps_the_rest_as_credit(self):
        f1, f2 = self._flight(100, 101, when="2026-09-01"), self._flight(101, 102, when="2026-09-02")
        self.assertEqual(self._balance(), -200.0)
        c = self.login("master")
        r = c.post(f"/flight/students/{self.student_id}/add_funds",
                   data={"amount": "250", "note": "Check #1024", "apply_flight_ids": [str(f1), str(f2)]},
                   follow_redirects=True)
        body = r.get_data(as_text=True)
        self.assertIn("Added $250.00", body)
        self.assertIn("$50.00 left as credit", body)
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id = ?", (f1,))["paid"], 1)
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id = ?", (f2,))["paid"], 1)
        self.assertEqual(self._balance(), 50.0)
        # Ledger rose by exactly the $250 added: two $100 payments + $50 funds.
        total = self.q1("SELECT SUM(amount) t FROM student_ledger WHERE student_id = ? AND entry_type IN ('payment', 'funds_added')",
                        (self.student_id,))["t"]
        self.assertEqual(total, 250.0)
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM student_ledger WHERE entry_type = 'funds_added' AND student_id = ?",
                                 (self.student_id,))["c"], 1)

    def test_add_funds_refuses_when_ticked_flights_need_more_than_added(self):
        f1, f2 = self._flight(100, 101), self._flight(101, 102)
        c = self.login("master")
        r = c.post(f"/flight/students/{self.student_id}/add_funds",
                   data={"amount": "150", "apply_flight_ids": [str(f1), str(f2)]}, follow_redirects=True)
        self.assertIn("untick some or add more", r.get_data(as_text=True))
        self.assertEqual(self._balance(), -200.0)
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id = ?", (f1,))["paid"], 0)

    def test_add_funds_without_ticks_is_a_plain_top_up(self):
        self._flight()
        c = self.login("master")
        c.post(f"/flight/students/{self.student_id}/add_funds", data={"amount": "40"})
        self.assertEqual(self._balance(), -60.0)
        self.assertEqual(self.q1("SELECT entry_type FROM student_ledger WHERE student_id = ? ORDER BY id DESC LIMIT 1",
                                 (self.student_id,))["entry_type"], "funds_added")

    def test_pay_with_card_on_billing_pays_every_unpaid_flight(self):
        f1, f2 = self._flight(100, 101), self._flight(101, 102)
        c = self.login("master")
        r = c.post(f"/flight/billing/student/{self.student_id}/pay-card", data={"card_number": "4242 4242 4242 4242"},
                   follow_redirects=True)
        self.assertIn("Charged $200.00 to the card ending in 4242", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT paid, card_last4, payment_method FROM flights WHERE id = ?", (f2,))["card_last4"], "4242")
        self.assertEqual(self.q1("SELECT payment_method FROM flights WHERE id = ?", (f1,))["payment_method"], "Card")
        self.assertEqual(self._balance(), 0.0)
        self.assertTrue(self._clean())

    def test_billing_page_has_the_new_controls(self):
        self._flight()
        html = self.login("master").get("/flight/billing").get_data(as_text=True)
        self.assertIn("Mark <span id=\"billing-selected-n\">0</span> selected paid", html)
        self.assertIn("Pay with Card", html)
        self.assertIn("How was it paid?", html)
        self.assertIn("Connect Wave to send real invoices", html)
        self.assertNotIn("/billing/student/", html.split("pay-card")[0].split("wave-invoice")[0][-0:] or "zzz")

    def test_old_per_student_mark_paid_route_is_gone(self):
        r = self.login("master").post(f"/flight/billing/student/{self.student_id}/mark_paid")
        self.assertEqual(r.status_code, 404)

    def test_my_account_shows_owed_without_a_minus_sign(self):
        self._flight()
        html = self.login("flight_student").get("/flight/my-account").get_data(as_text=True)
        self.assertIn("$100.00 owed", html)
        self.assertNotIn("$-100.00", html)
