"""'Things to Order' is now a 'To order' tab on Orders: opens first when
anything is on it (with a red count), otherwise Pending opens as before."""
from harness import OpsHubTestCase


class ToOrderTabTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("shop_admin")

    def page(self, url="/orders"):
        return self.client.get(url).get_data(as_text=True)

    def test_empty_list_opens_on_pending_with_no_count(self):
        html = self.page()
        self.assertNotIn("Things to Order", html)
        self.assertIn('class="nav-link " href="/orders?status=to_order', html.replace("nav-link  ", "nav-link "))
        self.assertNotIn('id="to-order"', html)
        self.assertNotIn("bg-danger\">1<", html)

    def test_add_item_opens_to_order_tab_with_count(self):
        r = self.client.post("/orders/wishlist/new", data=dict(description="Safety wire", urgency="needed_now"))
        self.assertIn("status=to_order", r.headers["Location"])
        html = self.page()
        self.assertIn('id="to-order"', html)
        self.assertIn("Safety wire", html)
        self.assertIn('<span class="badge bg-danger">1</span>', html)
        self.assertNotIn("Things to Order", html)
        self.assertNotIn("By Vendor", html)

    def test_other_tabs_still_work_with_items_waiting(self):
        self.client.post("/orders/wishlist/new", data=dict(description="Safety wire"))
        html = self.page("/orders?status=pending")
        self.assertNotIn('id="to-order"', html)
        self.assertIn("By Vendor", html)

    def test_add_form_defaults_to_needed_now(self):
        self.client.post("/orders/wishlist/new", data=dict(description="x"))
        self.assertIn('<option value="needed_now" selected>', self.page("/orders?status=to_order"))
