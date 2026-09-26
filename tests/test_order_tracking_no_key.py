"""Order tracking without a Shippo key (idea "orders" edit): no "isn't
switched on" error, just the number and a link to the carrier."""
from harness import OpsHubTestCase
import db
import tracking


class TrackingWithoutKeyTest(OpsHubTestCase):
    def test_orders_page_has_no_switched_on_error_and_links_to_carrier(self):
        self.assertFalse(tracking.SHIPPO_API_KEY)
        conn = db.get_db()
        conn.execute("INSERT INTO orders (description, qty_ordered, status, tracking_number, created_at) "
                     "VALUES ('Spark plugs', 4, 'pending', '1Z999AA10123456784', ?)", (db.now_iso(),))
        conn.commit(); conn.close()
        c = self.login("shop_admin")
        html = c.get("/orders").get_data(as_text=True)
        self.assertNotIn("switched on", html)
        self.assertIn("1Z999AA10123456784", html)
        oid = self.q1("SELECT id FROM orders")["id"]
        d = c.get(f"/api/orders/{oid}/tracking").get_json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["live"])
        self.assertIn("ups.com", d["carrier_url"])
