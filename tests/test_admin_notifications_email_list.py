"""Idea "admin notifications": the "Send a test email to..." box on
Admin > Notifications now offers a filterable dropdown of every active
account's email (a <datalist>, so typing still narrows it down one
character at a time) while still accepting any typed-in address that
isn't in the list.
"""
from harness import OpsHubTestCase


class AdminNotificationsEmailListTest(OpsHubTestCase):
    def test_active_users_emails_are_listed(self):
        self.exec("UPDATE users SET email = 'boss@shop.example' WHERE id = ?", (self.users["master"]["id"],))
        html = self.login("master").get("/admin/notifications").get_data(as_text=True)
        self.assertIn('id="test-email-list"', html)
        self.assertIn('value="boss@shop.example"', html)
        self.assertIn('list="test-email-list"', html)

    def test_users_without_an_email_are_not_listed(self):
        html = self.login("master").get("/admin/notifications").get_data(as_text=True)
        self.assertNotIn("value=\"\">", html.split('id="test-email-list"')[1].split("</datalist>")[0])

    def test_inactive_users_are_not_listed(self):
        self.exec("UPDATE users SET email = 'gone@shop.example', active = 0 WHERE id = ?", (self.users["tech"]["id"],))
        html = self.login("master").get("/admin/notifications").get_data(as_text=True)
        self.assertNotIn("gone@shop.example", html)

    def test_the_input_still_accepts_an_unlisted_email(self):
        # No server-side restriction to the list - a datalist never blocks
        # a typed value, and the test-email route itself doesn't check
        # membership either.
        r = self.login("master").post("/admin/notifications/test_email", data={"test_email": "someone-else@example.com"})
        self.assertEqual(r.status_code, 302)
