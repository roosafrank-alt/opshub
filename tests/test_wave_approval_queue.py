"""FLY-07: a student asks to pay a flight by card through Wave; a master admin
or Flight Admin reviews the hours and taps "Accept & bill" (one POST), which
makes the Wave invoice. Wave is faked (FakeWave); no network."""
from datetime import date
from unittest import mock

import db
import flight
from harness import seed_row
from test_wave_invoicing import WaveTestBase

CONSENT = "I'd like an invoice for this flight to pay by card through Wave, once the school approves it."


class WaveApprovalQueueTest(WaveTestBase):
    def setUp(self):
        super().setUp()
        for target in ("_in_background",):
            p = mock.patch.object(flight, target, lambda fn: None)
            p.start()
            self.addCleanup(p.stop)
        self.suid = self.users["flight_student"]["id"]
        self.exec("UPDATE users SET email = 'stu@example.com' WHERE id = ?", (self.suid,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.suid,))["id"]
        self.exec("UPDATE students SET plane_rate_override = 150 WHERE id = ?", (self.student,))
        self.asset = self.make_asset("N321CC")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.asset,))

    def running(self, **kw):
        vals = dict(student_id=self.student, asset_id=self.asset, flight_date=date.today().isoformat(), solo=1,
                    started_at="2026-09-28 09:00:00", stopped_at="2026-09-28 10:00:00", hobbs_start=100.0, tach_start=90.0)
        vals.update(kw)
        conn = db.get_db()
        fid = seed_row(conn, "flights", **vals)
        conn.commit()
        conn.close()
        return fid

    def end(self, fid, **extra):
        form = dict(hobbs_end="101.5", tach_end="90.8")
        form.update(extra)
        return self.login("flight_student").post(f"/flight/log/{fid}/end", data=form)

    def ended_with_request(self):
        self.connect_wave()
        fid = self.running()
        self.end(fid, charge_choice="wave", consent="1")
        req = self.q1("SELECT * FROM flight_charge_requests WHERE flight_id = ?", (fid,))
        return fid, req

    # ----- student choice ---------------------------------------------------
    def test_no_choice_when_wave_not_connected(self):
        fid = self.running()
        html = self.login("flight_student").get("/flight/log/active").get_data(as_text=True)
        self.assertNotIn("Pay by card through Wave", html)
        self.assertIn("Email me a receipt", html)
        self.end(fid, charge_choice="wave", consent="1")
        self.assertEqual(self.q("SELECT id FROM flight_charge_requests"), [])

    def test_choice_shown_to_student_only(self):
        self.connect_wave()
        self.running()
        html = self.login("flight_student").get("/flight/log/active").get_data(as_text=True)
        self.assertIn("Pay by card through Wave", html)
        self.assertIn("I'll settle with the school", html)
        self.assertIn("Email me a receipt", html)
        html = self.login("master").get("/flight/log/active").get_data(as_text=True)
        self.assertNotIn("Pay by card through Wave", html)

    def test_request_needs_consent(self):
        self.connect_wave()
        fid = self.running()
        r = self.end(fid, charge_choice="wave")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.q1("SELECT ended_at FROM flights WHERE id = ?", (fid,))["ended_at"])
        self.assertEqual(self.q("SELECT id FROM flight_charge_requests"), [])

    def test_request_makes_no_wave_call_and_stays_unpaid(self):
        fid, req = self.ended_with_request()
        self.assertEqual(req["status"], "pending")
        self.assertEqual(req["consent_text"], CONSENT)
        self.assertGreater(req["amount_cents"], 0)
        self.assertEqual(self.wave.calls, [])
        f = self.q1("SELECT paid, wave_invoice_id, ended_at FROM flights WHERE id = ?", (fid,))
        self.assertEqual((f["paid"], f["wave_invoice_id"]), (0, None))
        self.assertTrue(f["ended_at"])
        self.assertTrue(self.q("SELECT * FROM field_change_log WHERE entity_type = 'flight_charge_request'"))

    def test_settle_with_school_makes_no_request(self):
        self.connect_wave()
        fid = self.running()
        self.end(fid, charge_choice="school")
        self.assertEqual(self.q("SELECT id FROM flight_charge_requests"), [])

    def test_request_later_from_my_account(self):
        self.connect_wave()
        fid = self.running()
        self.end(fid)
        c = self.login("flight_student")
        html = c.get("/flight/my-account").get_data(as_text=True)
        self.assertIn("Pay this flight by card through Wave", html)
        c.post(f"/flight/my-account/flight/{fid}/card-request", data={})
        self.assertEqual(self.q("SELECT id FROM flight_charge_requests"), [])  # no consent
        c.post(f"/flight/my-account/flight/{fid}/card-request", data={"consent": "1"})
        c.post(f"/flight/my-account/flight/{fid}/card-request", data={"consent": "1"})
        self.assertEqual(len(self.q("SELECT id FROM flight_charge_requests")), 1)
        self.assertEqual(self.wave.calls, [])

    def test_admins_notified_on_request(self):
        with mock.patch.object(flight.push, "queue_and_push") as qp:
            self.ended_with_request()
        ids = {c.args[1] for c in qp.call_args_list}
        self.assertIn(self.users["master"]["id"], ids)
        self.assertIn(self.users["flight_admin"]["id"], ids)
        self.assertNotIn(self.users["cfi"]["id"], ids)

    # ----- permissions --------------------------------------------------------
    def test_only_admins_can_use_queue(self):
        fid, req = self.ended_with_request()
        for role in ("cfi", "cfi_billing", "shop_admin", "flight_student"):
            c = self.login(role)
            self.assertEqual(c.get("/flight/billing/approvals").status_code, 302, role)
            c.post(f"/flight/billing/approvals/{req['id']}/approve")
            c.post(f"/flight/billing/approvals/{req['id']}/decline", data={"reason": "x"})
        self.assertEqual(self.q1("SELECT status FROM flight_charge_requests")["status"], "pending")
        self.assertEqual(self.wave.calls, [])
        for role in ("master", "flight_admin"):
            self.assertEqual(self.login(role).get("/flight/billing/approvals").status_code, 200)

    # ----- the queue row and one-tap approve ------------------------------------
    def test_row_shows_hours_and_total_and_accept_is_one_post(self):
        fid, req = self.ended_with_request()
        html = self.login("flight_admin").get("/flight/billing/approvals").get_data(as_text=True)
        due = req["amount_cents"] / 100
        self.assertIn("Accept &amp; bill", html)
        self.assertIn("1.5 hr", html)
        self.assertIn("$150.00/hr", html)
        self.assertIn(f"${due:,.2f}", html)
        self.assertIn("Flight Student", html)
        self.assertIn("View flight", html)
        self.assertIn("N321CC", html)
        r = self.login("flight_admin").post(f"/flight/billing/approvals/{req['id']}/approve")
        self.assertEqual(r.status_code, 302)
        r = self.client.get(r.headers["Location"])
        req2 = self.q1("SELECT * FROM flight_charge_requests WHERE id = ?", (req["id"],))
        self.assertEqual(req2["status"], "approved")
        self.assertEqual(req2["decided_by"], "Flight Admin")
        self.assertTrue(req2["wave_local_invoice_id"])
        self.assertEqual(len(self.wave.created_items()), 1)
        total = sum(float(i["quantity"]) * float(i["unitPrice"]) for i in self.wave.created_items()[0])
        self.assertAlmostEqual(total, due, places=2)
        f = self.q1("SELECT paid, wave_invoice_id FROM flights WHERE id = ?", (fid,))
        self.assertEqual(f["wave_invoice_id"], req2["wave_local_invoice_id"])
        self.assertEqual(f["paid"], 0)
        self.assertTrue(self.q1("SELECT sent_at FROM wave_invoices WHERE id = ?", (f["wave_invoice_id"],))["sent_at"])

    def test_double_approve_makes_one_invoice(self):
        fid, req = self.ended_with_request()
        c = self.login("master")
        c.post(f"/flight/billing/approvals/{req['id']}/approve")
        c.post(f"/flight/billing/approvals/{req['id']}/approve")
        self.assertEqual(len(self.wave.created_items()), 1)
        self.assertEqual(len(self.q("SELECT id FROM wave_invoices")), 1)

    def test_changed_amount_is_shown_and_billed(self):
        fid, req = self.ended_with_request()
        self.exec("UPDATE flights SET hobbs_end = 102.5 WHERE id = ?", (fid,))
        html = self.login("master").get("/flight/billing/approvals").get_data(as_text=True)
        self.assertIn("was $", html)
        self.login("master").post(f"/flight/billing/approvals/{req['id']}/approve")
        new_cents = self.q1("SELECT amount_cents FROM flight_charge_requests")["amount_cents"]
        self.assertGreater(new_cents, req["amount_cents"])
        self.assertAlmostEqual(sum(float(i["quantity"]) * float(i["unitPrice"]) for i in self.wave.created_items()[0]), new_cents / 100, places=2)

    def test_wave_failure_marks_failed_and_stays_unpaid(self):
        fid, req = self.ended_with_request()

        def boom(*a, **k):
            raise flight.wave_billing.WaveError("Wave refused the access token")
        c = self.login("master")
        with mock.patch.object(flight.wave_billing, "create_invoice", boom):
            r = c.post(f"/flight/billing/approvals/{req['id']}/approve")
        html = c.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Wave refused the access token", html)
        row = self.q1("SELECT * FROM flight_charge_requests")
        self.assertEqual(row["status"], "failed")
        self.assertIn("refused", row["failure_message"])
        f = self.q1("SELECT paid, wave_invoice_id FROM flights WHERE id = ?", (fid,))
        self.assertEqual((f["paid"], f["wave_invoice_id"]), (0, None))
        c.post(f"/flight/billing/approvals/{req['id']}/approve")  # no automatic or repeat retry
        self.assertEqual(self.wave.created_items(), [])

    def test_decline_needs_reason_stores_it_and_tells_student(self):
        fid, req = self.ended_with_request()
        c = self.login("master")
        c.post(f"/flight/billing/approvals/{req['id']}/decline", data={"reason": " "})
        self.assertEqual(self.q1("SELECT status FROM flight_charge_requests")["status"], "pending")
        c.post(f"/flight/billing/approvals/{req['id']}/decline", data={"reason": "Please see me first"})
        row = self.q1("SELECT * FROM flight_charge_requests")
        self.assertEqual((row["status"], row["decline_reason"]), ("declined", "Please see me first"))
        notes = [r["message"] for r in self.q("SELECT message FROM student_notifications")]
        self.assertTrue(any("Please see me first" in m for m in notes))
        self.assertEqual(self.wave.calls, [])

    def test_approve_refused_when_flight_already_paid(self):
        fid, req = self.ended_with_request()
        self.exec("UPDATE flights SET paid = 1 WHERE id = ?", (fid,))
        self.login("master").post(f"/flight/billing/approvals/{req['id']}/approve")
        self.assertEqual(self.wave.created_items(), [])
        self.assertEqual(self.q1("SELECT status FROM flight_charge_requests")["status"], "pending")

    def test_request_for_paid_flight_refused(self):
        self.connect_wave()
        fid = self.running()
        self.end(fid)
        self.exec("UPDATE flights SET paid = 1 WHERE id = ?", (fid,))
        self.login("flight_student").post(f"/flight/my-account/flight/{fid}/card-request", data={"consent": "1"})
        self.assertEqual(self.q("SELECT id FROM flight_charge_requests"), [])

    def test_student_cannot_approve_own(self):
        fid, req = self.ended_with_request()
        self.login("flight_student").post(f"/flight/billing/approvals/{req['id']}/approve")
        self.assertEqual(self.q1("SELECT status FROM flight_charge_requests")["status"], "pending")

    def test_badge_for_approvers_only(self):
        self.ended_with_request()
        html = self.login("flight_admin").get("/flight/").get_data(as_text=True)
        html = self.login("flight_admin").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Card requests", html)
        self.assertNotIn("Card requests", self.login("cfi_billing").get("/flight/dashboard").get_data(as_text=True))

    def test_flips_to_paid_through_existing_sync(self):
        fid, req = self.ended_with_request()
        self.login("master").post(f"/flight/billing/approvals/{req['id']}/approve")
        wid = self.q1("SELECT wave_invoice_id FROM wave_invoices")["wave_invoice_id"]
        self.wave.pay(wid)
        conn = db.get_db()
        flight.wave_billing.sync_open_invoices(conn, flight.wave_billing.apply_paid, program="flight")
        conn.close()
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id = ?", (fid,))["paid"], 1)


