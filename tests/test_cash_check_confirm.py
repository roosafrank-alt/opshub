"""Cash / check taken by someone who is not a master admin or Flight Admin is
"Received - awaiting confirmation": the flight stays Unpaid, nothing posts, and
an admin confirms (one tap) or rejects (with a reason). Admins recording
cash/check, and other methods, behave as before."""
from datetime import date
from unittest import mock

import db
import flight
import flight_payments
from harness import seed_row
from test_wave_invoicing import WaveTestBase

AWAIT = "Received - awaiting confirmation"


class CashCheckConfirmTest(WaveTestBase):
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

    def ended(self):
        conn = db.get_db()
        fid = seed_row(conn, "flights", student_id=self.student, asset_id=self.asset, flight_date=date.today().isoformat(),
                       solo=1, started_at="2026-09-28 09:00:00", stopped_at="2026-09-28 10:00:00",
                       hobbs_start=100.0, tach_start=90.0)
        conn.commit()
        conn.close()
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

    def pending(self):
        return self.q("SELECT * FROM flight_payment_confirmations ORDER BY id")

    # ----- who is held -----------------------------------------------------
    def test_cfi_billing_cash_is_held_not_paid(self):
        fid = self.ended()
        bal = self.balance()
        ledger = len(self.q("SELECT id FROM student_ledger"))
        self.pay("cfi_billing", fid, "Cash")
        self.assertEqual(self.paid(fid), 0)
        self.assertEqual(self.balance(), bal)
        self.assertEqual(len(self.q("SELECT id FROM student_ledger")), ledger)
        (row,) = self.pending()
        self.assertEqual((row["status"], row["method"], row["note"], row["flight_id"]), ("pending", "Cash", "n1", fid))
        self.assertEqual(row["recorded_by"], "Cfi Billing")
        self.assertAlmostEqual(row["amount"], 150.0, places=2)
        self.assertTrue(self.q("SELECT 1 FROM field_change_log WHERE entity_type = 'flight_payment_confirmation'"))

    def test_check_is_held_and_method_match_ignores_case(self):
        fid = self.ended()
        self.pay("cfi_billing", fid, "check")
        self.assertEqual(self.paid(fid), 0)
        self.assertEqual(len(self.pending()), 1)

    def test_other_methods_still_paid_immediately(self):
        for method in ("Card", "Wave", "Venmo/Zelle", "Other", ""):
            fid = self.ended()
            self.pay("cfi_billing", fid, method)
            self.assertEqual(self.paid(fid), 1, method)
        self.assertEqual(self.pending(), [])

    def test_admins_cash_and_check_paid_immediately(self):
        for role, method in (("master", "Cash"), ("flight_admin", "Check"), ("flight_admin_cfi", "Cash")):
            fid = self.ended()
            self.pay(role, fid, method)
            self.assertEqual(self.paid(fid), 1, role)
        self.assertEqual(self.pending(), [])

    def test_second_pending_on_same_flight_refused(self):
        fid = self.ended()
        self.pay("cfi_billing", fid, "Cash")
        self.pay("cfi_billing", fid, "Check")
        self.assertEqual(len(self.pending()), 1)

    # ----- batch -----------------------------------------------------------
    def test_batch_reports_paid_vs_held(self):
        a, b = self.ended(), self.ended()
        c = self.login("cfi_billing")
        r = c.post("/flight/billing/mark-selected-paid", data=dict(ids=f"{a},{b}", paid_method="Cash"), follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertEqual((self.paid(a), self.paid(b)), (0, 0))
        self.assertIn("2 flights", html)
        self.assertIn("awaiting confirmation", html)
        self.assertEqual(len(self.pending()), 2)
        # second try: already awaiting, left alone
        r = c.post("/flight/billing/mark-selected-paid", data=dict(ids=f"{a}", paid_method="Cash"), follow_redirects=True)
        self.assertEqual(len(self.pending()), 2)

    def test_batch_card_pays_and_cash_mix_per_flight(self):
        a, b = self.ended(), self.ended()
        self.pay("cfi_billing", b, "Cash")  # b already awaiting
        c = self.login("cfi_billing")
        c.post("/flight/billing/mark-selected-paid", data=dict(ids=f"{a},{b}", paid_method="Card"))
        self.assertEqual((self.paid(a), self.paid(b)), (1, 1))  # Card is not a held method
        c = self.login("master")
        a2, b2 = self.ended(), self.ended()
        r = c.post("/flight/billing/mark-selected-paid", data=dict(ids=f"{a2},{b2}", paid_method="Cash"), follow_redirects=True)
        self.assertEqual((self.paid(a2), self.paid(b2)), (1, 1))
        self.assertIn("Marked 2 flights", r.get_data(as_text=True))

    # ----- add funds -------------------------------------------------------
    def test_add_funds_apply_cash_held_for_non_admin(self):
        fid = self.ended()
        bal = self.balance()
        self.login("cfi_billing").post(f"/flight/students/{self.student}/add_funds",
                                       data=dict(amount="200", paid_method="Cash", apply_flight_ids=[str(fid)]))
        self.assertEqual(self.paid(fid), 0)
        self.assertEqual(len(self.pending()), 1)
        self.assertAlmostEqual(self.balance(), bal + 50.0, places=2)  # only the leftover posts as funds

    def test_add_funds_apply_cash_paid_for_admin(self):
        fid = self.ended()
        self.login("flight_admin").post(f"/flight/students/{self.student}/add_funds",
                                        data=dict(amount="150", paid_method="Cash", apply_flight_ids=[str(fid)]))
        self.assertEqual(self.paid(fid), 1)
        self.assertEqual(self.pending(), [])

    # ----- queue page and permissions --------------------------------------
    def test_only_admins_see_queue_and_badge(self):
        fid = self.ended()
        self.pay("cfi_billing", fid, "Cash")
        cid = self.pending()[0]["id"]
        for role in ("cfi", "cfi_billing", "shop_admin", "flight_student"):
            c = self.login(role)
            self.assertEqual(c.get("/flight/billing/cash-check").status_code, 302, role)
            c.post(f"/flight/billing/cash-check/{cid}/confirm")
            c.post(f"/flight/billing/cash-check/{cid}/reject", data=dict(reason="x"))
        self.assertEqual(self.pending()[0]["status"], "pending")
        self.assertEqual(self.paid(fid), 0)
        for role in ("master", "flight_admin"):
            html = self.login(role).get("/flight/billing/cash-check").get_data(as_text=True)
            self.assertIn("Student", html)
            self.assertIn("$150.00", html)
            self.assertIn("Cfi Billing", html)
            self.assertIn("Confirm", html)
            dash = self.login(role).get("/flight/dashboard").get_data(as_text=True)
            self.assertIn("Cash/check to confirm", dash)
        self.assertNotIn("Cash/check to confirm", self.login("cfi_billing").get("/flight/dashboard").get_data(as_text=True))

    def test_admins_notified(self):
        fid = self.ended()
        with mock.patch.object(flight.push, "queue_and_push") as qp:
            self.pay("cfi_billing", fid, "Cash")
        ids = {c.args[1] for c in qp.call_args_list}
        self.assertIn(self.users["master"]["id"], ids)
        self.assertIn(self.users["flight_admin"]["id"], ids)
        self.assertNotIn(self.users["cfi"]["id"], ids)

    # ----- confirm / reject --------------------------------------------------
    def test_confirm_marks_paid_and_posts_once(self):
        fid = self.ended()
        bal = self.balance()
        self.pay("cfi_billing", fid, "Cash")
        cid = self.pending()[0]["id"]
        c = self.login("flight_admin")
        c.post(f"/flight/billing/cash-check/{cid}/confirm")
        c.post(f"/flight/billing/cash-check/{cid}/confirm")  # double tap
        row = self.pending()[0]
        self.assertEqual((row["status"], row["decided_by"]), ("confirmed", "Flight Admin"))
        f = self.q1("SELECT paid, payment_method FROM flights WHERE id = ?", (fid,))
        self.assertEqual((f["paid"], f["payment_method"]), (1, "Cash"))
        pays = self.q("SELECT * FROM student_ledger WHERE flight_id = ? AND entry_type = 'payment'", (fid,))
        self.assertEqual(len(pays), 1)
        self.assertEqual(pays[0]["created_by"], "Flight Admin")
        self.assertIn("n1", pays[0]["note"])
        self.assertAlmostEqual(self.balance(), bal + 150.0, places=2)
        self.assertGreaterEqual(len(self.q("SELECT 1 FROM field_change_log WHERE entity_type = 'flight_payment_confirmation'")), 2)
        html = c.get("/flight/billing/cash-check").get_data(as_text=True)
        self.assertIn("Recently handled", html)
        self.assertIn("Confirmed", html)

    def test_reject_needs_reason_keeps_unpaid_and_tells_recorder(self):
        fid = self.ended()
        self.pay("cfi_billing", fid, "Check")
        cid = self.pending()[0]["id"]
        c = self.login("master")
        c.post(f"/flight/billing/cash-check/{cid}/reject", data=dict(reason=" "))
        self.assertEqual(self.pending()[0]["status"], "pending")
        with mock.patch.object(flight.push, "queue_and_push") as qp:
            c.post(f"/flight/billing/cash-check/{cid}/reject", data=dict(reason="Check bounced"))
        row = self.pending()[0]
        self.assertEqual((row["status"], row["reject_reason"]), ("rejected", "Check bounced"))
        self.assertEqual(self.paid(fid), 0)
        self.assertIn(self.users["cfi_billing"]["id"], {x.args[1] for x in qp.call_args_list})
        rec = self.login("cfi_billing")
        html = rec.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Check bounced", html)
        rec.post("/flight/billing/cash-check/seen")
        self.assertNotIn("Check bounced", rec.get("/flight/dashboard").get_data(as_text=True))
        # after a rejection a new entry can be recorded
        self.pay("cfi_billing", fid, "Cash")
        self.assertEqual(len(self.pending()), 2)

    def test_confirm_when_flight_paid_another_way_posts_nothing_extra(self):
        fid = self.ended()
        self.pay("cfi_billing", fid, "Cash")
        self.pay("master", fid, "Card")
        before = len(self.q("SELECT id FROM student_ledger"))
        self.login("master").post(f"/flight/billing/cash-check/{self.pending()[0]['id']}/confirm")
        self.assertEqual(len(self.q("SELECT id FROM student_ledger")), before)

    # ----- badges ------------------------------------------------------------
    def test_badge_shown_to_billers_and_student(self):
        fid = self.ended()
        self.pay("cfi_billing", fid, "Cash")
        for url in ("/flight/billing", f"/flight/log/{fid}", "/flight/log"):
            html = self.login("cfi_billing").get(url).get_data(as_text=True)
            self.assertIn(AWAIT, html, url)
        html = self.login("flight_student").get("/flight/my-account").get_data(as_text=True)
        self.assertIn(AWAIT, html)
        self.assertIn("$150.00", html)  # still owed
        # gone once confirmed
        self.login("master").post(f"/flight/billing/cash-check/{self.pending()[0]['id']}/confirm")
        self.assertNotIn(AWAIT, self.login("cfi_billing").get("/flight/billing?status=all").get_data(as_text=True))

    # ----- delete / reset ----------------------------------------------------
    def test_flight_delete_and_reset_remove_pending_rows(self):
        a, b = self.ended(), self.ended()
        self.pay("cfi_billing", a, "Cash")
        self.pay("cfi_billing", b, "Cash")
        r = self.login("master").post(f"/flight/log/{a}/delete", follow_redirects=True)
        self.assertNotIn("FOREIGN KEY", r.get_data(as_text=True))
        self.assertIsNone(self.q1("SELECT id FROM flights WHERE id = ?", (a,)))
        r = self.login("master").post("/admin/reset/flights", follow_redirects=True)
        self.assertNotIn("FOREIGN KEY", r.get_data(as_text=True))
        self.assertEqual(self.pending(), [])
