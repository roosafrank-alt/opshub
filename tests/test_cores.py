"""Exchange cores owed back to suppliers (QA feat-core-return-tracker):
an order marked Exchange gets a core due date when received, shows under
"Cores owed" on Orders (amber within 7 days, red overdue), can be marked
shipped and credited, and shop admins are alerted before it's due.
"""
from datetime import date, timedelta

from harness import OpsHubTestCase, SIDE_EFFECTS
import db
import notify


class CoresTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.part = self.make_part(name="Starter", barcode="PART-ST", qty=0)
        self.project = self.make_project()
        self.login("shop_admin")

    def order(self, **form):
        base = dict(description="", part_id=str(self.part), qty_ordered="1", unit_cost="450",
                    supplier="Air Power", is_exchange="on", core_charge="300", core_days="30")
        base.update(form)
        self.client.post("/orders/new", data=base)
        return self.q1("SELECT * FROM orders ORDER BY id DESC LIMIT 1")

    def test_exchange_order_gets_due_date_when_received(self):
        o = self.order()
        self.assertEqual((o["is_exchange"], o["core_charge"], o["core_days"], o["core_due_date"]), (1, 300, 30, None))
        self.client.post(f"/orders/{o['id']}/receive")
        due = self.q1("SELECT core_due_date FROM orders WHERE id=?", (o["id"],))["core_due_date"]
        self.assertEqual(due, (date.today() + timedelta(days=30)).isoformat())
        html = self.client.get("/orders").get_data(as_text=True)
        self.assertIn("Cores owed", html)
        self.assertIn("30 days left", html)

    def test_plain_order_has_no_core(self):
        o = self.order(is_exchange="")
        self.client.post(f"/orders/{o['id']}/receive")
        self.assertIsNone(self.q1("SELECT core_due_date FROM orders WHERE id=?", (o["id"],))["core_due_date"])
        self.assertNotIn("Cores owed", self.client.get("/orders").get_data(as_text=True))

    def test_bad_core_values_rejected(self):
        for form in (dict(core_charge="-5"), dict(core_days="0"), dict(core_days="abc")):
            with self.subTest(**form):
                before = len(self.q("SELECT id FROM orders"))
                r = self.client.post("/orders/new", data=dict(description="Mag", qty_ordered="1", is_exchange="on",
                                                               **{"core_charge": "10", "core_days": "30", **form}))
                self.assertEqual(r.status_code, 200)
                self.assertEqual(len(self.q("SELECT id FROM orders")), before)

    def test_overdue_and_soon_colors_and_close_out(self):
        o = self.order()
        self.client.post(f"/orders/{o['id']}/receive")
        self.exec("UPDATE orders SET core_due_date=? WHERE id=?", ((date.today() - timedelta(days=2)).isoformat(), o["id"]))
        html = self.client.get("/orders").get_data(as_text=True)
        self.assertIn("2 days overdue", html)
        self.client.post(f"/orders/{o['id']}/core-shipped", data=dict(core_tracking="1z 999"))
        row = self.q1("SELECT * FROM orders WHERE id=?", (o["id"],))
        self.assertIsNotNone(row["core_shipped_at"])
        self.assertEqual(row["core_tracking"], "1Z999")
        self.client.post(f"/orders/{o['id']}/core-credited")
        self.assertIsNotNone(self.q1("SELECT core_credited_at FROM orders WHERE id=?", (o["id"],))["core_credited_at"])
        self.assertNotIn("Cores owed", self.client.get("/orders").get_data(as_text=True))

    def test_only_shop_admins_close_cores(self):
        o = self.order()
        self.client.post(f"/orders/{o['id']}/receive")
        self.login("tech").post(f"/orders/{o['id']}/core-credited")
        self.assertIsNone(self.q1("SELECT core_credited_at FROM orders WHERE id=?", (o["id"],))["core_credited_at"])

    def test_admin_alerted_once_when_due_within_7_days(self):
        o = self.order()
        self.client.post(f"/orders/{o['id']}/receive")
        self.exec("UPDATE orders SET core_due_date=? WHERE id=?", ((date.today() + timedelta(days=5)).isoformat(), o["id"]))
        self.exec("UPDATE users SET notify_email=1, email='admin@example.com' WHERE username='shop_admin'")
        conn = db.get_db()
        settings = {"smtp_host": "smtp.example.com", "smtp_port": "587", "smtp_username": "u", "smtp_password": "p",
                    "smtp_from": "shop@example.com"}
        orig = notify.send_email
        sent = []
        notify.send_email = lambda st, to, subj, body, brand=None: (sent.append(subj) or (True, None))
        try:
            self.assertEqual(notify.check_cores_due(conn, settings), 1)
            self.assertEqual(notify.check_cores_due(conn, settings), 0)  # not again
        finally:
            notify.send_email = orig
            conn.close()
        self.assertTrue(sent and "Core due soon" in sent[0])
