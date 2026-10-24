"""QA finding feat-shop-needs-you-list: the shop home shows one 'Needs you'
list (one line + one button per waiting item) instead of five coloured
boxes, and hides it when nothing is waiting."""
from harness import OpsHubTestCase, seed_row
import db


class ShopNeedsYouListTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N555NY")

    def test_hidden_when_nothing_waiting(self):
        body = self.login("shop_admin").get("/shop").get_data(as_text=True)
        self.assertNotIn("Needs you", body)

    def test_new_squawk_and_waiting_footer_one_line_each(self):
        conn = db.get_db()
        seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Left brake soft")
        seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Door seal",
                 assigned_to=self.users["tech"]["id"], acknowledged_at=db.now_iso(), acknowledged_by="Admin")
        conn.commit()
        conn.close()
        body = self.login("shop_admin").get("/shop").get_data(as_text=True)
        self.assertIn('id="needs-you"', body)
        self.assertEqual(body.count("&middot; New squawk"), 1)
        self.assertIn("Left brake soft", body)
        self.assertIn("Waiting on others: 1 squawk not accepted yet (N555NY)", body)
        self.assertNotIn("Pick Who Fixes Them", body)

    def test_only_waiting_on_others_still_shows_card_without_count(self):
        conn = db.get_db()
        seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Door seal",
                 assigned_to=self.users["tech"]["id"], acknowledged_at=db.now_iso(), acknowledged_by="Admin")
        conn.commit()
        conn.close()
        body = self.login("shop_admin").get("/shop").get_data(as_text=True)
        self.assertIn("Needs you", body)
        self.assertNotIn("badge bg-danger align-middle", body)

    def test_inspector_sees_only_sign_off_line(self):
        conn = db.get_db()
        seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Right brake dragging",
                 assigned_to=self.users["tech"]["id"], acknowledged_at=db.now_iso(), acknowledged_by="Tech",
                 worker_acknowledged_at=db.now_iso(),
                 repair_confirm_requested_at=db.now_iso(), repair_confirm_requested_by="Tech")
        seed_row(conn, "plane_squawks", asset_id=self.asset_id, notes="Left brake soft")
        conn.commit()
        conn.close()
        body = self.login("inspector").get("/shop").get_data(as_text=True)
        self.assertIn("Repair to sign off", body)
        self.assertNotIn("&middot; New squawk", body)
