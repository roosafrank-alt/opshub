"""My Aircraft shows a job that is in the shop once, as one "In the shop now"
card (progress, what's left, promised date, cost so far), instead of an
Appointments row plus a Job Costs row."""
from datetime import date, timedelta
from harness import OpsHubTestCase
import db


class OwnerJobInShopTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N12345")
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, self.asset))
        self.job = self.make_project(name="Annual - N12345", asset_id=self.asset)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.exec("UPDATE projects SET scheduled_date = ? WHERE id = ?", (yesterday, self.job))
        self.exec("INSERT INTO project_sections (project_id, name, completed_at) VALUES (?, 'Oil change', ?)",
                  (self.job, db.now_iso()))
        self.exec("INSERT INTO project_sections (project_id, name) VALUES (?, 'Left main tire')", (self.job,))

    def page(self):
        return self.login("customer").get(f"/portal/aircraft/{self.asset}").get_data(as_text=True)

    def test_job_in_shop_shows_once_with_progress_and_items(self):
        html = self.page()
        self.assertIn("In the shop now", html)
        self.assertIn("1 of 2 done", html)
        self.assertIn("Oil change", html)
        self.assertIn("Left main tire", html)
        self.assertIn("On track", html)
        self.assertIn("Cost so far", html)
        # no second copy under Appointments or Job Costs
        self.assertNotIn("Nothing scheduled", html)
        self.assertEqual(html.count("Annual - N12345"), 1)

    def test_promised_date_only_when_shop_shows_it(self):
        later = (date.today() + timedelta(days=3)).isoformat()
        self.exec("UPDATE projects SET promised_date = ?, promised_show_owner = 0 WHERE id = ?", (later, self.job))
        self.assertNotIn("Promised back", self.page())
        self.exec("UPDATE projects SET promised_show_owner = 1 WHERE id = ?", (self.job,))
        self.assertIn("Promised back", self.page())

    def test_waiting_on_a_part(self):
        self.exec("INSERT INTO order_wishlist (description, project_id, status) VALUES ('Vacuum pump', ?, 'open')", (self.job,))
        html = self.page()
        self.assertIn("Waiting on part", html)
        self.assertNotIn("On track", html)

    def test_future_job_stays_under_appointments(self):
        later = (date.today() + timedelta(days=9)).isoformat()
        self.exec("UPDATE projects SET scheduled_date = ? WHERE id = ?", (later, self.job))
        html = self.page()
        self.assertNotIn("In the shop now", html)
        self.assertIn("Appointments", html)
