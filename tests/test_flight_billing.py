"""Flight school billing: student ledger vs balance, cost math (rounding, solo,
rate overrides), Add Funds, Mark Paid / Unpaid, CFI pay, who may do what."""
from harness import OpsHubTestCase, open_finding
import db


class FlightBillingTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N400BL")
        self.exec("UPDATE assets SET is_flight_asset=1, hobbs_hours=100.0, tach_hours=90.0 WHERE id=?", (self.plane,))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id=?", (self.users["cfi"]["id"],))["id"]
        self.student = self.q1("SELECT id FROM students WHERE user_id=?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE cfis SET rate_per_hour=50, pay_rate_per_hour=30 WHERE id=?", (self.cfi,))
        self.exec("UPDATE students SET plane_rate_override=100 WHERE id=?", (self.student,))

    # ----- helpers ---------------------------------------------------------
    def logged_flight(self, hobbs=(100.0, 101.0), solo=0, ground=0, flight_date="2026-10-01", cfi=True):
        """A finished flight with its cost already taken off the balance, the
        way End Session leaves it."""
        fid = self.exec(
            "INSERT INTO flights (student_id, asset_id, cfi_id, flight_date, hobbs_start, hobbs_end, tach_start, tach_end, "
            "solo, ground_time_hours, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.student, self.plane, self.cfi if cfi else None, flight_date, hobbs[0], hobbs[1], hobbs[0], hobbs[1],
             solo, ground, "2026-10-01T09:00:00", "2026-10-01T10:00:00"))
        conn = db.get_db()
        import flight as fl
        f = fl._row_with_cost(conn.execute(fl._LOG_ROW_SQL + " WHERE f.id=?", (fid,)).fetchone())
        fl._deduct_flight_cost(conn, f, created_by="test")
        conn.commit()
        conn.close()
        return fid

    def balance(self):
        return round(self.q1("SELECT balance FROM students WHERE id=?", (self.student,))["balance"], 2)

    def ledger_sum(self):
        return round(self.q1("SELECT COALESCE(SUM(amount),0) AS t FROM student_ledger WHERE student_id=?", (self.student,))["t"], 2)

    def assertBalanceMatchesLedger(self):
        self.assertEqual(self.balance(), self.ledger_sum())

    def add_funds(self, **data):
        return self.client.post(f"/flight/students/{self.student}/add_funds", data=data)

    # ----- cost math -------------------------------------------------------
    def test_dual_flight_cost_is_plane_plus_instructor(self):
        self.logged_flight(hobbs=(100.0, 101.0))
        self.assertEqual(self.balance(), -150.0)  # 1.0h x (100 plane + 50 instructor)
        self.assertBalanceMatchesLedger()

    def test_minutes_round_up_to_next_tenth(self):
        self.logged_flight(hobbs=(100.0, 100.01))
        self.assertEqual(self.balance(), -15.0)  # 0.1h x 150

    def test_solo_flight_has_no_instructor_charge(self):
        self.logged_flight(hobbs=(100.0, 101.0), solo=1)
        self.assertEqual(self.balance(), -100.0)

    def test_ground_time_billed_at_instructor_rate_only(self):
        self.logged_flight(hobbs=(100.0, 101.0), ground=0.5)
        self.assertEqual(self.balance(), -175.0)  # 150 + 0.5 x 50

    def test_student_without_plane_rate_is_charged_nothing_for_plane(self):
        self.exec("UPDATE students SET plane_rate_override=NULL WHERE id=?", (self.student,))
        self.logged_flight(hobbs=(100.0, 101.0))
        self.assertEqual(self.balance(), -50.0)

    def test_student_instructor_rate_override_wins(self):
        self.exec("UPDATE students SET rate_override=70 WHERE id=?", (self.student,))
        self.logged_flight(hobbs=(100.0, 101.0))
        self.assertEqual(self.balance(), -170.0)

    def test_deduction_happens_only_once(self):
        fid = self.logged_flight()
        import flight as fl
        conn = db.get_db()
        f = fl._row_with_cost(conn.execute(fl._LOG_ROW_SQL + " WHERE f.id=?", (fid,)).fetchone())
        fl._deduct_flight_cost(conn, f)
        fl._deduct_flight_cost(conn, f)
        conn.commit()
        conn.close()
        self.assertEqual(self.balance(), -150.0)
        self.assertBalanceMatchesLedger()

    # ----- add funds -------------------------------------------------------
    def test_add_funds_raises_balance_and_ledger_together(self):
        self.login("master")
        self.add_funds(amount="200", paid_method="Cash")
        self.assertEqual(self.balance(), 200.0)
        self.assertBalanceMatchesLedger()

    def test_add_funds_rejects_bad_amounts(self):
        self.login("master")
        for bad in ("", "0", "-50", "abc", "nan", "inf", "-inf"):
            with self.subTest(amount=bad):
                r = self.add_funds(amount=bad)
                self.assertLess(r.status_code, 500)
                self.assertEqual(self.balance(), 0.0)
                self.assertEqual(self.q1("SELECT COUNT(*) AS n FROM student_ledger")["n"], 0)

    def test_add_funds_unknown_student_is_not_an_error_page(self):
        self.login("master")
        r = self.client.post("/flight/students/99999/add_funds", data={"amount": "10"})
        self.assertLess(r.status_code, 500)

    def test_add_funds_needs_billing_permission(self):
        for role in ("cfi", "flight_student", "no_roles", "tech"):
            with self.subTest(role=role):
                self.login(role)
                self.add_funds(amount="500")
                self.assertEqual(self.balance(), 0.0)
        self.login(None)
        self.add_funds(amount="500")
        self.assertEqual(self.balance(), 0.0)

    def test_billing_cfi_can_add_funds(self):
        self.login("cfi_billing")
        self.add_funds(amount="75.50")
        self.assertEqual(self.balance(), 75.5)

    def test_add_funds_with_card_but_no_number_changes_nothing(self):
        self.login("master")
        self.add_funds(amount="100", paid_by_card="1", card_number="")
        self.assertEqual(self.balance(), 0.0)

    def test_add_funds_can_pay_ticked_flights_and_leave_the_rest(self):
        self.login("master")
        fid = self.logged_flight()  # owes 150
        self.add_funds(amount="200", apply_flight_ids=str(fid), paid_method="Cash")
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id=?", (fid,))["paid"], 1)
        self.assertEqual(self.balance(), 50.0)  # 200 in, 150 flight cost paid, 50 left as credit
        self.assertBalanceMatchesLedger()

    def test_add_funds_refuses_when_ticked_flights_cost_more(self):
        self.login("master")
        fid = self.logged_flight()  # owes 150
        self.add_funds(amount="100", apply_flight_ids=str(fid))
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id=?", (fid,))["paid"], 0)
        self.assertEqual(self.balance(), -150.0)
        self.assertBalanceMatchesLedger()

    def test_add_funds_cannot_pay_another_students_flight(self):
        self.login("master")
        other = self.exec("INSERT INTO students (name, username, password_hash) VALUES ('Other','other','x')")
        fid = self.exec("INSERT INTO flights (student_id, asset_id, flight_date) VALUES (?,?,?)",
                        (other, self.plane, "2026-10-01"))
        self.add_funds(amount="500", apply_flight_ids=str(fid))
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id=?", (fid,))["paid"], 0)
        self.assertEqual(self.balance(), 0.0)

    def test_add_funds_junk_flight_ids_do_not_crash(self):
        self.login("master")
        r = self.client.post(f"/flight/students/{self.student}/add_funds",
                             data={"amount": "20", "apply_flight_ids": ["abc", "", "99999"]})
        self.assertLess(r.status_code, 500)

    # ----- mark paid / unpaid ---------------------------------------------
    def toggle(self, fid, **data):
        return self.client.post(f"/flight/log/{fid}/toggle_paid", data=data)

    def test_mark_paid_then_unpaid_returns_ledger_to_start(self):
        self.login("master")
        fid = self.logged_flight()
        self.toggle(fid, paid_method="Cash")
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id=?", (fid,))["paid"], 1)
        self.assertEqual(self.balance(), 0.0)
        self.toggle(fid)
        self.assertEqual(self.q1("SELECT paid FROM flights WHERE id=?", (fid,))["paid"], 0)
        self.assertEqual(self.balance(), -150.0)
        self.assertBalanceMatchesLedger()

    def test_mark_paid_partial_amount_records_only_that_amount(self):
        self.login("master")
        fid = self.logged_flight()
        self.toggle(fid, payment_amount="100", payment_method="Cash")
        self.assertEqual(self.balance(), -50.0)
        self.assertBalanceMatchesLedger()

    def test_mark_paid_amount_above_cost_is_clamped(self):
        self.login("master")
        fid = self.logged_flight()
        self.toggle(fid, payment_amount="9999", payment_method="Cash")
        self.assertEqual(self.balance(), 0.0)

    def test_mark_paid_bad_amount_does_not_corrupt_ledger(self):
        for bad in ("nan", "inf", "-100", "abc"):
            with self.subTest(amount=bad):
                self.login("master")
                self.exec("DELETE FROM student_ledger")
                self.exec("DELETE FROM flights")
                self.exec("UPDATE students SET balance=0")
                fid = self.logged_flight()
                r = self.toggle(fid, payment_amount=bad, payment_method="Cash")
                self.assertLess(r.status_code, 500)
                self.assertBalanceMatchesLedger()
                self.assertLessEqual(self.balance(), 0.0)
                self.assertGreaterEqual(self.balance(), -150.0)

    def test_mark_selected_paid_twice_does_not_double_credit(self):
        self.login("master")
        fid = self.logged_flight()
        self.client.post("/flight/billing/mark-selected-paid", data={"ids": str(fid), "paid_method": "Cash"})
        self.client.post("/flight/billing/mark-selected-paid", data={"ids": str(fid), "paid_method": "Cash"})
        self.assertEqual(self.balance(), 0.0)
        self.assertBalanceMatchesLedger()

    def test_mark_selected_paid_junk_and_empty_ids(self):
        self.login("master")
        for ids in ("", "abc", "99999", ",,"):
            with self.subTest(ids=ids):
                r = self.client.post("/flight/billing/mark-selected-paid", data={"ids": ids})
                self.assertLess(r.status_code, 500)

    def test_toggle_paid_missing_flight_is_not_an_error_page(self):
        self.login("master")
        self.assertLess(self.toggle(99999).status_code, 500)

    def test_toggle_paid_needs_billing_permission(self):
        fid = self.logged_flight()
        for role in ("cfi", "flight_student", "no_roles"):
            with self.subTest(role=role):
                self.login(role)
                self.toggle(fid, paid_method="Cash")
                self.assertEqual(self.q1("SELECT paid FROM flights WHERE id=?", (fid,))["paid"], 0)
                self.assertEqual(self.balance(), -150.0)

    def test_cash_collected_by_billing_cfi_is_logged_for_office_check(self):
        self.login("cfi_billing")
        fid = self.logged_flight()
        self.toggle(fid, paid_method="Cash")
        n = self.q1("SELECT COUNT(*) AS n FROM flight_payment_confirmations WHERE flight_id=?", (fid,))["n"]
        self.assertEqual(n, 1)

    def test_whole_history_always_adds_up(self):
        self.login("master")
        a = self.logged_flight(hobbs=(100.0, 101.0))
        b = self.logged_flight(hobbs=(101.0, 102.5))
        self.add_funds(amount="80")
        self.toggle(a, paid_method="Cash")
        self.toggle(b, payment_amount="40", payment_method="Card")
        self.toggle(a)
        self.add_funds(amount="12.34", apply_flight_ids=str(a))
        self.assertBalanceMatchesLedger()

    # ----- CFI pay ---------------------------------------------------------
    def test_cfi_pay_page_roles(self):
        self.logged_flight()
        url = f"/flight/cfis/{self.cfi}/pay"
        self.login("cfi")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.login("master")
        self.assertEqual(self.client.get(url).status_code, 200)
        for role in ("cfi_billing", "flight_student", "no_roles"):
            with self.subTest(role=role):
                self.login(role)
                r = self.client.get(url)
                self.assertNotEqual(r.status_code, 200)

    def test_cfi_pay_missing_cfi_is_not_an_error_page(self):
        self.login("master")
        self.assertLess(self.client.get("/flight/cfis/99999/pay").status_code, 500)

    def test_cfi_pay_amount_uses_pay_rate_not_student_rate(self):
        self.logged_flight(hobbs=(100.0, 102.0))
        self.login("cfi")
        html = self.client.get(f"/flight/cfis/{self.cfi}/pay?range=custom&start=2026-10-01&end=2026-10-31").get_data(as_text=True)
        self.assertIn("60.00", html)      # 2h x $30 pay rate

    def test_cfi_pay_excludes_solo_flights(self):
        self.logged_flight(hobbs=(100.0, 102.0), solo=1)
        self.login("cfi")
        html = self.client.get(f"/flight/cfis/{self.cfi}/pay?range=custom&start=2026-10-01&end=2026-10-31").get_data(as_text=True)
        self.assertNotIn("60.00", html)

    def test_mark_period_paid_only_master_and_only_once(self):
        fid = self.logged_flight()
        data = {"start": "2026-10-01", "end": "2026-10-31"}
        url = f"/flight/cfis/{self.cfi}/pay/mark-paid"
        self.login("cfi")
        self.client.post(url, data=data)
        self.assertIsNone(self.q1("SELECT cfi_paid_at FROM flights WHERE id=?", (fid,))["cfi_paid_at"])
        self.login("master")
        self.client.post(url, data=data)
        stamp = self.q1("SELECT cfi_paid_at FROM flights WHERE id=?", (fid,))["cfi_paid_at"]
        self.assertIsNotNone(stamp)
        self.client.post(url, data=data)
        self.assertEqual(self.q1("SELECT cfi_paid_at FROM flights WHERE id=?", (fid,))["cfi_paid_at"], stamp)

    def test_mark_period_paid_bad_dates_do_not_crash(self):
        self.login("master")
        for s, e in (("", ""), ("abc", "2026-10-31"), ("2026-13-45", "2026-10-31")):
            with self.subTest(s=s, e=e):
                r = self.client.post(f"/flight/cfis/{self.cfi}/pay/mark-paid", data={"start": s, "end": e})
                self.assertLess(r.status_code, 500)

    # ----- billing pages ---------------------------------------------------
    def test_billing_page_roles(self):
        self.logged_flight()
        self.login("master")
        self.assertEqual(self.client.get("/flight/billing").status_code, 200)
        self.login("cfi_billing")
        self.assertEqual(self.client.get("/flight/billing").status_code, 200)
        for role in ("cfi", "flight_student", "no_roles"):
            with self.subTest(role=role):
                self.login(role)
                r = self.client.get("/flight/billing")
                self.assertNotIn(b"Outstanding", r.data)
                self.assertNotEqual(r.status_code, 500)
