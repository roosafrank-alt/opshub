"""QA finding ux-shop-stat-boxes-by-role: on the shop dashboard, Active
Projects and Low Stock Items looked exactly like Pending Orders and Upcoming
Projects but didn't open anything when tapped, and a Tech's Pending Orders
box opened Orders - a page Techs can't see, so it just bounced them back.
Every box now opens its list (with a small arrow to show it), and a Tech
sees Open Squawks instead of Pending Orders."""
from harness import OpsHubTestCase
import db


class ShopStatBoxesByRoleTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        self.part_id = self.exec(
            "INSERT INTO parts (name, barcode, qty_on_hand, reorder_point, unit, unit_cost, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("Oil Filter", "OILFLT1", 1, 5, "ea", 10.0, db.now_iso(), db.now_iso()))

    def test_active_projects_and_low_stock_boxes_are_now_links(self):
        c = self.login("shop_admin")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn(f'href="{"/projects"}"', body)
        self.assertIn('href="/parts?low_stock=1"', body)

    def test_admin_sees_pending_orders_not_open_squawks(self):
        c = self.login("shop_admin")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Pending Orders", body)
        self.assertNotIn("Open Squawks", body)

    def test_tech_sees_open_squawks_count(self):
        self.exec(
            "INSERT INTO plane_squawks (asset_id, notes, reported_at) VALUES (?,?,?)",
            (self.asset_id, "Left brake soft", db.now_iso()))
        c = self.login("tech")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Open Squawks", body)
        self.assertIn('href="/squawks"', body)

    def test_low_stock_link_opens_a_filtered_parts_list(self):
        c = self.login("shop_admin")
        body = c.get("/parts?low_stock=1").get_data(as_text=True)
        self.assertIn("Low Stock Items", body)
        self.assertIn("Oil Filter", body)

    def test_low_stock_filtered_list_excludes_well_stocked_parts(self):
        self.exec(
            "INSERT INTO parts (name, barcode, qty_on_hand, reorder_point, unit, unit_cost, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("Spark Plug", "SPARK1", 50, 5, "ea", 8.0, db.now_iso(), db.now_iso()))
        c = self.login("shop_admin")
        body = c.get("/parts?low_stock=1").get_data(as_text=True)
        self.assertIn("Oil Filter", body)
        self.assertNotIn("Spark Plug", body)
