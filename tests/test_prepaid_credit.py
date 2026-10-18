"""Prepaid credit section on the student's page: a prepayment with a method
lands on the account as credit, shows in the history, and is billing-gated."""
from harness import OpsHubTestCase
import db


class PrepaidCreditTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]

    def test_prepayment_with_method_adds_credit_and_history(self):
        c = self.login("master")
        r = c.post(f"/flight/students/{self.sid}/add_funds",
                   data={"amount": "300", "paid_method": "Check", "note": "#1024"}, follow_redirects=True)
        self.assertIn("Added $300.00", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT balance FROM students WHERE id = ?", (self.sid,))["balance"], 300.0)
        row = self.q1("SELECT note, entry_type FROM student_ledger WHERE student_id = ?", (self.sid,))
        self.assertEqual(row["entry_type"], "funds_added")
        self.assertEqual(row["note"], "Check: #1024")
        page = c.get(f"/flight/students/{self.sid}/edit").get_data(as_text=True)
        self.assertIn("prepaid credit", page)
        self.assertIn("$300.00 credit", page)
        self.assertIn("Check: #1024", page)

    def test_method_only_and_bad_amount(self):
        c = self.login("master")
        c.post(f"/flight/students/{self.sid}/add_funds", data={"amount": "50", "paid_method": "Cash"})
        self.assertEqual(self.q1("SELECT note FROM student_ledger WHERE student_id = ?", (self.sid,))["note"], "Cash")
        c.post(f"/flight/students/{self.sid}/add_funds", data={"amount": "-5"})
        self.assertEqual(self.q1("SELECT balance FROM students WHERE id = ?", (self.sid,))["balance"], 50.0)

    def test_student_cannot_add_prepayment(self):
        c = self.login("flight_student")
        c.post(f"/flight/students/{self.sid}/add_funds", data={"amount": "99"})
        self.assertEqual(self.q1("SELECT balance FROM students WHERE id = ?", (self.sid,))["balance"], 0.0)


class PrepaidEditTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.c = self.login("master")
        self.c.post(f"/flight/students/{self.sid}/add_funds", data={"amount": "300", "paid_method": "Check", "note": "#1024"})
        self.lid = self.q1("SELECT id FROM student_ledger WHERE student_id = ?", (self.sid,))["id"]

    def test_admin_edit_adjusts_balance_and_logs(self):
        r = self.c.post(f"/flight/students/{self.sid}/ledger/{self.lid}/edit",
                        data={"amount": "250", "paid_method": "Cash", "note": "fixed"}, follow_redirects=True)
        self.assertIn("corrected", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT balance FROM students WHERE id = ?", (self.sid,))["balance"], 250.0)
        row = self.q1("SELECT amount, note, created_by FROM student_ledger WHERE id = ?", (self.lid,))
        self.assertEqual((row["amount"], row["note"]), (250.0, "Cash: fixed"))
        self.assertTrue(row["created_by"])
        e = self.q1("SELECT * FROM student_ledger_edits WHERE ledger_id = ?", (self.lid,))
        self.assertEqual((e["old_amount"], e["new_amount"], e["old_method"], e["new_method"], e["old_note"]),
                         (300.0, 250.0, "Check", "Cash", "#1024"))
        page = self.c.get(f"/flight/students/{self.sid}/edit").get_data(as_text=True)
        self.assertIn("Edited by", page)

    def test_student_cannot_edit(self):
        s = self.login("flight_student")
        s.post(f"/flight/students/{self.sid}/ledger/{self.lid}/edit", data={"amount": "1"})
        self.assertEqual(self.q1("SELECT balance FROM students WHERE id = ?", (self.sid,))["balance"], 300.0)
