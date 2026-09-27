"""Idea "Credit card": a simulated Stripe-style "Pay with Card" button next
to Mark Paid on Billing - no real Stripe account, no network call, just a
fake charge id and the card's last 4 digits kept alongside the usual paid
fields.
"""
from harness import OpsHubTestCase, seed_row
import db


class PayWithCardTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        conn = db.get_db()
        self.laborer = seed_row(conn, "laborers", name="Tech", code="LABOR-A", rate=85, active=1)
        conn.commit()
        conn.close()
        self.project = self.make_project(name="Annual - N12345")
        today = db.now_iso()[:10]
        self.exec("""INSERT INTO labor_sessions (laborer_id, project_id, started_at, ended_at, hours, rate, cost, created_at)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (self.laborer, self.project, today + " 08:00:00", today + " 10:00:00", 2.0, 85, 170.0, db.now_iso()))
        self.exec("UPDATE projects SET payment_status = 'invoiced', invoiced_at = ? WHERE id = ?",
                  (db.now_iso(), self.project))

    def project_row(self):
        return self.q1("SELECT * FROM projects WHERE id = ?", (self.project,))

    def test_pay_with_card_marks_paid_and_keeps_only_last4(self):
        c = self.login("shop_admin")
        c.post(f"/shop/billing/{self.project}/pay-card",
               data={"card_number": "4242 4242 4242 4242", "card_expiry": "12/30", "card_cvc": "123"},
               follow_redirects=True)
        row = self.project_row()
        self.assertEqual(row["payment_status"], "paid")
        self.assertEqual(row["paid_method"], "Card")
        self.assertEqual(row["card_last4"], "4242")
        self.assertTrue(row["card_charge_id"].startswith("sim_ch_"))
        self.assertTrue(row["paid_at"])
        self.assertEqual(row["paid_by"], "Shop Admin")

    def test_billing_page_shows_pay_with_card_button_and_receipt(self):
        c = self.login("shop_admin")
        html = c.get("/shop/billing?period=all").get_data(as_text=True)
        self.assertIn("Pay with Card", html)
        c.post(f"/shop/billing/{self.project}/pay-card", data={"card_number": "4111111111111111"}, follow_redirects=True)
        html = c.get("/shop/billing?period=all").get_data(as_text=True)
        self.assertIn("****1111", html)
        # No action left on this row once it's paid (the modal itself always
        # mentions "Pay with Card" in its title/button, so check the row's
        # own trigger is gone, not the phrase anywhere on the page).
        self.assertNotIn(f"/shop/billing/{self.project}/pay-card", html)

    def test_blank_card_number_is_refused(self):
        c = self.login("shop_admin")
        c.post(f"/shop/billing/{self.project}/pay-card", data={"card_number": ""}, follow_redirects=True)
        self.assertEqual(self.project_row()["payment_status"], "invoiced")

    def test_a_tech_cannot_pay_with_card(self):
        c = self.login("tech")
        c.post(f"/shop/billing/{self.project}/pay-card", data={"card_number": "4242424242424242"}, follow_redirects=True)
        self.assertEqual(self.project_row()["payment_status"], "invoiced")
