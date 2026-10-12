"""Cash / check collected by a non-admin (CFI with billing) posts as Paid right
away, exactly as before, and is also logged for the office to cross-check:
Admin > Cash/check collected ("Received at office" / "Not received"). Verifying
or flagging never posts or reverses a payment. Admin-entered cash/check needs
no log row."""
from datetime import date
from unittest import mock

import db
import flight
from harness import seed_row
from test_wave_invoicing import WaveTestBase

NEW_PAGE = "/flight/billing/cash-check"


class _Base(WaveTestBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(flight, "_in_background", lambda fn: None)
        p.start()
        self.addCleanup(p.stop)
        self.suid = self.users["flight_student"]["id"]
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.suid,))["id"]
        self.exec("UPDATE students SET plane_rate_override = 150 WHERE id = ?", (self.student,))
        # A Flight Admin only reaches Billing when billing is also ticked for the account.
        self.exec("UPDATE users SET can_bill = 1 WHERE id = ?", (self.users["flight_admin"]["id"],))
        self.exec("UPDATE users SET can_bill = 1 WHERE id = ?", (self.users["flight_admin_cfi"]["id"],))
        self.asset = self.make_asset("N321CC")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.asset,))

    def running(self, role="master"):
        row = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users[role]["id"],))
        conn = db.get_db()
        fid = seed_row(conn, "flights", cfi_id=row["id"] if row else None, student_id=self.student, asset_id=self.asset,
                       flight_date=date.today().isoformat(), solo=1, started_at="2026-09-28 09:00:00",
                       stopped_at="2026-09-28 10:00:00", hobbs_start=100.0, tach_start=90.0)
        conn.commit()
        conn.close()
        return fid

    def ended(self):
        fid = self.running()
        self.login("flight_student").post(f"/flight/log/{fid}/end", data=dict(hobbs_end="101.0", tach_end="90.8"))
        return fid

    def paid(self, fid):
        return self.q1("SELECT paid FROM flights WHERE id = ?", (fid,))["paid"]

    def balance(self):
        return self.q1("SELECT balance FROM students WHERE id = ?", (self.student,))["balance"]

    def pay(self, role, fid, method="Cash", **extra):
        data = dict(paid_method=method, note="n1")
        data.update(extra)
        return self.login(role).post(f"/flight/log/{fid}/toggle_paid", data=data)

    def rows(self):
        return self.q("SELECT * FROM flight_payment_confirmations ORDER BY id")

    def payments(self, fid):
        return self.q("SELECT * FROM student_ledger WHERE flight_id = ? AND entry_type = 'payment'", (fid,))


