"""QA finding feat-owner-book-due-item: an owner taps "Book it" on a due
reminder in My Aircraft; that makes an undated job on the plane carrying the
week they asked for (customer.customer_book_item). It shows as Requested under
Appointments and on the shop dashboard until the shop sets a date."""
import db
import customer
from harness import OpsHubTestCase, seed_row


class OwnerBookDueItemTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        conn = db.get_db()
        self.asset = seed_row(conn, "assets", tag="N4729K", name="Cessna 172S", created_at=db.now_iso(), updated_at=db.now_iso())
        self.other = seed_row(conn, "assets", tag="N1OTHER", name="Other", created_at=db.now_iso(), updated_at=db.now_iso())
        conn.execute("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, self.asset))
        self.item = seed_row(conn, "maintenance_items", asset_id=self.asset, name="Annual Inspection", type="calendar",
                             interval_days=365, last_done_date="2024-01-01")
        self.ok_item = seed_row(conn, "maintenance_items", asset_id=self.asset, name="Fresh Item", type="calendar",
                                interval_days=3650, last_done_date=db.now_iso()[:10])
        self.other_item = seed_row(conn, "maintenance_items", asset_id=self.other, name="Not Yours", type="calendar",
                                   interval_days=365, last_done_date="2024-01-01")
        conn.commit()
        conn.close()
        self.week = customer._week_options()[1][0]

    def _book(self, item=None, asset=None, week=None):
        return self.client.post(f"/portal/aircraft/{asset or self.asset}/book/{item or self.item}",
                                data={"week": week or self.week}, follow_redirects=True)

    def test_button_only_on_due_items(self):
        self.login("customer")
        html = self.client.get(f"/portal/aircraft/{self.asset}").get_data(as_text=True)
        self.assertEqual(html.count("Book it"), 1)

    def test_book_creates_request_and_shows_requested(self):
        self.login("customer")
        html = self._book().get_data(as_text=True)
        self.assertIn("Requested", html)
        p = self.q1("SELECT * FROM projects WHERE asset_id = ?", (self.asset,))
        self.assertEqual(p["name"], "Annual Inspection")
        self.assertEqual(p["customer_requested_week"], self.week)
        self.assertIsNone(p["scheduled_date"])
        self.assertNotIn("Book it", html)

    def test_double_tap_makes_one_request(self):
        self.login("customer")
        self._book()
        self._book()
        self.assertEqual(len(self.q("SELECT id FROM projects WHERE asset_id = ?", (self.asset,))), 1)

    def test_cannot_book_someone_elses_plane_or_item(self):
        self.login("customer")
        self.assertEqual(self._book(item=self.other_item, asset=self.other).status_code, 404)
        self.assertEqual(self._book(item=self.other_item).status_code, 404)
        self.assertEqual(len(self.q("SELECT id FROM projects")), 0)

    def test_shop_sees_request_until_dated(self):
        self.login("customer")
        self._book()
        self.login("shop_admin")
        self.assertIn("Owner Booking Request", self.client.get("/shop").get_data(as_text=True))
        self.exec("UPDATE projects SET scheduled_date = '2030-01-07' WHERE asset_id = ?", (self.asset,))
        self.assertNotIn("Owner Booking Request", self.client.get("/shop").get_data(as_text=True))
