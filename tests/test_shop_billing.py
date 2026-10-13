"""Shop Billing, Labor Pay and Stats: the dollar totals shown must match the
underlying parts transactions and clocked labor, and only the right people
may see or change them. Drives the real pages and reads the numbers off them.
"""
import re
from datetime import datetime, timedelta

from harness import OpsHubTestCase, seed_row, open_finding
import db


def stamp(days_ago=0, hour=12):
    d = datetime.now() - timedelta(days=days_ago)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


class BillingBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        # sell price = cost * 1.3 -> 10 * 1.3 = 13.00 per unit
        self.part = self.make_part(qty=100, unit_cost=10)
        self.project = self.make_project()
        self.other_project = self.make_project(name="Other job")
        conn = db.get_db()
        self.lab = seed_row(conn, "laborers", name="Tech", code="LABOR-AAAA0001", rate=40, active=1)
        conn.commit()
        conn.close()

    def txn(self, kind, qty, project=None, when=None):
        conn = db.get_db()
        seed_row(conn, "transactions", part_id=self.part, type=kind, qty=qty, project_id=project or self.project,
                 created_at=when or stamp(0))
        conn.commit()
        conn.close()

    def session(self, hours, cost, project="default", ended=True, when=None, laborer=None):
        pid = self.project if project == "default" else project
        start = when or stamp(0, 8)
        conn = db.get_db()
        seed_row(conn, "labor_sessions", laborer_id=laborer or self.lab, project_id=pid, started_at=start,
                 ended_at=start if ended else None, hours=hours if ended else None, rate=40,
                 cost=cost if ended else None)
        conn.commit()
        conn.close()

    def page(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.get_data(as_text=True)


class BillingTotalsTest(BillingBase):
    def setUp(self):
        super().setUp()
        self.login("shop_admin")

    def test_total_is_parts_at_sell_price_plus_labor(self):
        self.txn("out", 3)              # 3 x 13.00 = 39.00
        self.session(2, 80)             # 2h x 40
        html = self.page("/shop/billing")
        self.assertIn("Parts $39.00 + Labor $80.00", html)
        self.assertIn("$119.00", html)

    def test_returned_parts_are_taken_off(self):
        self.txn("out", 5)
        self.txn("in", 2)               # returned to the shelf
        self.assertIn("Parts $39.00 + Labor $0.00", self.page("/shop/billing"))

    def test_fully_returned_part_bills_nothing(self):
        self.txn("out", 2)
        self.txn("in", 2)
        html = self.page("/shop/billing")
        self.assertIn("Parts $0.00", html)

    def test_two_jobs_add_up_to_the_grand_total(self):
        self.txn("out", 1)              # 13.00 on job 1
        self.txn("out", 2, project=self.other_project)   # 26.00 on job 2
        self.session(1, 40)
        self.session(1.5, 60, project=self.other_project)
        self.assertIn("Parts $39.00 + Labor $100.00", self.page("/shop/billing"))

    def test_running_timer_is_not_billed(self):
        self.session(3, 120, ended=False)
        self.assertIn("Labor $0.00", self.page("/shop/billing"))

    def test_general_shop_time_is_not_billed_to_a_customer(self):
        self.session(2, 80, project=None)
        self.assertIn("Labor $0.00", self.page("/shop/billing"))

    def test_last_months_work_is_outside_this_month(self):
        self.txn("out", 4, when=stamp(45))
        self.session(2, 80, when=stamp(45))
        self.assertIn("Parts $0.00 + Labor $0.00", self.page("/shop/billing"))
        self.assertIn("Parts $52.00 + Labor $80.00", self.page("/shop/billing?period=all"))

    def test_custom_range_is_inclusive_on_both_ends(self):
        day = stamp(10)[:10]
        self.txn("out", 1, when=day + " 23:59:59")
        self.txn("out", 1, when=day + " 00:00:00")
        html = self.page(f"/shop/billing?from={day}&to={day}")
        self.assertIn("Parts $26.00", html)

    def test_garbage_dates_fall_back_to_the_default_period(self):
        self.txn("out", 1)
        self.assertIn("Parts $13.00", self.page("/shop/billing?from=banana&to=%27%3B--"))

    def test_reversed_dates_are_swapped(self):
        a, b = stamp(5)[:10], stamp(1)[:10]
        self.txn("out", 1, when=stamp(3))
        self.assertIn("Parts $13.00", self.page(f"/shop/billing?from={b}&to={a}"))


class StatsMatchBillingTest(BillingBase):
    def setUp(self):
        super().setUp()
        self.login("shop_admin")

    def test_stats_total_matches_billing_total(self):
        self.txn("out", 3)
        self.txn("in", 1)
        self.session(2, 80)
        stats = self.page("/shop/stats")
        billing = self.page("/shop/billing")
        self.assertIn("$26.00", stats)          # parts billed (2 x 13)
        self.assertIn("$80.00", stats)
        self.assertIn("$106.00", stats)         # total billed
        self.assertIn("Parts $26.00 + Labor $80.00", billing)

    def test_stats_hours_match_sessions(self):
        self.session(1.5, 60)
        self.session(2.25, 90)
        self.assertIn("3.8", self.page("/shop/stats"))      # 3.75 -> 1 decimal

    def test_general_shop_hours_count_in_stats_labor(self):
        self.session(2, 80, project=None)
        self.assertIn("$80.00", self.page("/shop/stats"))

    def test_empty_period_does_not_divide_by_zero(self):
        r = self.client.get("/shop/stats")
        self.assertEqual(r.status_code, 200)


class PayPageTest(BillingBase):
    def test_pay_total_is_sum_of_finished_sessions(self):
        self.login("shop_admin")
        self.session(2, 80)
        self.session(1, 40)
        self.session(5, 200, ended=False)
        html = self.page("/shop/pay")
        self.assertIn("$120.00", html)
        self.assertIn("3.0 hrs clocked", html)

    def test_worker_sees_only_own_pay(self):
        conn = db.get_db()
        other = seed_row(conn, "laborers", name="Joe", code="LABOR-BBBB0002", rate=25, active=1)
        conn.execute("UPDATE laborers SET user_id=? WHERE id=?", (self.users["tech"]["id"], self.lab))
        conn.commit()
        conn.close()
        self.session(2, 80)
        self.session(4, 100, laborer=other)
        self.login("tech")
        html = self.page("/shop/pay")
        self.assertIn("$80.00", html)
        self.assertNotIn("$100.00", html)
        self.assertNotIn("Joe", html)

    def test_worker_cannot_pick_someone_elses_pay_with_the_url(self):
        conn = db.get_db()
        other = seed_row(conn, "laborers", name="Joe", code="LABOR-BBBB0002", rate=25, active=1)
        conn.commit()
        conn.close()
        self.session(4, 100, laborer=other)
        self.login("tech")
        html = self.page(f"/shop/pay?laborer_id={other}")
        self.assertNotIn("Joe", html)
        self.assertNotIn("$100.00", html)

    def test_flight_only_accounts_have_no_shop_pay(self):
        for role in ("cfi", "flight_student", "no_roles"):
            self.login(role)
            r = self.client.get("/shop/pay")
            self.assertEqual(r.status_code, 302, role)


class BillingPermissionsTest(BillingBase):
    def test_only_admins_see_billing_and_stats(self):
        for role in ("tech", "inspector", "shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            for url in ("/shop/billing", "/shop/stats"):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 302, f"{role} {url}")

    def test_logged_out_is_sent_to_login(self):
        self.login(None)
        self.assertEqual(self.client.get("/shop/billing").status_code, 302)

    def test_non_admin_cannot_mark_paid(self):
        self.login("tech")
        self.client.post(f"/shop/billing/{self.project}/mark-paid", data=dict(paid_method="Cash"))
        self.assertNotEqual(self.q1("SELECT payment_status s FROM projects WHERE id=?", (self.project,))["s"], "paid")


class PaymentStatusTest(BillingBase):
    def setUp(self):
        super().setUp()
        self.login("shop_admin")

    def status(self):
        return self.q1("SELECT * FROM projects WHERE id=?", (self.project,))

    def test_invoice_then_pay_then_undo_twice(self):
        self.client.post(f"/shop/billing/{self.project}/mark-invoiced")
        self.assertEqual(self.status()["payment_status"], "invoiced")
        self.client.post(f"/shop/billing/{self.project}/mark-paid", data=dict(paid_method="Check"))
        row = self.status()
        self.assertEqual((row["payment_status"], row["paid_method"]), ("paid", "Check"))
        self.assertTrue(row["paid_at"])
        self.client.post(f"/shop/billing/{self.project}/undo-payment")
        row = self.status()
        self.assertEqual((row["payment_status"], row["paid_at"], row["paid_method"]), ("invoiced", None, None))
        self.client.post(f"/shop/billing/{self.project}/undo-payment")
        self.assertEqual(self.status()["payment_status"], "not_invoiced")
        self.client.post(f"/shop/billing/{self.project}/undo-payment")      # nothing left to undo
        self.assertEqual(self.status()["payment_status"], "not_invoiced")

    def test_mark_paid_needs_a_real_method(self):
        for bad in ("", "Bitcoin", "  "):
            self.client.post(f"/shop/billing/{self.project}/mark-paid", data=dict(paid_method=bad))
            self.assertNotEqual(self.status()["payment_status"], "paid", bad)

    def test_missing_job_is_404(self):
        for action in ("mark-invoiced", "mark-paid", "undo-payment"):
            r = self.client.post(f"/shop/billing/9999/{action}", data=dict(paid_method="Cash"))
            self.assertEqual(r.status_code, 404, action)

    def test_owed_total_counts_only_invoiced_jobs(self):
        self.txn("out", 2)                              # 26.00 on the main job
        self.txn("out", 1, project=self.other_project)  # 13.00, never invoiced
        self.client.post(f"/shop/billing/{self.project}/mark-invoiced")
        html = self.page("/shop/billing")
        self.assertIn("$26.00</div>", html)
        self.assertIn("1 job", html)
        self.client.post(f"/shop/billing/{self.project}/mark-paid", data=dict(paid_method="Cash"))
        self.assertNotIn("Owed to the shop", self.page("/shop/billing"))

    def test_paying_twice_keeps_the_first_payment_record(self):
        self.client.post(f"/shop/billing/{self.project}/mark-paid", data=dict(paid_method="Cash"))
        first = self.status()
        self.exec("UPDATE projects SET paid_at='2026-01-01 09:00:00' WHERE id=?", (self.project,))
        self.client.post(f"/shop/billing/{self.project}/mark-paid", data=dict(paid_method="Card"))
        row = self.status()
        self.assertEqual((row["paid_at"], row["paid_method"]), ("2026-01-01 09:00:00", "Cash"))
