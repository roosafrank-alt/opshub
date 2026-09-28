"""Wave invoicing (wave_billing.py): real invoices in Wave from Shop Billing
(a job) and Flight School Billing (a student's unpaid flights), emailed by
Wave, and a payment check that marks the job / flights paid once Wave says
the invoice is PAID. Wave itself is faked - FakeWave stands in for its
GraphQL API and records every call."""
import io
import json
import urllib.error
import urllib.request
from datetime import date
from unittest import mock

from harness import OpsHubTestCase, seed_row
import db
import wave_billing
import app as app_module


class FakeWave:
    """Answers the handful of queries/mutations wave_billing sends."""

    def __init__(self):
        self.calls = []
        self.tokens = []
        self.invoices = {}
        self.fail_send = False

    def __call__(self, token, query, variables=None, timeout=20):
        variables = variables or {}
        self.calls.append((query, variables))
        self.tokens.append(token)
        if "businesses(" in query:
            if token == "tok2":
                return {"businesses": {"edges": [
                    {"node": {"id": "BIZ3", "name": "Fly with Kate", "isPersonal": False, "currency": {"code": "USD"}}}]}}
            return {"businesses": {"edges": [
                {"node": {"id": "BIZ1", "name": "Winds Aloft LLC", "isPersonal": False, "currency": {"code": "USD"}}},
                {"node": {"id": "BIZ2", "name": "Frank personal", "isPersonal": True, "currency": {"code": "USD"}}}]}}
        if "products(" in query:
            return {"business": {"products": {"edges": [
                {"node": {"id": "P-LABOR", "name": "Shop labor", "unitPrice": "0", "isSold": True, "isArchived": False}},
                {"node": {"id": "P-PARTS", "name": "Parts", "unitPrice": "0", "isSold": True, "isArchived": False}},
                {"node": {"id": "P-FLIGHT", "name": "Flight training", "unitPrice": "0", "isSold": True, "isArchived": False}},
                {"node": {"id": "P-OLD", "name": "Old thing", "unitPrice": "0", "isSold": True, "isArchived": True}}]}}}
        if "customers(" in query:
            return {"business": {"customers": {"edges": []}}}
        if "customerCreate" in query:
            return {"customerCreate": {"didSucceed": True, "inputErrors": [], "customer": {"id": "CUST-" + variables["input"]["name"]}}}
        if "invoiceCreate" in query:
            n = len(self.invoices) + 1
            items = variables["input"]["items"]
            total = round(sum(float(i["quantity"]) * float(i["unitPrice"]) for i in items), 2)
            inv = {"id": f"INV{n}", "invoiceNumber": str(100 + n), "status": "SAVED", "_business": variables["input"]["businessId"],
                   "viewUrl": f"https://next.waveapps.com/view/INV{n}", "pdfUrl": f"https://next.waveapps.com/pdf/INV{n}",
                   "total": {"value": str(total)}, "amountDue": {"value": str(total)}, "amountPaid": {"value": "0"}}
            self.invoices[inv["id"]] = inv
            return {"invoiceCreate": {"didSucceed": True, "inputErrors": [], "invoice": inv}}
        if "invoiceSend" in query:
            if self.fail_send:
                return {"invoiceSend": {"didSucceed": False, "inputErrors": [{"message": "Email bounced"}]}}
            self.invoices[variables["input"]["invoiceId"]]["status"] = "SENT"
            return {"invoiceSend": {"didSucceed": True, "inputErrors": []}}
        if "invoice(id:" in query:
            inv = self.invoices.get(variables["invoiceId"])
            # Only found in the business it was made in.
            return {"business": {"invoice": inv if inv and inv["_business"] == variables["businessId"] else None}}
        raise AssertionError("unexpected Wave query: " + query)

    def pay(self, inv_id):
        inv = self.invoices[inv_id]
        inv["status"] = "PAID"
        inv["amountPaid"], inv["amountDue"] = inv["total"], {"value": "0"}

    def created_items(self):
        return [v["input"]["items"] for q, v in self.calls if "invoiceCreate" in q]

    def created_in(self):
        return [v["input"]["businessId"] for q, v in self.calls if "invoiceCreate" in q]


class WaveTestBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.wave = FakeWave()
        patcher = mock.patch.object(wave_billing, "_gql", self.wave)
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect_wave(self, n=1, token="tok", business="BIZ1", programs=("shop", "flight"), token_from="", name=""):
        """Sets up Wave account n with all three products and gives it to
        programs (as their default when they have none yet)."""
        conn = db.get_db()
        raw = wave_billing.get_settings(conn)
        values = {wave_billing.account_key(n, "token"): token, wave_billing.account_key(n, "token_from"): token_from,
                  wave_billing.account_key(n, "business_id"): business, wave_billing.account_key(n, "name"): name,
                  wave_billing.account_key(n, "labor_product_id"): "P-LABOR",
                  wave_billing.account_key(n, "parts_product_id"): "P-PARTS",
                  wave_billing.account_key(n, "flight_product_id"): "P-FLIGHT"}
        for p in programs:
            have = [x for x in raw[wave_billing.program_key(p, "accounts")].split(",") if x]
            values[wave_billing.program_key(p, "accounts")] = ",".join(have + [str(n)])
            values[wave_billing.program_key(p, "default")] = raw[wave_billing.program_key(p, "default")] or str(n)
        wave_billing.save_settings(conn, values)
        conn.close()


class AdminWaveTest(WaveTestBase):
    def settings(self):
        conn = db.get_db()
        s = wave_billing.get_settings(conn)
        conn.close()
        return s

    def test_setup_flow_token_then_business_then_products(self):
        c = self.login("master")
        html = c.get("/admin/wave").get_data(as_text=True)
        self.assertIn("Not set up", html)
        c.post("/admin/wave", data={"wave_acct1_token": "secret-token", "use_shop_1": "1"})
        html = c.get("/admin/wave").get_data(as_text=True)
        self.assertIn("Winds Aloft LLC", html)
        self.assertNotIn("secret-token", html)  # never echoed back
        c.post("/admin/wave", data={"wave_acct1_business_id": "BIZ1|Winds Aloft LLC", "use_shop_1": "1"})
        html = c.get("/admin/wave").get_data(as_text=True)
        self.assertIn("Connected", html)
        self.assertIn("Flight training", html)
        self.assertNotIn("Old thing", html)  # archived products aren't offered
        c.post("/admin/wave", data={"wave_acct1_business_id": "BIZ1|Winds Aloft LLC", "use_shop_1": "1",
                                    "wave_acct1_labor_product_id": "P-LABOR", "wave_acct1_parts_product_id": "P-PARTS"})
        s = self.settings()
        self.assertEqual(s["wave_acct1_token"], "secret-token")  # blank kept the saved one
        self.assertEqual(s["wave_acct1_business_name"], "Winds Aloft LLC")  # kept though the select sent no name
        self.assertEqual(s["wave_acct1_parts_product_id"], "P-PARTS")
        self.assertEqual((s["wave_shop_accounts"], s["wave_shop_default"]), ("1", "1"))
        self.assertEqual(s["wave_flight_accounts"], "")

    def test_switching_business_clears_products(self):
        self.connect_wave()
        c = self.login("master")
        c.post("/admin/wave", data={"wave_acct1_business_id": "BIZ2|Frank personal", "wave_acct1_labor_product_id": "P-LABOR"})
        s = self.settings()
        self.assertEqual(s["wave_acct1_business_id"], "BIZ2")
        self.assertEqual(s["wave_acct1_labor_product_id"], "")

    def test_three_accounts_two_on_one_program_with_a_default(self):
        c = self.login("master")
        c.post("/admin/wave", data={
            "wave_acct1_token": "tok", "wave_acct1_business_id": "BIZ1", "use_shop_1": "1",
            "wave_acct2_token_from": "1", "wave_acct2_business_id": "BIZ2", "use_shop_2": "1", "use_flight_2": "1",
            "wave_acct3_token": "tok2", "wave_acct3_business_id": "BIZ3", "use_flight_3": "1",
            "wave_shop_default": "2", "wave_flight_default": "1"})  # 1 isn't a Flight School account
        s = self.settings()
        self.assertEqual((s["wave_shop_accounts"], s["wave_shop_default"]), ("1,2", "2"))
        self.assertEqual((s["wave_flight_accounts"], s["wave_flight_default"]), ("2,3", "2"))
        conn = db.get_db()
        acct2 = wave_billing.account_config(wave_billing.get_settings(conn), 2)
        conn.close()
        self.assertEqual(acct2["token"], "tok")  # shares Account 1's login
        html = c.get("/admin/wave").get_data(as_text=True)
        self.assertIn("Fly with Kate", html)  # Account 3's own login lists its own business

    def test_disconnect_takes_the_account_off_both_programs(self):
        self.connect_wave(1)
        self.connect_wave(2, token="", token_from="1", business="BIZ2")
        self.login("master").post("/admin/wave/disconnect/1")
        s = self.settings()
        self.assertEqual(s["wave_acct1_token"], "")
        self.assertEqual(s["wave_acct2_token_from"], "")  # its shared login went with it
        self.assertEqual((s["wave_shop_accounts"], s["wave_shop_default"]), ("2", "2"))

    def test_only_master_admin(self):
        c = self.login("shop_admin")
        r = c.get("/admin/wave")
        self.assertNotEqual(r.status_code, 200)


