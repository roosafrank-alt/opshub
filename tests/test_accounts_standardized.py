"""Idea "Accounts": every account is set up/edited from one place, Admin >
Accounts, in one standardized layout (Login, Access, Profiles, Pay, Contact
& reminders); a CFI's or student's own profile page links back to that page
for password/access/billing/active-status changes instead of duplicating
them there."""
from harness import OpsHubTestCase


class AccountsStandardizedTest(OpsHubTestCase):
    def test_admin_account_form_has_every_standardized_section(self):
        user_id = self.users["cfi"]["id"]
        html = self.login("master").get(f"/admin/users/{user_id}/edit").get_data(as_text=True)
        for heading in ("Login", "Access", "Profiles", "Pay", "Contact &amp; reminders"):
            self.assertIn(heading, html)

    def test_cfi_profile_links_back_to_admin_account_edit(self):
        cfi_id = self.q1("SELECT id, user_id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))
        html = self.login("master").get(f"/flight/cfis/{cfi_id['id']}/edit").get_data(as_text=True)
        self.assertIn(f"/admin/users/{cfi_id['user_id']}/edit", html)
        self.assertIn("Edit Account", html)

    def test_student_profile_links_back_to_admin_account_edit(self):
        student_id = self.q1("SELECT id, user_id FROM students WHERE user_id = ?",
                             (self.users["flight_student"]["id"],))
        html = self.login("master").get(f"/flight/students/{student_id['id']}/edit").get_data(as_text=True)
        self.assertIn(f"/admin/users/{student_id['user_id']}/edit", html)
        self.assertIn("Edit Account", html)

    def test_admin_account_page_links_to_the_cfi_and_shop_badge(self):
        user_id = self.users["cfi"]["id"]
        html = self.login("master").get(f"/admin/users/{user_id}/edit").get_data(as_text=True)
        self.assertIn("CFI Profile", html)
