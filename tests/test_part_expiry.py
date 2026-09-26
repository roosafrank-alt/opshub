"""Optional shelf-life expiration dates on parts (QA feat-shelf-life-expiry)."""
from datetime import date, timedelta

from harness import OpsHubTestCase


def d(days):
    return (date.today() + timedelta(days=days)).isoformat()


class PartExpiryTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.part = self.make_part(name="RTV Sealant", barcode="PART-RTV", qty=3)
        self.plain = self.make_part(name="Washer", barcode="PART-W", qty=10)
        self.project = self.make_project()
        self.login("shop_admin")

    def set_exp(self, days):
        self.exec("UPDATE parts SET expiration_date = ? WHERE id = ?", (d(days) if days is not None else None, self.part))

    def test_no_dates_means_nothing_shows(self):
        html = self.client.get("/parts").get_data(as_text=True)
        self.assertNotIn("Expiration Dates", html)
        self.assertNotIn("Expires soon", html)

    def test_edit_sets_optional_date_and_tile_and_list_appear(self):
        form = dict(name="RTV Sealant", unit="ea", reorder_point="0", unit_cost="5", sell_price="8",
                    expiration_date=d(20))
        self.client.post(f"/parts/{self.part}/edit", data=form)
        self.assertEqual(self.q1("SELECT expiration_date FROM parts WHERE id=?", (self.part,))["expiration_date"], d(20))
        html = self.client.get("/parts").get_data(as_text=True)
        self.assertIn("Expiration Dates", html)
        self.assertIn("Expires soon", html)
        lst = self.client.get("/parts/expiring").get_data(as_text=True)
        self.assertIn("RTV Sealant", lst)
        self.assertNotIn("Washer", lst)
        self.assertIn("20 days left", lst)
        # Clearing it removes everything again.
        form["expiration_date"] = ""
        self.client.post(f"/parts/{self.part}/edit", data=form)
        self.assertIsNone(self.q1("SELECT expiration_date FROM parts WHERE id=?", (self.part,))["expiration_date"])

    def test_bad_date_rejected(self):
        self.client.post(f"/parts/{self.part}/edit", data=dict(name="RTV Sealant", expiration_date="2026-13-40"))
        self.assertIsNone(self.q1("SELECT expiration_date FROM parts WHERE id=?", (self.part,))["expiration_date"])

    def test_expired_list_order(self):
        self.set_exp(-5)
        self.exec("UPDATE parts SET expiration_date = ? WHERE id = ?", (d(100), self.plain))
        lst = self.client.get("/parts/expiring").get_data(as_text=True)
        self.assertLess(lst.index("RTV Sealant"), lst.index("Washer"))
        self.assertIn("Expired 5 days ago", lst)

    def test_scan_out_expired_needs_confirmation_and_is_noted(self):
        self.set_exp(-3)
        body = dict(barcode="PART-RTV", action="out", qty=1, project_id=self.project, performed_by="Tech")
        r = self.client.post("/api/scan", json=body)
        self.assertEqual((r.status_code, r.json["error"]), (409, "expired"))
        self.assertEqual(self.qty(self.part), 3)
        r = self.client.post("/api/scan", json=dict(body, confirm_expired=True))
        self.assertTrue(r.json["ok"])
        self.assertEqual(self.qty(self.part), 2)
        t = self.q1("SELECT note FROM transactions WHERE part_id=? AND type='out'", (self.part,))
        self.assertIn("past expiration", t["note"])

    def test_not_expired_scans_normally(self):
        self.set_exp(10)
        r = self.client.post("/api/scan", json=dict(barcode="PART-RTV", action="out", qty=1,
                                                    project_id=self.project, performed_by="Tech"))
        self.assertTrue(r.json["ok"])

    def test_project_page_assign_expired_needs_confirm(self):
        self.set_exp(-1)
        form = dict(part_id=str(self.part), qty="1", performed_by="Tech")
        self.client.post(f"/projects/{self.project}/add_part", data=form)
        self.assertEqual(self.qty(self.part), 3)
        self.client.post(f"/projects/{self.project}/add_part", data=dict(form, confirm_expired="1"))
        self.assertEqual(self.qty(self.part), 2)

    def test_receiving_dated_part_asks_for_new_date(self):
        self.set_exp(5)
        self.client.post("/orders/new", data=dict(description="", part_id=str(self.part), qty_ordered="2"))
        oid = self.q1("SELECT id FROM orders ORDER BY id DESC LIMIT 1")["id"]
        r = self.client.post(f"/orders/{oid}/receive")
        self.assertIn(f"/parts/{self.part}/expiration", r.location)
        self.client.post(f"/parts/{self.part}/expiration", data=dict(expiration_date=d(365)))
        self.assertEqual(self.q1("SELECT expiration_date FROM parts WHERE id=?", (self.part,))["expiration_date"], d(365))
        # A part with no date just goes back to Orders.
        self.client.post("/orders/new", data=dict(description="", part_id=str(self.plain), qty_ordered="2"))
        oid = self.q1("SELECT id FROM orders ORDER BY id DESC LIMIT 1")["id"]
        self.assertNotIn("expiration", self.client.post(f"/orders/{oid}/receive").location)
