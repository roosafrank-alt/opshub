"""Idea "Credit card" (revision): Frank's follow-up asked for the same
simulated Stripe-style "Pay with Card" (already on Manage > Billing) to also
show on Fly with Kate's End Flight payment box and on a student's Add Funds
- still no real Stripe account, no network call, just a fake charge id and
the card's last 4 digits kept alongside the usual paid fields."""
from datetime import date

from harness import OpsHubTestCase
import db


class EndFlightPayWithCardTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.exec("UPDATE cfis SET rate_per_hour = 100 WHERE id = ?", (self.cfi,))
        self.asset = self.make_asset("N321CC")

    def start_flight(self):
        started_at = (date.today().isoformat() + " 08:00:00")
        stopped_at = (date.today().isoformat() + " 09:00:00")
        return self.exec(
            "INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, solo, started_at, stopped_at, created_at) "
            "VALUES (?,?,?,?,0,?,?,?)",
            (self.cfi, self.student, self.asset, date.today().isoformat(), started_at, stopped_at, db.now_iso()))

    def flight_row(self, fid):
        return self.q1("SELECT * FROM flights WHERE id = ?", (fid,))

    def test_card_payment_at_end_flight_stores_last4(self):
        fid = self.start_flight()
        c = self.login("cfi")
        r = c.post(f"/flight/log/{fid}/end", data={
            "hobbs_start": "100.0", "hobbs_end": "101.0", "tach_start": "100.0", "tach_end": "101.0",
            "paid": "1", "payment_method": "Card", "payment_amount": "100.00",
            "card_number": "4242 4242 4242 4242",
        })
        self.assertEqual(r.status_code, 302)
        row = self.flight_row(fid)
        self.assertEqual(row["payment_method"], "Card")
        self.assertEqual(row["card_last4"], "4242")
        self.assertTrue(row["card_charge_id"].startswith("sim_ch_"))

    def test_card_payment_without_a_card_number_is_refused(self):
        fid = self.start_flight()
        c = self.login("cfi")
        r = c.post(f"/flight/log/{fid}/end", data={
            "hobbs_start": "100.0", "hobbs_end": "101.0", "tach_start": "100.0", "tach_end": "101.0",
            "paid": "1", "payment_method": "Card", "payment_amount": "100.00",
        })
        self.assertEqual(r.status_code, 302)
        row = self.flight_row(fid)
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(row["card_last4"])

    def test_cash_payment_leaves_card_fields_empty(self):
        fid = self.start_flight()
        c = self.login("cfi")
        c.post(f"/flight/log/{fid}/end", data={
            "hobbs_start": "100.0", "hobbs_end": "101.0", "tach_start": "100.0", "tach_end": "101.0",
            "paid": "1", "payment_method": "Cash", "payment_amount": "100.00",
        })
        row = self.flight_row(fid)
        self.assertEqual(row["payment_method"], "Cash")
        self.assertIsNone(row["card_last4"])


class AddFundsPayWithCardTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]

    def test_add_funds_with_card_credits_balance_and_notes_the_charge(self):
        c = self.login("cfi_billing")
        r = c.post(f"/flight/students/{self.student}/add_funds",
                   data={"amount": "50.00", "paid_by_card": "1", "card_number": "4111111111111111"})
        self.assertEqual(r.status_code, 302)
        student = self.q1("SELECT balance FROM students WHERE id = ?", (self.student,))
        self.assertAlmostEqual(student["balance"], 50.00)
        entry = self.q1("SELECT * FROM student_ledger WHERE student_id = ? ORDER BY id DESC LIMIT 1", (self.student,))
        self.assertIn("1111", entry["note"])
        self.assertIn("simulated", entry["note"])

    def test_add_funds_with_card_checked_but_no_number_is_refused(self):
        c = self.login("cfi_billing")
        c.post(f"/flight/students/{self.student}/add_funds", data={"amount": "50.00", "paid_by_card": "1"})
        student = self.q1("SELECT balance FROM students WHERE id = ?", (self.student,))
        self.assertEqual(student["balance"], 0)

    def test_add_funds_without_card_checkbox_is_unaffected(self):
        c = self.login("cfi_billing")
        c.post(f"/flight/students/{self.student}/add_funds", data={"amount": "50.00", "note": "Check #100"})
        student = self.q1("SELECT balance FROM students WHERE id = ?", (self.student,))
        self.assertAlmostEqual(student["balance"], 50.00)
        entry = self.q1("SELECT * FROM student_ledger WHERE student_id = ? ORDER BY id DESC LIMIT 1", (self.student,))
        self.assertEqual(entry["note"], "Check #100")
