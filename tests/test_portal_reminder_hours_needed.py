"""QA finding ux-owner-reminder-hours: on the owner's plane page a reminder
that can't be worked out yet (no hours reading) showed a green OK. It now
shows a grey "Hours needed" badge, like the shop's own page."""
from harness import OpsHubTestCase


class PortalReminderHoursNeededTest(OpsHubTestCase):
    def test_reminder_without_hours_reading_is_not_green_ok(self):
        asset = self.make_asset("N333HN")
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, asset))
        self.exec("INSERT INTO maintenance_items (asset_id, name, type, interval_hours) VALUES (?,?,?,?)",
                  (asset, "100-hour inspection", "hours", 100))
        html = self.login("customer").get(f"/portal/aircraft/{asset}").get_data(as_text=True)
        self.assertIn("100-hour inspection", html)
        self.assertIn("Needs an hours reading", html)
        self.assertIn('<span class="badge bg-secondary">Hours needed</span>', html)
        self.assertNotIn('<span class="badge bg-success">OK</span>', html)
