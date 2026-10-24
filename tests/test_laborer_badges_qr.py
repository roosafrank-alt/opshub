"""Idea "laborer barcode": Print Worker Badges now shows each laborer's
actual QR code on screen (with a "Print All" browser-print button), not
just a one-click send to the shop's label printer - so it works as a
quick way to see/print codes even without that printer handy.
"""
from harness import OpsHubTestCase


class LaborerBadgesQrTest(OpsHubTestCase):
    def make_laborer(self, name="Alex Tech", code="LABOR-AAAA1111"):
        return self.exec("INSERT INTO laborers (name, code, rate, active, created_at, updated_at) "
                         "VALUES (?, ?, 20, 1, datetime('now'), datetime('now'))", (name, code))

    def test_badges_page_shows_a_qr_code_per_laborer(self):
        self.make_laborer("Alex Tech", "LABOR-AAAA1111")
        self.make_laborer("Sam Wrench", "LABOR-BBBB2222")
        html = self.login("shop_admin").get("/labor/badges").get_data(as_text=True)
        self.assertIn("Alex Tech", html)
        self.assertIn("LABOR-AAAA1111", html)
        self.assertIn("Sam Wrench", html)
        self.assertIn("data-code=\"LABOR-AAAA1111\"", html)
        self.assertIn("Print page (regular printer)", html)  # SHOP-35: named by destination

    def test_inactive_laborers_are_not_listed(self):
        lid = self.make_laborer("Retired Person", "LABOR-CCCC3333")
        self.exec("UPDATE laborers SET active = 0 WHERE id = ?", (lid,))
        html = self.login("shop_admin").get("/labor/badges").get_data(as_text=True)
        self.assertNotIn("Retired Person", html)

    def test_tech_can_also_reach_the_badges_page(self):
        self.make_laborer()
        r = self.login("tech").get("/labor/badges")
        self.assertEqual(r.status_code, 200)