class WaveMigrationTest(OpsHubTestCase):
    def test_single_account_setup_becomes_account_1_for_both_programs(self):
        conn = db.get_db()
        for k, v in {"wave_access_token": "tok", "wave_business_id": "BIZ1", "wave_labor_product_id": "P-LABOR",
                     "wave_parts_product_id": "P-PARTS", "wave_flight_product_id": "P-FLIGHT",
                     "wavecust:owner@example.com": "CUST-9"}.items():
            conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (k, v))
        conn.execute("""INSERT INTO wave_invoices (kind, ref_id, wave_invoice_id, invoice_number, created_at)
                        VALUES ('project', 1, 'INV1', '101', ?)""", (db.now_iso(),))
        conn.commit()
        db._migrate(conn)
        s = wave_billing.get_settings(conn)
        self.assertEqual((s["wave_acct1_token"], s["wave_acct1_business_id"], s["wave_acct1_flight_product_id"]),
                         ("tok", "BIZ1", "P-FLIGHT"))
        self.assertEqual((s["wave_shop_accounts"], s["wave_flight_default"]), ("1", "1"))
        inv = conn.execute("SELECT account, business_id FROM wave_invoices").fetchone()
        self.assertEqual((inv["account"], inv["business_id"]), (1, "BIZ1"))
        self.assertIsNotNone(conn.execute("SELECT 1 FROM app_settings WHERE key = 'wavecust:BIZ1:owner@example.com'").fetchone())
        self.assertIsNone(conn.execute("SELECT 1 FROM app_settings WHERE key = 'wave_access_token'").fetchone())
        db._migrate(conn)  # runs once - a second pass changes nothing
        self.assertEqual(wave_billing.get_settings(conn)["wave_acct1_token"], "tok")
        conn.close()


