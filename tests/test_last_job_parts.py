"""Idea "New job: show the parts the plane's last job of the same kind used,
with shelf counts and 'Order what's short'"."""
from harness import OpsHubTestCase


class LastJobPartsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N12345")
        self.old = self.make_project("Annual - N12345", status="completed", asset_id=self.asset)
        self.exec("UPDATE projects SET created_at = '2025-10-05T09:00:00' WHERE id = ?", (self.old,))
        self.filter = self.make_part("Oil Filter CH48110-1", "P-1", qty=4)
        self.plugs = self.make_part("Spark plug REM38E", "P-2", qty=6)
        self.filt2 = self.make_part("Air filter BA-111", "P-3", qty=0)
        for pid, q in ((self.filter, 1), (self.plugs, 8), (self.filt2, 1)):
            self.exec("INSERT INTO transactions (part_id, project_id, type, qty, created_at) VALUES (?,?,'out',?,?)",
                      (pid, self.old, q, "2025-10-06T09:00:00"))
        self.new = self.make_project("Annual - N12345", asset_id=self.asset)

    def test_new_job_lists_last_parts_with_shelf_counts(self):
        html = self.login("tech").get(f"/projects/{self.new}").get_data(as_text=True)
        self.assertIn("Oil Filter CH48110-1", html)
        self.assertIn("Spark plug REM38E", html)
        self.assertIn("Order the 2 short", html)
        self.assertIn("last-job-parts", html)

    def test_other_plane_or_kind_shows_nothing(self):
        other = self.make_project("Annual - N99", asset_id=self.make_asset("N99"))
        html = self.login("tech").get(f"/projects/{other}").get_data(as_text=True)
        self.assertNotIn("Order the", html)
        self.assertNotIn("last-job-parts", html)

    def test_order_short_asks_once_for_each_short_part(self):
        c = self.login("tech")
        r = c.post(f"/projects/{self.new}/order-short", follow_redirects=True)
        self.assertIn(b"Asked the office for 2 short parts", r.data)
        rows = self.q("SELECT * FROM order_wishlist WHERE project_id = ?", (self.new,))
        self.assertEqual({w["part_id"] for w in rows}, {self.plugs, self.filt2})
        c.post(f"/projects/{self.new}/order-short", follow_redirects=True)
        self.assertEqual(len(self.q("SELECT id FROM order_wishlist WHERE project_id = ?", (self.new,))), 2)
        html = c.get(f"/projects/{self.new}").get_data(as_text=True)
        self.assertNotIn("Order the", html)
