"""SHOP-01: the simulated "Pay with Card" button and its pop-up are gone from
Billing (it marked jobs Paid with a pretend charge). A job that was already
marked paid that way stays readable, labelled as simulated."""
from harness import OpsHubTestCase, seed_row
import db


class PayWithCardRemovedTest(OpsHubTestCase):
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

    def test_billing_page_has_no_pay_with_card(self):
        html = self.login("shop_admin").get("/shop/billing?period=all").get_data(as_text=True)
        self.assertNotIn("Pay with Card", html)
        self.assertNotIn("Charge Card", html)
        self.assertNotIn("pay-card", html)
        self.assertIn("Mark Paid", html)

    def test_the_pay_with_card_route_is_gone(self):
        c = self.login("shop_admin")
        r = c.post(f"/shop/billing/{self.project}/pay-card", data={"card_number": "4242424242424242"})
        self.assertEqual(r.status_code, 404)
        row = self.q1("SELECT payment_status FROM projects WHERE id = ?", (self.project,))
        self.assertEqual(row["payment_status"], "invoiced")

    def test_an_old_simulated_card_payment_stays_readable_and_is_labelled(self):
        self.exec("""UPDATE projects SET payment_status = 'paid', paid_at = ?, paid_by = 'Shop Admin',
                     paid_method = 'Card', card_last4 = '4242', card_charge_id = 'sim_ch_x' WHERE id = ?""",
                  (db.now_iso(), self.project))
        html = self.login("shop_admin").get("/shop/billing?period=all").get_data(as_text=True)
        self.assertIn("(simulated) ****4242", html)
