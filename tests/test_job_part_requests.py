"""Idea "Techs ask for a part right from the job": a tech taps Need a part on
a job, the request lands on the admin's To order list with the job and
urgency filled in, the job shows 'Waiting on parts' with each part's step
(Asked / On order / Arrived), and when the order is received the tech who
asked gets an alert on the shop home.
"""
from harness import OpsHubTestCase


class JobPartRequestsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.proj = self.make_project("Annual - N1")

    def _ask(self, role="tech", **kw):
        data = {"description": "Brake pad set", "urgency": "rush"}
        data.update(kw)
        return self.login(role).post(f"/projects/{self.proj}/part-request", data=data, follow_redirects=True)

    def test_tech_request_lands_on_the_list_with_job_and_urgency(self):
        r = self._ask()
        self.assertIn(b"Asked the office", r.data)
        w = self.q1("SELECT * FROM order_wishlist WHERE description = 'Brake pad set'")
        self.assertEqual(w["project_id"], self.proj)
        self.assertEqual(w["urgency"], "rush")
        self.assertEqual(w["status"], "open")
        html = self.login("shop_admin").get("/orders?status=to_order").get_data(as_text=True)
        self.assertIn("Brake pad set", html)

    def test_job_shows_waiting_tag_and_step(self):
        self._ask()
        html = self.login("tech").get(f"/projects/{self.proj}").get_data(as_text=True)
        self.assertIn("Waiting on parts", html)
        self.assertIn("Asked", html)

    def test_blank_request_is_refused(self):
        r = self._ask(description="  ")
        self.assertIn(b"Type what part", r.data)
        self.assertIsNone(self.q1("SELECT id FROM order_wishlist"))

    def test_order_then_receive_marks_arrived_and_alerts_the_tech(self):
        self._ask()
        wid = self.q1("SELECT id FROM order_wishlist")["id"]
        admin = self.login("shop_admin")
        admin.post("/orders/new", data={"wishlist_id": str(wid), "supplier": "Aircraft Spruce",
                   "project_id": str(self.proj), "expected_date": "2030-01-05",
                   "description": ["Brake pad set"], "qty_ordered": ["1"], "unit_cost": ["10"], "part_id": [""]})
        order = self.q1("SELECT * FROM orders WHERE wishlist_id = ?", (wid,))
        self.assertIsNotNone(order)
        html = self.login("tech").get(f"/projects/{self.proj}").get_data(as_text=True)
        self.assertIn("On order", html)
        self.assertIn("Waiting on parts", html)
        self.login("shop_admin").post(f"/orders/{order['id']}/receive")
        html = self.login("tech").get(f"/projects/{self.proj}").get_data(as_text=True)
        self.assertNotIn("Waiting on parts", html)
        self.assertIn("Arrived", html)
        home = self.login("tech").get("/shop").get_data(as_text=True)
        self.assertIn("Your part arrived", home)
        # another tech is not told
        self.assertNotIn("Your part arrived", self.login("shop_admin").get("/shop").get_data(as_text=True))
        self.login("tech").post(f"/part-requests/{wid}/seen")
        self.assertNotIn("Your part arrived", self.login("tech").get("/shop").get_data(as_text=True))
