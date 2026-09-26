"""Scanning from any page keeps you on that page (idea "scanner"): the code
goes to the Scan page running in a hidden frame and its result pops up where
you are. (Checked end-to-end in a real browser by hand; these guard the wiring.)"""
from harness import OpsHubTestCase


class ScanStaysOnPageTest(OpsHubTestCase):
    def test_pages_no_longer_jump_to_the_scan_page(self):
        html = self.login("tech").get("/projects").get_data(as_text=True)
        self.assertNotIn("?autoscan=\" + encodeURIComponent(code)", html)
        self.assertIn("global-scan-toasts", html)
        self.assertIn("embedded=1", html)

    def test_embedded_scan_page_has_no_menus_and_opens_links_in_the_real_page(self):
        html = self.login("tech").get("/scan?embedded=1").get_data(as_text=True)
        self.assertNotIn('<nav class="navbar', html)
        self.assertIn('<base target="_top">', html)
        self.assertIn("Never pull focus or scroll the page behind", html)
        normal = self.login("tech").get("/scan").get_data(as_text=True)
        self.assertIn('<nav class="navbar', normal)
        self.assertNotIn("Never pull focus or scroll the page behind", normal)

    def test_master_admin_gets_scan_capture_too(self):
        html = self.login("master").get("/projects").get_data(as_text=True)
        self.assertIn("global-scan-toasts", html)