class CashCheckLogTest(_Base):
    # ----- recording is immediate, plus a log row ---------------------------
    def test_cfi_cash_posts_immediately_and_is_logged(self):
        fid = self.ended()
        bal = self.balance()
        self.pay("cfi_billing", fid, "Cash")
        self.assertEqual(self.paid(fid), 1)
        self.assertEqual(len(self.payments(fid)), 1)
        self.assertAlmostEqual(self.balance(), bal + 150.0, places=2)
        (row,) = self.rows()
        self.assertEqual((row["status"], row["method"], row["note"], row["flight_id"]), ("pending", "Cash", "n1", fid))
        self.assertEqual(row["recorded_by"], "Cfi Billing")
        self.assertAlmostEqual(row["amount"], 150.0, places=2)
        self.assertTrue(row["recorded_at"])
        self.assertTrue(self.q("SELECT 1 FROM field_change_log WHERE entity_type = 'flight_payment_confirmation'"))

    def test_check_logged_case_insensitive(self):
        fid = self.ended()
        self.pay("cfi_billing", fid, "check")
        self.assertEqual(self.paid(fid), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_other_methods_and_admins_make_no_row(self):
        for role, method in (("cfi_billing", "Card"), ("cfi_billing", "Wave"), ("cfi_billing", "Venmo/Zelle"),
                             ("cfi_billing", ""), ("master", "Cash"), ("flight_admin", "Check"),
                             ("flight_admin_cfi", "Cash")):
            fid = self.ended()
            self.pay(role, fid, method)
            self.assertEqual(self.paid(fid), 1, (role, method))
        self.assertEqual(self.rows(), [])

    def test_mark_paid_button_not_hidden_and_no_awaiting_state(self):
        fid = self.ended()
        self.pay("cfi_billing", fid, "Cash")
        c = self.login("cfi_billing")
        for url in ("/flight/billing?status=all", f"/flight/log/{fid}", "/flight/log"):
            self.assertNotIn("awaiting confirmation", c.get(url).get_data(as_text=True), url)
        self.assertNotIn("awaiting confirmation", self.login("flight_student").get("/flight/my-account").get_data(as_text=True))

    # ----- batch / add funds -------------------------------------------------
    def test_batch_pays_all_and_logs_each(self):
        a, b = self.ended(), self.ended()
        r = self.login("cfi_billing").post("/flight/billing/mark-selected-paid", data=dict(ids=f"{a},{b}", paid_method="Cash"),
                                           follow_redirects=True)
        self.assertEqual((self.paid(a), self.paid(b)), (1, 1))
        self.assertIn("Marked 2 flights", r.get_data(as_text=True))
        self.assertEqual(len(self.rows()), 2)

    def test_batch_by_admin_makes_no_rows(self):
        a = self.ended()
        self.login("master").post("/flight/billing/mark-selected-paid", data=dict(ids=str(a), paid_method="Cash"))
        self.assertEqual(self.paid(a), 1)
        self.assertEqual(self.rows(), [])

    def test_add_funds_with_flights_pays_and_logs(self):
        fid = self.ended()
        bal = self.balance()
        self.login("cfi_billing").post(f"/flight/students/{self.student}/add_funds",
                                       data=dict(amount="200", paid_method="Cash", apply_flight_ids=[str(fid)]))
        self.assertEqual(self.paid(fid), 1)
        self.assertAlmostEqual(self.balance(), bal + 200.0, places=2)
        (row,) = self.rows()
        self.assertAlmostEqual(row["amount"], 150.0, places=2)

    def test_add_funds_by_admin_no_row(self):
        fid = self.ended()
        self.login("flight_admin").post(f"/flight/students/{self.student}/add_funds",
                                        data=dict(amount="150", paid_method="Cash", apply_flight_ids=[str(fid)]))
        self.assertEqual(self.paid(fid), 1)
        self.assertEqual(self.rows(), [])

    # ----- admin page and permissions --------------------------------------
    def test_only_admins_see_page_and_badge(self):
        fid = self.ended()
        self.pay("cfi_billing", fid, "Cash")
        cid = self.rows()[0]["id"]
        for role in ("cfi", "cfi_billing", "shop_admin", "flight_student"):
            c = self.login(role)
            self.assertEqual(c.get(NEW_PAGE).status_code, 302, role)
            c.post(f"{NEW_PAGE}/{cid}/verify")
            c.post(f"{NEW_PAGE}/{cid}/flag", data=dict(reason="x"))
        self.assertEqual(self.rows()[0]["status"], "pending")
        for role in ("master", "flight_admin"):
            html = self.login(role).get(NEW_PAGE).get_data(as_text=True)
            for want in ("Cash/check collected", "Student", "$150.00", "Cfi Billing", "Received at office", "Not received"):
                self.assertIn(want, html, (role, want))
            self.assertIn("Cash/check collected", self.login(role).get("/flight/dashboard").get_data(as_text=True))
        self.assertNotIn("Cash/check collected", self.login("cfi_billing").get("/flight/dashboard").get_data(as_text=True))

    def test_running_total_per_collector_and_sort(self):
        for _ in range(2):
            self.pay("cfi_billing", self.ended(), "Cash")
        html = self.login("master").get(NEW_PAGE).get_data(as_text=True)
        self.assertIn("$300.00", html)
        html = self.login("master").get(NEW_PAGE + "?sort=date").get_data(as_text=True)
        self.assertIn("$300.00", html)

    def test_admins_notified_on_new_entry_not_for_admin_entry(self):
        fid = self.ended()
        with mock.patch.object(flight.push, "queue_and_push") as qp:
            self.pay("cfi_billing", fid, "Cash")
        ids = {c.args[1] for c in qp.call_args_list}
        self.assertIn(self.users["master"]["id"], ids)
        self.assertIn(self.users["flight_admin"]["id"], ids)
        self.assertNotIn(self.users["cfi"]["id"], ids)
        fid2 = self.ended()
        with mock.patch.object(flight.push, "queue_and_push") as qp:
            self.pay("master", fid2, "Cash")
        self.assertEqual(qp.call_count, 0)

    # ----- verify / flag -----------------------------------------------------
    def test_verify_marks_verified_and_posts_nothing(self):
        fid = self.ended()
        self.pay("cfi_billing", fid, "Cash")
        cid = self.rows()[0]["id"]
        bal, ledger = self.balance(), len(self.q("SELECT id FROM student_ledger"))
        c = self.login("flight_admin")
        c.post(f"{NEW_PAGE}/{cid}/verify")
        c.post(f"{NEW_PAGE}/{cid}/verify")
        row = self.rows()[0]
        self.assertEqual((row["status"], row["decided_by"]), ("verified", "Flight Admin"))
        self.assertTrue(row["decided_at"])
        self.assertEqual(len(self.q("SELECT id FROM student_ledger")), ledger)
        self.assertAlmostEqual(self.balance(), bal, places=2)
        self.assertEqual(self.paid(fid), 1)
        self.assertIn("Office verified", c.get(NEW_PAGE).get_data(as_text=True))
        self.assertEqual(len(self.payments(fid)), 1)

    def test_flag_needs_note_keeps_payment_and_lists_in_flagged(self):
        fid = self.ended()
        self.pay("cfi_billing", fid, "Check")
        cid = self.rows()[0]["id"]
        c = self.login("master")
        c.post(f"{NEW_PAGE}/{cid}/flag", data=dict(reason=" "))
        self.assertEqual(self.rows()[0]["status"], "pending")
        with mock.patch.object(flight.push, "queue_and_push") as qp:
            c.post(f"{NEW_PAGE}/{cid}/flag", data=dict(reason="Check never arrived"))
        row = self.rows()[0]
        self.assertEqual((row["status"], row["reject_reason"]), ("missing", "Check never arrived"))
        self.assertEqual(self.paid(fid), 1)  # not reversed
        self.assertEqual(len(self.payments(fid)), 1)
        self.assertIn(self.users["flight_admin"]["id"], {x.args[1] for x in qp.call_args_list})
        html = c.get(NEW_PAGE).get_data(as_text=True)
        self.assertIn("Flagged - not received", html)
        self.assertIn("Check never arrived", html)
        # no longer counted in the red unverified number, and can later be verified
        conn = db.get_db()
        self.assertEqual(flight.flight_payments.pending_confirmation_count(conn), 0)
        conn.close()
        c.post(f"{NEW_PAGE}/{cid}/verify")
        self.assertEqual(self.rows()[0]["status"], "verified")

    # ----- badge for billers only ---------------------------------------------
    def test_office_badge_for_billers_not_students(self):
        fid = self.ended()
        self.pay("cfi_billing", fid, "Cash")
        c = self.login("cfi_billing")
        for url in ("/flight/billing?status=all", f"/flight/log/{fid}", "/flight/log"):
            self.assertIn("Not yet verified", c.get(url).get_data(as_text=True), url)
        for url in ("/flight/my-account", f"/flight/log/{fid}", "/flight/log"):
            html = self.login("flight_student").get(url).get_data(as_text=True)
            self.assertNotIn("Not yet verified", html, url)
            self.assertNotIn("Office verified", html, url)
        self.login("master").post(f"{NEW_PAGE}/{self.rows()[0]['id']}/verify")
        self.assertIn("Office verified", c.get("/flight/billing?status=all").get_data(as_text=True))

    # ----- delete / reset ----------------------------------------------------
    def test_flight_delete_and_reset_remove_rows(self):
        a, b = self.ended(), self.ended()
        self.pay("cfi_billing", a, "Cash")
        self.pay("cfi_billing", b, "Cash")
        r = self.login("master").post(f"/flight/log/{a}/delete", follow_redirects=True)
        self.assertNotIn("FOREIGN KEY", r.get_data(as_text=True))
        r = self.login("master").post("/admin/reset/flights", follow_redirects=True)
        self.assertNotIn("FOREIGN KEY", r.get_data(as_text=True))
        self.assertEqual(self.rows(), [])


class CashCheckAtFlightTest(_Base):
    """End Session and Log a Past Session post as before and log cash/check from non-admins."""

    def end_as(self, role, method, amount="150", **extra):
        fid = self.running(role)
        data = dict(hobbs_end="101.0", tach_end="90.8", paid="1", payment_method=method, payment_amount=amount)
        data.update(extra)
        self.login(role).post(f"/flight/log/{fid}/end", data=data)
        return fid

    def test_end_session_cash_by_cfi_paid_and_logged(self):
        for role in ("cfi_billing", "cfi"):
            fid = self.end_as(role, "Cash")
            f = self.q1("SELECT paid, ended_at FROM flights WHERE id = ?", (fid,))
            self.assertTrue(f["ended_at"], role)
            self.assertEqual(f["paid"], 1, role)
            self.assertEqual(len(self.payments(fid)), 1, role)
            row = self.q1("SELECT * FROM flight_payment_confirmations WHERE flight_id = ?", (fid,))
            self.assertEqual((row["status"], row["method"]), ("pending", "Cash"))
            self.assertAlmostEqual(row["amount"], 150.0, places=2)

    def test_end_session_admin_card_credit_make_no_row(self):
        self.assertEqual(self.paid(self.end_as("master", "Cash")), 1)
        self.assertEqual(self.paid(self.end_as("cfi_billing", "Card", card_number="4242424242424242")), 1)
        self.assertEqual(self.rows(), [])

    def test_end_session_short_cash_still_refused(self):
        fid = self.end_as("cfi_billing", "Cash", amount="10")
        self.assertIsNone(self.q1("SELECT ended_at FROM flights WHERE id = ?", (fid,))["ended_at"])
        self.assertEqual(self.rows(), [])

    def test_past_session_cash_paid_and_logged_admin_not(self):
        cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi_billing"]["id"],))["id"]
        form = dict(asset_id=self.asset, student_id=self.student, cfi_id=cfi_id, flight_date=date.today().isoformat(),
                    hobbs_end="101.0", tach_end="90.8", paid="1", payment_method="Cash", payment_amount="150")
        self.exec("UPDATE assets SET hobbs_hours = 100, tach_hours = 90 WHERE id = ?", (self.asset,))
        self.login("cfi_billing").post("/flight/log/new", data=form)
        fid = self.q1("SELECT id FROM flights ORDER BY id DESC")["id"]
        self.assertEqual(self.paid(fid), 1)
        self.assertEqual(len(self.payments(fid)), 1)
        self.assertEqual(self.q1("SELECT status FROM flight_payment_confirmations WHERE flight_id = ?", (fid,))["status"], "pending")
        self.exec("UPDATE assets SET hobbs_hours = 100, tach_hours = 90 WHERE id = ?", (self.asset,))
        self.login("master").post("/flight/log/new", data=form)
        fid2 = self.q1("SELECT id FROM flights ORDER BY id DESC")["id"]
        self.assertNotEqual(fid, fid2)
        self.assertEqual(self.paid(fid2), 1)
        self.assertEqual(len(self.rows()), 1)