class ShopWaveInvoiceTest(WaveTestBase):
    def setUp(self):
        super().setUp()
        conn = db.get_db()
        self.laborer = seed_row(conn, "laborers", name="Tech", code="LABOR-A", rate=85, active=1)
        conn.commit()
        conn.close()
        self.asset = self.make_asset("N12345")
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, self.asset))
        self.project = self.make_project(name="Annual - N12345", asset_id=self.asset)
        today = db.now_iso()[:10]
        self.exec("""INSERT INTO labor_sessions (laborer_id, project_id, section, started_at, ended_at, hours, rate, cost, created_at)
                     VALUES (?,?,?,?,?,?,?,?,?)""",
                  (self.laborer, self.project, "Engine", today + " 08:00:00", today + " 10:30:00", 2.5, 85, 212.5, db.now_iso()))
        part = self.make_part(name="Oil Filter", barcode="PART-001", qty=10)
        self.exec("UPDATE parts SET sell_price = 30 WHERE id = ?", (part,))
        self.exec("""INSERT INTO transactions (part_id, type, qty, project_id, section, created_at)
                     VALUES (?, 'out', 2, ?, 'Engine', ?)""", (part, self.project, db.now_iso()))

    def invoice(self, role="shop_admin", **data):
        body = {"customer_name": "Owner Customer", "customer_email": "owner@example.com", "send": "1"}
        body.update(data)
        return self.login(role).post(f"/shop/billing/{self.project}/wave-invoice", data=body, follow_redirects=True)

    def test_billing_page_offers_wave_only_once_connected(self):
        c = self.login("shop_admin")
        self.assertNotIn("Invoice in Wave</button>", c.get("/shop/billing?period=all").get_data(as_text=True))
        self.connect_wave()
        html = c.get("/shop/billing?period=all").get_data(as_text=True)
        self.assertIn("Invoice in Wave</button>", html)
        self.assertIn('data-customer-email="owner@example.com"', html)  # prefilled from My Aircraft owner

    def test_creates_sends_and_marks_invoiced(self):
        self.connect_wave()
        html = self.invoice().get_data(as_text=True)
        self.assertIn("Wave invoice #101 created in Account 1 for $272.50 and emailed to owner@example.com", html)
        items = self.wave.created_items()[0]
        self.assertEqual([i["productId"] for i in items], ["P-PARTS", "P-LABOR"])
        self.assertEqual((items[0]["quantity"], items[0]["unitPrice"]), (2, 30))
        self.assertEqual((items[1]["quantity"], items[1]["unitPrice"]), (2.5, 85))
        p = self.q1("SELECT * FROM projects WHERE id = ?", (self.project,))
        self.assertEqual(p["payment_status"], "invoiced")
        w = self.q1("SELECT * FROM wave_invoices WHERE kind = 'project' AND ref_id = ?", (self.project,))
        self.assertEqual(w["invoice_number"], "101")
        self.assertEqual(w["total"], 272.5)
        self.assertTrue(w["sent_at"])
        html = self.login("shop_admin").get("/shop/billing?period=all").get_data(as_text=True)
        self.assertIn("Wave #101", html)
        self.assertNotIn(f"/shop/billing/{self.project}/wave-invoice", html)

    def test_second_invoice_for_same_job_is_refused(self):
        self.connect_wave()
        self.invoice()
        self.invoice()
        self.assertEqual(len(self.wave.created_items()), 1)

    def test_send_failure_still_keeps_the_invoice(self):
        self.connect_wave()
        self.wave.fail_send = True
        html = self.invoice().get_data(as_text=True)
        self.assertIn("wasn&#39;t emailed", html)
        w = self.q1("SELECT * FROM wave_invoices WHERE ref_id = ?", (self.project,))
        self.assertIsNone(w["sent_at"])

    def test_not_connected_or_missing_product_is_refused(self):
        self.invoice()
        self.assertEqual(self.wave.created_items(), [])
        self.connect_wave()
        self.exec("UPDATE app_settings SET value = '' WHERE key = 'wave_acct1_labor_product_id'")
        html = self.invoice().get_data(as_text=True)
        self.assertIn("product for shop labor", html)
        self.assertEqual(self.wave.created_items(), [])

    def test_send_without_email_is_refused(self):
        self.connect_wave()
        self.invoice(customer_email="")
        self.assertEqual(self.wave.created_items(), [])

    def test_tech_cannot_invoice(self):
        self.connect_wave()
        self.invoice(role="tech")
        self.assertEqual(self.wave.created_items(), [])

    def test_customer_is_remembered_by_email(self):
        self.connect_wave()
        self.invoice()
        other = self.make_project(name="Oil change", asset_id=self.asset)
        self.exec("""INSERT INTO labor_sessions (laborer_id, project_id, started_at, ended_at, hours, rate, cost, created_at)
                     VALUES (?,?,?,?,?,?,?,?)""", (self.laborer, other, db.now_iso(), db.now_iso(), 1, 85, 85, db.now_iso()))
        self.login("shop_admin").post(f"/shop/billing/{other}/wave-invoice",
                                      data={"customer_name": "Owner Customer", "customer_email": "owner@example.com"})
        self.assertEqual(sum(1 for q, v in self.wave.calls if "customerCreate" in q), 1)

    def test_payment_sync_marks_job_paid_once(self):
        self.connect_wave()
        self.invoice()
        c = self.login("shop_admin")
        html = c.post("/shop/billing/wave-sync", follow_redirects=True).get_data(as_text=True)
        self.assertIn("no new payments", html)
        self.assertEqual(self.q1("SELECT payment_status FROM projects WHERE id = ?", (self.project,))[0], "invoiced")
        self.wave.pay("INV1")
        html = c.post("/shop/billing/wave-sync", follow_redirects=True).get_data(as_text=True)
        self.assertIn("1 newly paid", html)
        p = self.q1("SELECT * FROM projects WHERE id = ?", (self.project,))
        self.assertEqual((p["payment_status"], p["paid_method"]), ("paid", "Wave"))
        html = c.post("/shop/billing/wave-sync", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Checked 0 open Wave invoices", html)

    def test_payment_sync_leaves_a_hand_marked_paid_job_alone(self):
        self.connect_wave()
        self.invoice()
        self.exec("UPDATE projects SET payment_status = 'paid', paid_method = 'Check' WHERE id = ?", (self.project,))
        self.wave.pay("INV1")
        self.login("shop_admin").post("/shop/billing/wave-sync")
        self.assertEqual(self.q1("SELECT paid_method FROM projects WHERE id = ?", (self.project,))[0], "Check")

    def test_two_accounts_on_the_shop_default_then_swap(self):
        self.connect_wave(1, name="Maintenance")
        self.connect_wave(2, token="tok2", business="BIZ3", programs=("shop",), name="Avionics")
        html = self.login("shop_admin").get("/shop/billing?period=all").get_data(as_text=True)
        self.assertIn('name="wave_account"', html)
        self.assertIn("Avionics", html)
        self.invoice()  # nothing picked = the Shop's default (Account 1)
        other = self.make_project(name="Radio install", asset_id=self.asset)
        self.exec("""INSERT INTO labor_sessions (laborer_id, project_id, started_at, ended_at, hours, rate, cost, created_at)
                     VALUES (?,?,?,?,?,?,?,?)""", (self.laborer, other, db.now_iso(), db.now_iso(), 1, 85, 85, db.now_iso()))
        html = self.login("shop_admin").post(f"/shop/billing/{other}/wave-invoice", follow_redirects=True, data={
            "customer_name": "Owner Customer", "customer_email": "owner@example.com", "wave_account": "2"}).get_data(as_text=True)
        self.assertIn("created in Avionics", html)
        self.assertEqual(self.wave.created_in(), ["BIZ1", "BIZ3"])
        rows = {r["ref_id"]: (r["account"], r["business_id"]) for r in self.q("SELECT * FROM wave_invoices")}
        self.assertEqual(rows, {self.project: (1, "BIZ1"), other: (2, "BIZ3")})
        # The same customer gets their own id in each business.
        self.assertEqual(sum(1 for q, v in self.wave.calls if "customerCreate" in q), 2)
        # Each payment is looked up with the login/business it was made in.
        self.wave.pay("INV2")
        self.wave.tokens.clear()
        self.login("shop_admin").post("/shop/billing/wave-sync")
        self.assertEqual(sorted(self.wave.tokens), ["tok", "tok2"])
        self.assertEqual(self.q1("SELECT payment_status FROM projects WHERE id = ?", (other,))[0], "paid")
        self.assertEqual(self.q1("SELECT payment_status FROM projects WHERE id = ?", (self.project,))[0], "invoiced")

    def test_invoice_found_after_the_program_swaps_its_default(self):
        self.connect_wave(1)
        self.invoice()
        self.connect_wave(2, token="tok2", business="BIZ3", programs=("shop",))
        self.exec("UPDATE app_settings SET value = '2' WHERE key = 'wave_shop_default'")
        self.wave.pay("INV1")
        self.login("shop_admin").post("/shop/billing/wave-sync")
        self.assertEqual(self.q1("SELECT payment_status FROM projects WHERE id = ?", (self.project,))[0], "paid")

    def test_account_not_given_to_the_shop_is_refused(self):
        self.connect_wave(1, programs=("shop",))
        self.connect_wave(2, token="tok2", business="BIZ3", programs=("flight",))
        html = self.invoice(wave_account="2").get_data(as_text=True)
        self.assertIn("isn&#39;t set up for the Shop", html)
        self.assertEqual(self.wave.created_items(), [])

    def test_disconnected_account_invoices_are_reported_not_crashed(self):
        self.connect_wave(1)
        self.invoice()
        self.connect_wave(2, token="tok2", business="BIZ3")
        self.login("master").post("/admin/wave/disconnect/1")
        html = self.login("shop_admin").post("/shop/billing/wave-sync", follow_redirects=True).get_data(as_text=True)
        self.assertIn("isn&#39;t connected any more", html)

    def test_background_check_runs_only_when_connected(self):
        app_module._wave_last_sync[0] = 0
        app_module.run_wave_payment_check()
        self.assertEqual(self.wave.calls, [])
        self.connect_wave()
        self.invoice()
        self.wave.pay("INV1")
        app_module._wave_last_sync[0] = 0
        app_module.run_wave_payment_check()
        self.assertEqual(self.q1("SELECT payment_status FROM projects WHERE id = ?", (self.project,))[0], "paid")


class FlightWaveInvoiceTest(WaveTestBase):
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE users SET email = 'stu@example.com' WHERE id = ?", (self.users["flight_student"]["id"],))
        self.exec("UPDATE students SET plane_rate_override = 150 WHERE id = ?", (self.student,))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.exec("UPDATE cfis SET rate_per_hour = 60 WHERE id = ?", (self.cfi,))
        self.asset = self.make_asset("N321CC")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.asset,))
        self.f1 = self.flight(100.0, 101.5)
        self.f2 = self.flight(101.5, 102.5)
        self.paid = self.flight(102.5, 103.0, paid=1)
        self.guest = self.flight(103.0, 104.0, guest_name="Intro Guest")

    def flight(self, hs, he, paid=0, guest_name=None):
        return self.exec("""INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, solo, hobbs_start, hobbs_end,
                                                 paid, guest_name, created_at) VALUES (?,?,?,?,0,?,?,?,?,?)""",
                         (self.cfi, self.student, self.asset, date.today().isoformat(), hs, he, paid, guest_name, db.now_iso()))

    def invoice(self, role="cfi_billing"):
        return self.login(role).post(f"/flight/billing/student/{self.student}/wave-invoice",
                                     data={"customer_name": "Flight Student", "customer_email": "stu@example.com", "send": "1"},
                                     follow_redirects=True)

    def test_billing_page_button_prefilled_with_student_email(self):
        self.connect_wave()
        html = self.login("cfi_billing").get("/flight/billing").get_data(as_text=True)
        self.assertIn("Invoice in Wave</button>", html)
        self.assertIn('data-customer-email="stu@example.com"', html)

    def test_invoices_unpaid_flights_and_links_them(self):
        self.connect_wave()
        html = self.invoice().get_data(as_text=True)
        # 1.5 hr: plane 225 + instructor 90; 1.0 hr: plane 150 + instructor 60
        self.assertIn("created in Account 1 for 2 flights, $525.00", html)
        items = self.wave.created_items()[0]
        self.assertEqual(len(items), 4)
        self.assertTrue(all(i["productId"] == "P-FLIGHT" for i in items))
        w = self.q1("SELECT id FROM wave_invoices WHERE kind = 'student' AND ref_id = ?", (self.student,))
        linked = {r["id"] for r in self.q("SELECT id FROM flights WHERE wave_invoice_id = ?", (w["id"],))}
        self.assertEqual(linked, {self.f1, self.f2})  # not the paid one, not the guest's
        # Nothing left to invoice now
        html = self.invoice().get_data(as_text=True)
        self.assertIn("No unpaid flights left", html)
        self.assertEqual(len(self.wave.created_items()), 1)

    def test_payment_sync_marks_those_flights_paid(self):
        self.connect_wave()
        self.invoice()
        self.wave.pay("INV1")
        self.login("cfi_billing").post("/flight/billing/wave-sync")
        paid = {r["id"]: r["paid"] for r in self.q("SELECT id, paid FROM flights")}
        self.assertEqual((paid[self.f1], paid[self.f2], paid[self.guest]), (1, 1, 0))

    def test_cfi_without_billing_cannot_invoice(self):
        self.connect_wave()
        self.invoice(role="cfi")
        self.assertEqual(self.wave.created_items(), [])


