"""A pending order past its expected date is flagged red with 'N days late';
its supplier sorts first with a '1 late' tag and the Pending tab shows it."""
from datetime import date, timedelta
import db
from harness import OpsHubTestCase


class OrdersLateFlagTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("shop_admin")

    def add(self, supplier, expected):
        conn = db.get_db()
        conn.execute("INSERT INTO orders (description, supplier, qty_ordered, status, ordered_date, expected_date, created_at) "
                     "VALUES ('Widget', ?, 1, 'pending', '2026-01-01', ?, '2026-01-01')", (supplier, expected))
        conn.commit()
        conn.close()

    def test_late_order_flagged_and_supplier_first(self):
        today = date.today()
        self.add("Aaa Supply", (today + timedelta(days=3)).isoformat())
        self.add("Zed Parts", (today - timedelta(days=1)).isoformat())
        html = self.client.get("/orders?status=pending").get_data(as_text=True)
        self.assertIn("1 day late", html)
        self.assertIn("1 late", html)
        self.assertIn('class="table-danger"', html)
        self.assertLess(html.index("Zed Parts"), html.index("Aaa Supply"))

    def test_on_time_orders_unflagged(self):
        self.add("Aaa Supply", (date.today() + timedelta(days=3)).isoformat())
        self.add("Bbb", None)
        html = self.client.get("/orders?status=pending").get_data(as_text=True)
        self.assertNotIn("late</", html)
        self.assertNotIn('class="table-danger"', html)
