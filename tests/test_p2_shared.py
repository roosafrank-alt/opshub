"""Phase 2, shared area: HUB-15 (waiting found items on the My Aircraft list),
FLY-12 (one date format, usdate / usdate_short), SEAM-15 (page titles) and the
confirm box's accent colour (DESIGN-1)."""
import re

from harness import OpsHubTestCase  # first: it points the app at a throwaway database

import app as app_module


class OwnerListWaitingItemsTest(OpsHubTestCase):
    """HUB-15: the multi-plane list never says All caught up while found
    items wait for the owner's OK."""

    def setUp(self):
        super().setUp()
        self.a1 = self.make_asset("N111AA")
        self.a2 = self.make_asset("N222BB")
        for a in (self.a1, self.a2):
            self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, a))
        self.project = self.make_project(name="Annual - N111AA", asset_id=self.a1)

    def add_found(self, status="waiting"):
        self.exec("INSERT INTO found_items (project_id, description, status, created_by, created_at) "
                  "VALUES (?, 'Cracked exhaust stack', ?, 'Tech', datetime('now'))", (self.project, status))

    def card(self, html, tag):
        parts = re.split(r'<a href="/portal/aircraft/', html)
        return next(p for p in parts if tag in p)

    def test_waiting_items_show_a_badge_and_no_all_caught_up(self):
        self.add_found()
        self.add_found()
        html = self.login("customer").get("/portal/").get_data(as_text=True)
        busy = self.card(html, "N111AA")
        self.assertIn("2 need your OK", busy)
        self.assertNotIn("All caught up", busy)
        self.assertIn("All caught up", self.card(html, "N222BB"))

    def test_one_waiting_item_uses_the_plane_pages_singular(self):
        self.add_found()
        html = self.login("customer").get("/portal/").get_data(as_text=True)
        self.assertIn("1 needs your OK", html)

    def test_answered_items_do_not_count(self):
        self.add_found(status="approved")
        html = self.login("customer").get("/portal/").get_data(as_text=True)
        self.assertNotIn("your OK", html)
        self.assertEqual(html.count("All caught up"), 2)


class DateFormatTest(OpsHubTestCase):
    """FLY-12."""

    def test_usdate_is_spelled_out_with_the_weekday(self):
        self.assertEqual(app_module.usdate("2026-10-02"), "Fri, Oct 2, 2026")
        self.assertEqual(app_module.usdate("2026-03-09"), "Mon, Mar 9, 2026")
        self.assertEqual(app_module.usdate("2026-10-02 13:05:09", True), "Fri, Oct 2, 2026 13:05")
        self.assertEqual(app_module.usdate("2026-10-02T13:05:09Z", True), "Fri, Oct 2, 2026 13:05")

    def test_usdate_short_has_no_weekday_or_year(self):
        self.assertEqual(app_module.usdate_short("2026-10-02"), "Oct 2")
        self.assertEqual(app_module.usdate_short("2026-12-25 08:30:00", True), "Dec 25 08:30")
        self.assertEqual(app_module.shortdate("2026-10-02"), "Oct 2")

    def test_non_dates_pass_through_unchanged(self):
        for v in (None, "", "soon", "2026-13-45", "26-10-02"):
            self.assertEqual(app_module.usdate(v), v)
            self.assertEqual(app_module.usdate_short(v), v)

    def test_filters_are_registered_for_templates(self):
        f = app_module.app.jinja_env.filters
        self.assertIs(f["usdate"], app_module.usdate)
        self.assertIs(f["usdate_short"], app_module.usdate_short)


class PageTitlesTest(OpsHubTestCase):
    """SEAM-15: Page name, a middle dot, then the program name."""

    def title(self, client, url):
        html = client.get(url, follow_redirects=True).get_data(as_text=True)
        return re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()

    def test_admin_account_and_customers_titles(self):
        c = self.login("master")
        self.assertEqual(self.title(c, "/admin/users"), "Manage Accounts · Admin")
        self.assertEqual(self.title(c, "/admin/system"), "System · Admin")
        self.assertEqual(self.title(c, "/account"), "Profile · My Account")
        self.assertEqual(self.title(c, "/customers"), "Customers · Winds Aloft")
        self.assertEqual(self.title(c, "/payroll"), "Payroll · Admin")

    def test_owner_portal_titles_carry_the_program_name(self):
        a = self.make_asset("N111AA")
        b = self.make_asset("N222BB")
        for x in (a, b):
            self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, x))
        c = self.login("customer")
        self.assertEqual(self.title(c, "/portal/"), "Your Aircraft · My Aircraft")
        self.assertEqual(self.title(c, f"/portal/aircraft/{a}"), "N111AA · My Aircraft")
        self.assertEqual(self.title(c, "/portal/account"), "My Account · My Aircraft")

    def test_no_old_opshub_suffix_on_the_shared_pages(self):
        c = self.login("master")
        for url in ("/admin", "/admin/notifications", "/admin/wave", "/admin/login-attempts", "/admin/system/log"):
            with self.subTest(url=url):
                t = self.title(c, url)
                self.assertTrue(t.endswith(" · Admin"), t)


class ConfirmBoxAccentTest(OpsHubTestCase):
    """DESIGN-1: the dialog's go-ahead button follows the program's accent."""

    def modal_default(self, client, url):
        html = client.get(url).get_data(as_text=True)
        return re.search(r'id="confirmActionModal" data-default-style="(\w+)"', html).group(1)

    def test_accent_per_program(self):
        m = self.login("master")
        self.assertEqual(self.modal_default(m, "/admin/system"), "primary")
        self.assertEqual(self.modal_default(m, "/customers"), "dark")
        self.assertEqual(self.modal_default(m, "/parts"), "dark")