class WaveTransportTest(OpsHubTestCase):
    """_gql itself, with urlopen faked at the HTTP level."""

    def test_bad_token_reads_as_a_clear_message(self):
        def refuse(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
        with mock.patch.object(urllib.request, "urlopen", refuse):
            with self.assertRaises(wave_billing.WaveError) as cm:
                wave_billing.list_businesses("bad")
        self.assertIn("refused the access token", str(cm.exception))

    def test_graphql_errors_and_bearer_header(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def answer(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            seen["url"] = req.full_url
            return Resp(json.dumps({"errors": [{"message": "Invalid business"}]}).encode())
        with mock.patch.object(urllib.request, "urlopen", answer):
            with self.assertRaises(wave_billing.WaveError) as cm:
                wave_billing.list_products("tok", "BIZ")
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertEqual(seen["url"], wave_billing.API_URL)
        self.assertIn("Invalid business", str(cm.exception))

    def test_no_token_never_calls_out(self):
        with self.assertRaises(wave_billing.WaveError):
            wave_billing.list_businesses("")


class WaveSwitchAndTestButtonsTest(WaveTestBase):
    def setUp(self):
        super().setUp()
        conn = db.get_db()
        self.laborer = seed_row(conn, "laborers", name="Tech", code="LABOR-A", rate=85, active=1)
        conn.commit()
        conn.close()
        self.project = self.make_project(name="Annual - N12345")
        self.exec("""INSERT INTO labor_sessions (laborer_id, project_id, started_at, ended_at, hours, rate, cost, created_at)
                     VALUES (?,?,?,?,?,?,?,?)""", (self.laborer, self.project, db.now_iso(), db.now_iso(), 1, 85, 85, db.now_iso()))
        self.connect_wave()

    def test_switch_off_hides_buttons_refuses_and_pauses_then_back_on(self):
        c = self.login("master")
        c.post("/admin/wave/toggle", data={"enabled": "0"})
        self.assertIn("Wave invoicing off", c.get("/admin/wave").get_data(as_text=True))
        html = self.login("shop_admin").get("/shop/billing?period=all").get_data(as_text=True)
        self.assertNotIn("Invoice in Wave</button>", html)
        self.assertNotIn("Check Wave for payments", html)
        html = self.login("shop_admin").post(f"/shop/billing/{self.project}/wave-invoice", follow_redirects=True,
                                             data={"customer_name": "Owner"}).get_data(as_text=True)
        self.assertIn("Wave invoicing is turned off", html)
        self.assertEqual(self.wave.created_items(), [])
        app_module._wave_last_sync[0] = 0
        self.exec("""INSERT INTO wave_invoices (kind, ref_id, wave_invoice_id, account, created_at)
                     VALUES ('project', ?, 'INV9', 1, ?)""", (self.project, db.now_iso()))
        app_module.run_wave_payment_check()
        self.assertEqual([q for q, v in self.wave.calls if "invoice(id:" in q], [])
        # Settings survived; back on, the button is back.
        self.login("master").post("/admin/wave/toggle", data={"enabled": "1"})
        html = self.login("shop_admin").get("/shop/billing?period=all").get_data(as_text=True)
        self.assertIn("Check Wave for payments", html)

    def test_connection_test_reports_each_check(self):
        c = self.login("master")
        html = c.post("/admin/wave/test/1", follow_redirects=True).get_data(as_text=True)
        self.assertIn("everything checks out", html)
        self.assertIn("Business found: Winds Aloft LLC", html)
        self.assertIn("Flight School: flight training goes under", html)
        self.assertEqual([q for q, v in self.wave.calls if "mutation" in q], [])  # read-only
        self.exec("UPDATE app_settings SET value = 'P-OLD' WHERE key = 'wave_acct1_parts_product_id'")
        html = c.post("/admin/wave/test/1", follow_redirects=True).get_data(as_text=True)
        self.assertIn("something needs fixing", html)
        self.assertIn("parts product is gone or archived", html)

    def test_connection_test_without_a_token(self):
        html = self.login("master").post("/admin/wave/test/3", follow_redirects=True).get_data(as_text=True)
        self.assertIn("No access token saved", html)

    def test_test_invoice_is_a_draft_sent_to_no_one(self):
        html = self.login("master").post("/admin/wave/test/1/invoice", follow_redirects=True).get_data(as_text=True)
        self.assertIn("made in Wave as a draft", html)
        create = [v for q, v in self.wave.calls if "invoiceCreate" in q][0]["input"]
        self.assertEqual(create["status"], "DRAFT")
        self.assertEqual(create["items"][0]["unitPrice"], 1.0)
        self.assertEqual([q for q, v in self.wave.calls if "invoiceSend" in q], [])
        self.assertIsNone(self.q1("SELECT 1 FROM wave_invoices"))  # not tied to any job

    def test_switch_and_tests_are_master_admin_only(self):
        c = self.login("shop_admin")
        c.post("/admin/wave/toggle", data={"enabled": "0"})
        c.post("/admin/wave/test/1/invoice")
        conn = db.get_db()
        self.assertTrue(wave_billing.is_enabled(wave_billing.get_settings(conn)))
        conn.close()
        self.assertEqual(self.wave.created_items(), [])
