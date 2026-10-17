"""Add Account: the Email box fills itself from an email-style Username."""
from harness import OpsHubTestCase


class AdminNewUserEmailFillTest(OpsHubTestCase):
    def test_new_page_has_fill_script_and_note(self):
        body = self.login("master").get("/admin/users/new").get_data(as_text=True)
        self.assertIn('id="acct-username"', body)
        self.assertIn("Filled in from Username - change it if reminders should go elsewhere", body)
        self.assertIn("emailBox.value = autoEmail ? v : ''", body)

    def test_edit_page_has_no_note(self):
        uid = self.users["flight_student"]["id"]
        body = self.login("master").get(f"/admin/users/{uid}/edit").get_data(as_text=True)
        self.assertNotIn("Filled in from Username", body)
