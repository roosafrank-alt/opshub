"""Manage menu: Laborers, Print Worker Badges and General Shop Code are one
'Workers' item; the badges and shop code pages are reached from the Workers
page and go back to it (idea "Simplify: merge Laborers, ... into one Workers")."""
from harness import OpsHubTestCase


class WorkersMenuTest(OpsHubTestCase):
    def test_admin_menu_has_one_workers_item(self):
        html = self.login("shop_admin").get("/laborers").get_data(as_text=True)
        self.assertIn("</i> Workers</a>", html)
        self.assertNotIn("</i> Laborers</a>", html)
        self.assertNotIn("</i> Print Worker Badges</a>", html.split("Worker Badges")[0])
        self.assertNotIn("</i> General Shop Code</a>", html)

    def test_workers_page_has_badge_and_code_buttons(self):
        html = self.login("shop_admin").get("/laborers").get_data(as_text=True)
        self.assertIn("/labor/badges", html)
        self.assertIn("/labor/general-code", html)
        # SHOP-23: both live in a Print dropdown now.
        self.assertIn("Worker Badges</a>", html)
        self.assertIn("General Shop Code</a>", html)
        self.assertIn("Add Worker", html)

    def test_badge_and_code_pages_go_back_to_workers(self):
        c = self.login("shop_admin")
        for path in ("/labor/badges", "/labor/general-code"):
            self.assertIn("</i> Workers</a>", c.get(path).get_data(as_text=True))
