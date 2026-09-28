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
        self.invoices = {}
        self.fail_send = False

    def __call__(self, token, query, variables=None, timeout=20):
        variables = variables or {}
        self.calls.append((query, variables))
        if "businesses(" in query:
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
            inv = {"id": f"INV{n}", "invoiceNumber": str(100 + n), "status": "SAVED",
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
            return {"business": {"invoice": self.invoices.get(variables["invoiceId"])}}
        raise AssertionError("unexpected Wave query: " + query)

    def pay(self, inv_id):
        inv = self.invoices[inv_id]
        inv["status"] = "PAID"
        inv["amountPaid"], inv["amountDue"] = inv["total"], {"value": "0"}

    def created_items(self):
        return [v["input"]["items"] for q, v in self.calls if "invoiceCreate" in q]


class WaveTestBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.wave = FakeWave()
        patcher = mock.patch.object(wave_billing, "_gql", self.wave)
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect_wave(self):
        conn = db.get_db()
        wave_billing.save_settings(conn, {"wave_access_token": "tok", "wave_business_id": "BIZ1",
                                          "wave_labor_product_id": "P-LABOR", "wave_parts_product_id": "P-PARTS",
                                          "wave_flight_product_id": "P-FLIGHT"})
        conn.close()


class AdminWaveTest(WaveTestBase):
    def test_setup_flow_token_then_business_then_products(self):
        c = self.login("master")
        html = c.get("/admin/wave").get_data(as_text=True)
        self.assertIn("Not set up", html)
        c.post("/admin/wave", data={"wave_access_token": "secret-token", "wave_business_id": ""})
        html = c.get("/admin/wave").get_data(as_text=True)
        self.assertIn("Winds Aloft LLC", html)
        self.assertNotIn("secret-token", html)  # never echoed back
        c.post("/admin/wave", data={"wave_access_token": "", "wave_business_id": "BIZ1"})
        html = c.get("/admin/wave").get_data(as_text=True)
        self.assertIn("Connected", html)
        self.assertIn("Flight training", html)
        self.assertNotIn("Old thing", html)  # archived products aren't offered
        c.post("/admin/wave", data={"wave_access_token": "", "wave_business_id": "BIZ1",
                                    "wave_labor_product_id": "P-LABOR", "wave_parts_product_id": "P-PARTS",
                                    "wave_flight_product_id": "P-FLIGHT"})
        conn = db.get_db()
        s = wave_billing.get_settings(conn)
        conn.close()
        self.assertEqual(s["wave_access_token"], "secret-token")  # blank kept the saved one
        self.assertEqual(s["wave_parts_product_id"], "P-PARTS")

    def test_switching_business_clears_products(self):
        self.connect_wave()
        c = self.login("master")
        c.post("/admin/wave", data={"wave_business_id": "BIZ2", "wave_labor_product_id": "P-LABOR"})
        conn = db.get_db()
        s = wave_billing.get_settings(conn)
        conn.close()
        self.assertEqual(s["wave_business_id"], "BIZ2")
        self.assertEqual(s["wave_labor_product_id"], "")

    def test_disconnect(self):
        self.connect_wave()
        c = self.login("master")
        c.post("/admin/wave/disconnect")
        conn = db.get_db()
        self.assertFalse(wave_billing.is_connected(wave_billing.get_settings(conn)))
        conn.close()

    def test_only_master_admin(self):
        c = self.login("shop_admin")
        r = c.get("/admin/wave")
        self.assertNotEqual(r.status_code, 200)


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
        self.assertIn("Wave invoice #101 created for $272.50 and emailed to owner@example.com", html)
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
        self.exec("UPDATE app_settings SET value = '' WHERE key = 'wave_labor_product_id'")
        html = self.invoice().get_data(as_text=True)
        self.assertIn("product to use for labor", html)
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
        self.assertIn("created for 2 flights, $525.00", html)
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
