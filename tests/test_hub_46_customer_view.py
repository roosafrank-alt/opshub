"""HUB-46: a Winds Aloft admin can look at a customer's portal, look only."""
from harness import OpsHubTestCase


class CustomerViewTest(OpsHubTestCase):
    def test_chip_and_picker_for_admins_only(self):
        html = self.login("master").get("/shop").get_data(as_text=True)
        self.assertIn("/portal/view-as", html)
        self.assertIn("Look at a customer", self.login("master").get("/portal/view-as").get_data(as_text=True))
        for role in ("tech", "cfi", "customer"):
            c = self.login(role)
            self.assertEqual(c.get("/portal/view-as").status_code, 302, role)
        self.assertNotIn("/portal/view-as", self.login("tech").get("/shop").get_data(as_text=True))

    def test_look_only_view_of_a_customers_portal(self):
        c = self.login("master")
        r = c.post(f"/portal/view-as/{self.customer_id}")
        self.assertEqual(r.status_code, 302)
        page = c.get("/portal/", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Looking at Owner Customer", page)
        self.assertIn("Look only", page)
        # nothing can be changed: any POST in the portal is refused with a message
        r = c.post("/portal/account", data={"name": "Hacked", "email": "owner@example.com"}, follow_redirects=True)
        self.assertIn("nothing was changed", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT name FROM customers WHERE id = ?", (self.customer_id,))["name"], "Owner Customer")
        # exit
        c.post("/portal/view-as/exit")
        self.assertNotIn("Looking at", c.get("/portal/view-as").get_data(as_text=True))

    def test_a_customer_cannot_use_it_and_it_is_never_in_their_own_session(self):
        c = self.login("customer")
        c.post(f"/portal/view-as/{self.customer_id}")
        page = c.get("/portal/", follow_redirects=True).get_data(as_text=True)
        self.assertNotIn("Looking at", page)

    def test_view_as_does_not_open_winds_aloft_to_anyone_else(self):
        c = self.login("tech")
        c.post(f"/portal/view-as/{self.customer_id}")
        self.assertEqual(c.get("/portal/").status_code, 302)   # tech is not a customer and not an admin
