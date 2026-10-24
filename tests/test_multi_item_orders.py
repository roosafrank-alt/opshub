"""New Order with several items, several tracking numbers, and a new
project made right from the order form (idea "new order")."""
from werkzeug.datastructures import MultiDict

from harness import OpsHubTestCase


class MultiItemOrderTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.part = self.make_part(name="Oil Filter", barcode="P-OF", qty=2)
        self.c = self.login("shop_admin")

    def post_new(self, pairs):
        return self.c.post("/orders/new", data=MultiDict(pairs))

    def test_one_order_many_items_and_packages(self):
        r = self.post_new([
            ("part_id", str(self.part)), ("description", ""), ("qty_ordered", "3"), ("unit_cost", "12"),
            ("is_exchange", ""), ("core_charge", ""), ("core_days", "30"),
            ("part_id", ""), ("description", "Magneto"), ("qty_ordered", "1"), ("unit_cost", "400"),
            ("is_exchange", "on"), ("core_charge", "150"), ("core_days", "15"),
            ("part_id", ""), ("description", ""), ("qty_ordered", "1"), ("unit_cost", "0"),  # blank row, skipped
            ("is_exchange", ""), ("core_charge", ""), ("core_days", "30"),
            ("supplier", "Aircraft Spruce"),
            ("tracking_number", "1Z999AA10123456784"), ("tracking_carrier", ""),
            ("tracking_number", "9400111899223856924871"), ("tracking_carrier", "usps"),
            ("tracking_number", ""), ("tracking_carrier", ""),
        ])
        self.assertEqual(r.status_code, 302)
        lines = self.q("SELECT * FROM orders ORDER BY id")
        self.assertEqual([(l["description"], l["qty_ordered"], l["supplier"]) for l in lines],
                         [("Oil Filter", 3, "Aircraft Spruce"), ("Magneto", 1, "Aircraft Spruce")])
        self.assertEqual((lines[1]["is_exchange"], lines[1]["core_charge"], lines[1]["core_days"]), (1, 150, 15))
        self.assertEqual(lines[0]["is_exchange"], 0)
        self.assertEqual(lines[0]["batch_id"], lines[1]["batch_id"])
        ships = self.q("SELECT * FROM order_shipments WHERE batch_id = ? ORDER BY id", (lines[0]["batch_id"],))
        self.assertEqual([s["tracking_number"] for s in ships], ["1Z999AA10123456784", "9400111899223856924871"])
        html = self.c.get("/orders").get_data(as_text=True)
        self.assertIn("2 packages", html)
        # Each item is still received on its own.
        self.c.post(f"/orders/{lines[0]['id']}/receive")
        self.assertEqual(self.qty(self.part), 5)
        self.assertEqual(self.q1("SELECT status FROM orders WHERE id = ?", (lines[1]["id"],))["status"], "pending")

    def test_bad_item_saves_nothing(self):
        self.post_new([("description", "Tire"), ("qty_ordered", "1"),
                       ("description", "Tube"), ("qty_ordered", "-2")])
        self.assertEqual(self.q("SELECT id FROM orders"), [])

    def test_new_project_from_order_form(self):
        asset = self.make_asset("N77")
        r = self.post_new([("description", "Brake pads"), ("qty_ordered", "2"), ("project_id", "__new__"),
                           ("new_project_name", "Brakes - N77"), ("new_project_asset_id", str(asset))])
        self.assertEqual(r.status_code, 302)
        proj = self.q1("SELECT * FROM projects WHERE name = 'Brakes - N77'")
        self.assertEqual((proj["status"], proj["asset_id"]), ("active", asset))
        self.assertEqual(self.q1("SELECT project_id FROM orders")["project_id"], proj["id"])

    def test_new_project_needs_a_name(self):
        self.post_new([("description", "Brake pads"), ("project_id", "__new__"), ("new_project_name", " ")])
        self.assertEqual(self.q("SELECT id FROM orders"), [])

    def test_edit_changes_shared_tracking_numbers(self):
        self.post_new([("description", "A"), ("description", "B"),
                       ("tracking_number", "1Z999AA10123456784")])
        a, b = self.q("SELECT * FROM orders ORDER BY id")
        page = self.c.get(f"/orders/{a['id']}/edit").get_data(as_text=True)
        self.assertIn("one of 2 on the same order", page)
        self.assertIn("1Z999AA10123456784", page)
        self.c.post(f"/orders/{a['id']}/edit", data=MultiDict([
            ("description", "A"), ("qty_ordered", "1"), ("unit_cost", "0"),
            ("tracking_number", "1Z999AA10123456784"), ("tracking_carrier", ""),
            ("tracking_number", "123456789012"), ("tracking_carrier", "fedex")]))
        self.assertEqual(len(self.q("SELECT id FROM order_shipments WHERE batch_id = ?", (b["batch_id"],))), 2)
        d = self.c.get(f"/api/orders/{b['id']}/tracking").get_json()
        self.assertTrue(d["ok"])

    def test_receive_or_cancel_one_tracking_number(self):
        self.post_new([("description", "A"), ("qty_ordered", "1"), ("description", "B"), ("qty_ordered", "1"),
                       ("tracking_number", "1Z999AA10123456784"), ("tracking_number", "123456789012")])
        s1, s2 = self.q("SELECT * FROM order_shipments ORDER BY id")
        self.c.post(f"/orders/shipments/{s1['id']}/receive")
        self.c.post(f"/orders/shipments/{s2['id']}/cancel")
        st = [r["status"] for r in self.q("SELECT status FROM order_shipments ORDER BY id")]
        self.assertEqual(st, ["received", "cancelled"])
        html = self.c.get("/orders").get_data(as_text=True)
        self.assertIn("Received", html)
        self.c.post(f"/orders/shipments/{s1['id']}/reopen")
        self.assertIsNone(self.q1("SELECT status FROM order_shipments WHERE id = ?", (s1["id"],))["status"])
        # The order's items and other package are untouched.
        self.assertEqual([r["status"] for r in self.q("SELECT status FROM orders")], ["pending", "pending"])
        self.assertEqual(self.c.post("/orders/shipments/%d/bogus" % s1["id"]).status_code, 404)