class ChargeRequestAutoClearTest(WaveApprovalQueueTest):
    """A pending card request clears itself when it is no longer needed."""

    def row(self):
        return self.q1("SELECT * FROM flight_charge_requests")

    def assert_cleared(self, why):
        r = self.row()
        self.assertEqual(r["status"], "not_needed")
        self.assertIn(why, r["decline_reason"])
        self.assertTrue(self.q("SELECT 1 FROM field_change_log WHERE entity_type = 'flight_charge_request' "
                               "AND new_value LIKE 'not_needed%'"))

    def test_paid_another_way_clears_on_queue_view_and_shows_in_recent(self):
        fid, req = self.ended_with_request()
        self.exec("UPDATE flights SET paid = 1 WHERE id = ?", (fid,))
        html = self.login("master").get("/flight/billing/approvals").get_data(as_text=True)
        self.assert_cleared("paid another way")
        self.assertIn("No requests waiting", html)
        self.assertIn("Not needed", html)
        self.assertIn("Flight was paid another way", html)

    def test_badge_and_count_drop_it(self):
        fid, req = self.ended_with_request()
        self.assertIn("badge bg-danger", self.login("flight_admin").get("/flight/dashboard").get_data(as_text=True))
        self.exec("UPDATE flights SET paid = 1 WHERE id = ?", (fid,))
        conn = db.get_db()
        self.assertEqual(flight._pending_charge_count(conn), 0)
        conn.commit()
        conn.close()
        self.assertEqual(self.row()["status"], "not_needed")

    def test_clears_right_after_manual_payment(self):
        fid, req = self.ended_with_request()
        self.login("master").post(f"/flight/log/{fid}/toggle_paid", data=dict(paid_method="Cash"))
        self.assert_cleared("paid another way")

    def test_clears_after_wave_sync_marks_paid(self):
        fid, req = self.ended_with_request()
        # the flight gets onto some other Wave invoice and is then paid by Wave
        inv = flight.wave_billing  # noqa: F841
        conn = db.get_db()
        lid = seed_row(conn, "wave_invoices", kind="student", ref_id=self.student, wave_invoice_id="INVX", status="SENT")
        conn.execute("UPDATE flights SET wave_invoice_id = ? WHERE id = ?", (lid, fid))
        conn.commit()
        flight.wave_billing.apply_paid(conn, dict(conn.execute("SELECT * FROM wave_invoices WHERE id = ?", (lid,)).fetchone()))
        conn.commit()
        conn.close()
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id = ?", (fid,))["paid"], 1)
        self.assert_cleared("paid another way")

    def test_already_on_wave_invoice_clears(self):
        fid, req = self.ended_with_request()
        conn = db.get_db()
        lid = seed_row(conn, "wave_invoices", kind="student", ref_id=self.student, wave_invoice_id="INVY", status="SENT")
        conn.execute("UPDATE flights SET wave_invoice_id = ? WHERE id = ?", (lid, fid))
        conn.commit()
        conn.close()
        self.login("master").get("/flight/billing/approvals")
        self.assert_cleared("Wave invoice")

    def test_nothing_due_clears(self):
        fid, req = self.ended_with_request()
        self.exec("UPDATE flights SET credit_applied = 99999 WHERE id = ?", (fid,))
        self.login("master").get("/flight/billing/approvals")
        self.assert_cleared("Nothing is due")

    def test_still_due_stays_pending(self):
        fid, req = self.ended_with_request()
        self.login("master").get("/flight/billing/approvals")
        self.assertEqual(self.row()["status"], "pending")

    def test_student_can_ask_again_after_clear(self):
        fid, req = self.ended_with_request()
        self.exec("UPDATE flights SET paid = 1 WHERE id = ?", (fid,))
        self.login("master").get("/flight/billing/approvals")
        self.login("flight_student").get("/flight/my-account")  # no crash on not_needed status
