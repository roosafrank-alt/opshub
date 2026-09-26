"""Order tracking without a Shippo key (idea "orders" edit): no "isn't
switched on" error, just the number and a link to the carrier."""
from harness import OpsHubTestCase
import tracking


class TrackingWithoutKeyTest(OpsHubTestCase):
    def test_orders_page_has_no_switched_on_error_and_links_to_carrier(self):
        self.assertFalse(tracking.SHIPPO_API_KEY)
        c = self.login("shop_admin")
        c.post("/orders/new", data=dict(description="Spark plugs", qty_ordered="4",
                                        tracking_number="1Z999AA10123456784"))
        html = c.get("/orders").get_data(as_text=True)
        self.assertNotIn("switched on", html)
        self.assertIn("1Z999AA10123456784", html)
        oid = self.q1("SELECT id FROM orders")["id"]
        d = c.get(f"/api/orders/{oid}/tracking").get_json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["live"])
        self.assertIn("ups.com", d["carrier_url"])
