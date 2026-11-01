"""QA feat-owner-pay-due: on My Aircraft, each job in Job Costs says Due or
Paid, a "You owe $X" line adds up the invoiced jobs, and one Pay button
(simulated card checkout) marks them Paid."""
from harness import OpsHubTestCase


class PortalOwnerPayDueTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N777PD")
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)",
                  (self.customer_id, self.asset))
        part = self.make_part(unit_cost=10, barcode="PD-1")  # sells at 13.00
        self.jobs = {}
        for key, status in (("due", "invoiced"), ("paid", "paid"), ("new", None)):
            pid = self.make_project(name="Job " + key, asset_id=self.asset)
            self.exec("INSERT INTO transactions (part_id, project_id, type, qty) VALUES (?,?,'out',2)", (part, pid))
            if status:
                self.exec("UPDATE projects SET payment_status = ? WHERE id = ?", (status, pid))
            self.jobs[key] = pid

    def _page(self, client):
        return client.get(f"/portal/aircraft/{self.asset}").get_data(as_text=True)

    def test_tags_and_you_owe(self):
        html = self._page(self.login("customer"))
        self.assertIn("You owe $26.00", html)
        self.assertIn(">Due<", html)
        self.assertIn("Paid</span>", html)
        self.assertIn("Pay $26.00", html)

    def test_pay_marks_paid_and_clears_owed(self):
        c = self.login("customer")
        r = c.post(f"/portal/aircraft/{self.asset}/pay", data={"card_number": "4242 4242 4242 1111"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT payment_status, paid_method, card_last4 FROM projects WHERE id = ?", (self.jobs["due"],))
        self.assertEqual((row["payment_status"], row["paid_method"], row["card_last4"]), ("paid", "Card", "1111"))
        new = self.q1("SELECT payment_status FROM projects WHERE id = ?", (self.jobs["new"],))
        self.assertIsNone(new["payment_status"])
        self.assertNotIn("You owe", self._page(c))

    def test_needs_card_number(self):
        c = self.login("customer")
        c.post(f"/portal/aircraft/{self.asset}/pay", data={"card_number": ""})
        row = self.q1("SELECT payment_status FROM projects WHERE id = ?", (self.jobs["due"],))
        self.assertEqual(row["payment_status"], "invoiced")

    def test_cannot_pay_someone_elses_plane(self):
        other = self.make_asset("N888XX")
        c = self.login("customer")
        self.assertEqual(c.post(f"/portal/aircraft/{other}/pay", data={"card_number": "4242"}).status_code, 404)
