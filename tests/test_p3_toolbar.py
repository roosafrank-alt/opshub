"""Preview build, phase 3: DESIGN-9 one list-page toolbar and one page-title
style. The shared toolbar is templates/_list_toolbar.html; every program's
main list page uses it, and each page's search box keeps its id so the
page's own filter script still works."""
import re

from harness import OpsHubTestCase

# page, role to look at it as, ids that must still be on the page
LIST_PAGES = [
    ("/parts", "master", ["parts-live-search"]),
    ("/projects", "master", []),
    ("/assets", "master", []),
    ("/orders?status=pending", "master", ["orders-search"]),
    ("/laborers", "master", []),
    ("/manuals", "master", []),
    ("/squawks", "master", []),
    ("/customers", "master", []),
    ("/admin/users", "master", ["users-search"]),
    ("/flight/students", "master", ["students-search", "students-count"]),
    ("/flight/cfis", "master", ["cfis-search"]),
    ("/flight/planes", "master", ["planes-search"]),
]


class ToolbarTest(OpsHubTestCase):
    def test_list_pages_use_the_shared_toolbar(self):  # DESIGN-9
        c = self.login("master")
        for path, _role, ids in LIST_PAGES:
            with self.subTest(path=path):
                r = c.get(path)
                self.assertEqual(r.status_code, 200)
                html = r.get_data(as_text=True)
                self.assertEqual(html.count('class="list-toolbar"'), 1)
                self.assertIn('data-change="DESIGN-9', html)
                for i in ids:
                    self.assertIn('id="%s"' % i, html)

    def test_toolbar_has_title_search_and_add_in_order(self):  # DESIGN-9
        html = self.login("master").get("/laborers").get_data(as_text=True)
        bar = html[html.index('class="list-toolbar"'):]
        t = bar.index("list-toolbar-title")
        s = bar.index("list-toolbar-search")
        a = bar.index("list-toolbar-add")
        self.assertTrue(t < s < a)
        self.assertIn("Add Worker", bar[a:a + 800])

    def test_add_button_hidden_from_techs_but_toolbar_stays(self):  # DESIGN-9
        html = self.login("tech").get("/assets").get_data(as_text=True)
        self.assertIn('class="list-toolbar"', html)
        self.assertNotIn("Add Aircraft", html)

    def test_list_page_title_is_one_h4(self):  # DESIGN-9
        c = self.login("master")
        for path in ("/parts", "/customers", "/flight/students", "/admin/users"):
            with self.subTest(path=path):
                html = c.get(path).get_data(as_text=True)
                bar = html[html.index('class="list-toolbar"'):]
                self.assertIn('<h4 class="list-toolbar-title mb-0">', bar)
                self.assertEqual(html.count('class="list-toolbar-title'), 1)

    def test_former_h5_page_titles_are_h4(self):  # DESIGN-9
        html = self.login(None).get("/forgot-password").get_data(as_text=True)
        self.assertIn('<h4 class="mb-2" data-change="PW-3 DESIGN-9">', html)
        self.assertNotIn("<h5", html)
        pid = self.make_part()
        html = self.login("master").get("/parts/%d" % pid).get_data(as_text=True)
        self.assertRegex(html, r'<h4 data-change="DESIGN-9">Oil Filter')

    def test_old_hand_built_toolbar_classes_are_gone(self):  # DESIGN-9
        html = self.login("master").get("/parts").get_data(as_text=True)
        self.assertNotIn("order-md-", html.split('class="list-toolbar"')[1][:2500])
