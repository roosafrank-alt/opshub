"""New feature feat-shop-job-payments: Billing tracks whether a job has been
invoiced and paid, with an "Owed to the shop" total for anything invoiced but
not yet paid, and a warning on Mark Completed for a job that isn't paid off
yet.
"""
from datetime import timedelta
from harness import OpsHubTestCase, seed_row
import db


class ShopJobPaymentsTest(OpsHubTestCase):
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

    def project_row(self):
        return self.q1("SELECT * FROM projects WHERE id = ?", (self.project,))

    def test_new_project_defaults_to_not_invoiced(self):
        self.assertIn(self.project_row()["payment_status"], (None, "not_invoiced"))
        html = self.login("shop_admin").get("/shop/billing?period=all").get_data(as_text=True)
        self.assertIn("Not invoiced", html)
        self.assertIn("Mark Invoiced", html)

    def test_mark_invoiced_then_paid(self):
        c = self.login("shop_admin")
        c.post(f"/shop/billing/{self.project}/mark-invoiced", follow_redirects=True)
        row = self.project_row()
        self.assertEqual(row["payment_status"], "invoiced")
        self.assertTrue(row["invoiced_at"])
        self.assertEqual(row["invoiced_by"], "Shop Admin")

        # No method picked -> refused, stays invoiced.
        c.post(f"/shop/billing/{self.project}/mark-paid", data={}, follow_redirects=True)
        self.assertEqual(self.project_row()["payment_status"], "invoiced")

        c.post(f"/shop/billing/{self.project}/mark-paid", data={"paid_method": "Check"}, follow_redirects=True)
        row = self.project_row()
        self.assertEqual(row["payment_status"], "paid")
        self.assertEqual(row["paid_method"], "Check")
        self.assertTrue(row["paid_at"])

        html = c.get("/shop/billing?period=all").get_data(as_text=True)
        self.assertIn("Paid", html)
        self.assertIn("Check", html)
        # No action left on this row once it's paid (the modal itself always
        # mentions "Mark Paid" in its title/button, so check the row's own
        # trigger is gone, not the phrase anywhere on the page).
        self.assertNotIn(f"/shop/billing/{self.project}/mark-paid", html)

    def test_a_tech_cannot_mark_invoiced_or_paid(self):
        c = self.login("tech")
        c.post(f"/shop/billing/{self.project}/mark-invoiced", follow_redirects=True)
        self.assertIn(self.project_row()["payment_status"], (None, "not_invoiced"))

    def test_owed_to_the_shop_counts_invoiced_not_paid(self):
        c = self.login("shop_admin")
        c.post(f"/shop/billing/{self.project}/mark-invoiced", follow_redirects=True)
        html = c.get("/shop/billing?period=all").get_data(as_text=True)
        self.assertIn("Owed to the shop", html)
        self.assertIn("$170.00", html)
        self.assertIn("1 job", html)
        # Paying it off drops it out of "owed".
        c.post(f"/shop/billing/{self.project}/mark-paid", data={"paid_method": "Cash"}, follow_redirects=True)
        html = c.get("/shop/billing?period=all").get_data(as_text=True)
        self.assertNotIn("Owed to the shop", html)

    def test_not_invoiced_job_is_not_counted_as_owed(self):
        # Active work with nothing billed yet isn't "owed" - only something
        # that's actually been invoiced and is still waiting on payment.
        c = self.login("shop_admin")
        html = c.get("/shop/billing?period=all").get_data(as_text=True)
        self.assertNotIn("Owed to the shop", html)

    def test_over_30_days_counted_separately(self):
        c = self.login("shop_admin")
        c.post(f"/shop/billing/{self.project}/mark-invoiced", follow_redirects=True)
        old = (__import__("datetime").datetime.now() - timedelta(days=36)).isoformat()
        self.exec("UPDATE projects SET invoiced_at = ? WHERE id = ?", (old, self.project))
        html = c.get("/shop/billing?period=all").get_data(as_text=True)
        self.assertIn("1 over 30 days", html)

    def test_unpaid_only_filter_excludes_paid_jobs(self):
        other = self.make_project(name="Oil Change - N999")
        self.exec("""INSERT INTO labor_sessions (laborer_id, project_id, started_at, ended_at, hours, rate, cost, created_at)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (self.laborer, other, db.now_iso(), db.now_iso(), 1.0, 85, 85.0, db.now_iso()))
        self.exec("UPDATE projects SET payment_status = 'paid', paid_at = ?, paid_method = 'Cash' WHERE id = ?",
                  (db.now_iso(), other))
        c = self.login("shop_admin")
        html = c.get("/shop/billing?period=all&unpaid=1").get_data(as_text=True)
        self.assertIn("Annual", html)
        self.assertNotIn("Oil Change - N999", html)

    def test_mark_completed_warns_about_unpaid_amount(self):
        html = self.login("shop_admin").get(f"/projects/{self.project}").get_data(as_text=True)
        self.assertIn("hasn&#39;t been paid yet ($170.00 owed)", html)

    def test_mark_completed_confirm_is_plain_once_paid(self):
        c = self.login("shop_admin")
        c.post(f"/shop/billing/{self.project}/mark-invoiced", follow_redirects=True)
        c.post(f"/shop/billing/{self.project}/mark-paid", data={"paid_method": "Cash"}, follow_redirects=True)
        html = c.get(f"/projects/{self.project}").get_data(as_text=True)
        self.assertNotIn("hasn&#39;t been paid yet", html)
        self.assertIn("Mark this project as completed?", html)
