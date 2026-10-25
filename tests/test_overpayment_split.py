"""Paying more than a session costs: the ledger note, the receipt and the
End Session message say how much went to the session and how much was added
to the student's credit."""
import unittest
import flight


class OverpaymentSplitTest(unittest.TestCase):
    def test_example_100_session_500_paid(self):
        self.assertEqual(flight._overpayment_split(100.0, 0.0, 500.0), (100.0, 400.0))

    def test_exact_payment_has_no_credit(self):
        self.assertEqual(flight._overpayment_split(100.0, 0.0, 100.0), (100.0, 0.0))

    def test_credit_applied_first(self):
        self.assertEqual(flight._overpayment_split(100.0, 40.0, 100.0), (60.0, 40.0))

    def test_underpayment(self):
        self.assertEqual(flight._overpayment_split(100.0, 0.0, 60.0), (60.0, 0.0))

    def test_note_only_changes_for_overpayment(self):
        self.assertEqual(flight._paid_at_flight_note("Paid at flight (Cash)", 100.0, 0.0, 100.0), "Paid at flight (Cash)")
        n = flight._paid_at_flight_note("Paid at flight (Cash)", 100.0, 0.0, 500.0)
        self.assertEqual(n, "Paid at flight (Cash) - $100.00 paid this session, $400.00 added to credit")


if __name__ == "__main__":
    unittest.main()
