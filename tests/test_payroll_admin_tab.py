"""Payroll lives in the Admin tab (idea "billing", edit: move out of Winds Aloft)."""
from harness import OpsHubTestCase


class PayrollInAdminTest(OpsHubTestCase):
    def test_payroll_opens_in_admin_for_master_admin(self):
        c = self.login("master")
        r = c.get("/admin/payroll")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Payroll", html)
        self.assertIn("bi-shield-lock-fill\"></i> Admin", html)  # Admin header, not Winds Aloft
        self.assertIn("/admin/payroll", c.get("/admin").get_data(as_text=True))

    def test_old_address_still_works(self):
        self.assertEqual(self.login("master").get("/payroll").status_code, 200)

    def test_not_in_shop_or_flight_menus_anymore(self):
        c = self.login("master")
        self.assertNotIn("/admin/payroll", c.get("/projects").get_data(as_text=True))
        self.assertNotIn("/admin/payroll", c.get("/flight/").get_data(as_text=True))

    def test_still_master_admin_only(self):
        for role in ("shop_admin", "tech", "cfi_billing", "flight_student"):
            with self.subTest(role=role):
                r = self.login(role).get("/admin/payroll")
                self.assertEqual(r.status_code, 302)
