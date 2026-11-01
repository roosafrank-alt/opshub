"""Manage menu: Laborers, Print Worker Badges and General Shop Code are one
'Workers' item; the badges and shop code pages are reached from the Workers
page and go back to it (idea "Simplify: merge Laborers, ... into one Workers")."""
from harness import OpsHubTestCase


class WorkersMenuTest(OpsHubTestCase):
    def test_admin_menu_has_one_workers_item(self):
        # Frank 2026-10-16: no Workers button (set up in Admin > Accounts); badges and the shop code stay.
        html = self.login("shop_admin").get("/laborers").get_data(as_text=True)
        self.assertNotIn("</i> Workers</a>", html.split("</nav>")[0])
        self.assertNotIn("</i> Laborers</a>", html)
        # QA feat-badges-shop-code-one-page: the two items became one.
        self.assertIn("</i> Badges &amp; Shop Code</a>", html)
        self.assertNotIn("</i> Print Worker Badges</a>", html)
        self.assertNotIn("</i> General Shop Code</a>", html)

    def test_workers_page_has_badge_and_code_buttons(self):
        html = self.login("shop_admin").get("/laborers").get_data(as_text=True)
        self.assertIn("/labor/badges", html)
        # SHOP-23 / feat-badges-shop-code-one-page: one Print-dropdown item now.
        self.assertIn("Badges &amp; Shop Code</a>", html)
        self.assertIn("Add Worker", html)

    def test_one_page_has_shop_code_first_and_old_address_forwards(self):
        c = self.login("shop_admin")
        html = c.get("/labor/badges").get_data(as_text=True)
        self.assertIn("Badges &amp; Shop Code", html)
        self.assertIn("GENERAL-SHOP", html)
        self.assertLess(html.index("Shop / General Time"), html.index("Print page (regular printer)") + 10_000)
        self.assertNotIn("</i> Workers</a>", html.split("</nav>")[-1])
        r = c.get("/labor/general-code")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/labor/badges"))
        t = self.login("tech").get("/labor/badges")
        self.assertEqual(t.status_code, 200)
