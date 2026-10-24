"""Manage menu: 'My CFI Profile', 'My Schedule' and 'My Pay' are one 'Me' item;
the three pages share a Profile / My schedule & time off / My pay tab strip and
the old URLs still open the matching tab."""
from harness import OpsHubTestCase

TABS = 'id="me-tabs"'


class CfiMeTabsTest(OpsHubTestCase):
    def cfi_id(self, key="cfi"):
        return self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users[key]["id"],))["id"]

    def test_menu_has_one_me_item(self):
        html = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Me (profile, time off, pay)", html)
        for old in ("</i> My CFI Profile</a>", "</i> My Schedule</a>", "</i> My Pay</a>"):
            self.assertNotIn(old, html)

    def test_old_urls_open_matching_tab(self):
        c = self.login("cfi")
        pay = f"/flight/cfis/{self.cfi_id()}/pay"
        for path, tab in (("/flight/cfis/me", "Profile"), ("/flight/cfis/schedule", "My schedule"), (pay, "My pay")):
            r = c.get(path)
            self.assertEqual(r.status_code, 200, path)
            html = r.get_data(as_text=True)
            self.assertIn(TABS, html, path)
            seg = html.split(TABS, 1)[1].split("</ul>", 1)[0]
            self.assertEqual(seg.count("nav-link active"), 1, path)
            self.assertIn("nav-link active", seg.split(tab, 1)[0].rsplit("<a", 1)[1], path)
        self.assertNotIn("btn-outline-success btn-sm\"><i class=\"bi bi-cash-stack\"></i> My Pay",
                         c.get("/flight/cfis/me").get_data(as_text=True))

    def test_admin_viewing_other_cfi_has_no_tabs(self):
        c = self.login("master")
        cid = self.cfi_id()
        self.assertNotIn(TABS, c.get(f"/flight/cfis/{cid}/pay").get_data(as_text=True))
        self.assertNotIn(TABS, c.get(f"/flight/cfis/schedule?cfi_id={cid}").get_data(as_text=True))

    def test_admin_without_cfi_role_has_no_pay_tab(self):
        c = self.login("flight_admin_cfi")
        html = c.get("/flight/cfis/me").get_data(as_text=True)
        self.assertIn("My pay", html.split(TABS, 1)[1].split("</ul>", 1)[0])
