"""Email bill: the "Pay it here" link in a bill opens My Account, so it must open for
whoever's login the student record belongs to, not only for student-role logins."""
from harness import OpsHubTestCase


class MyAccountAnyRoleTest(OpsHubTestCase):
    def test_admin_with_a_student_record_can_open_my_account(self):
        master_id = self.users["master"]["id"]
        self.exec("INSERT INTO students (name, username, password_hash, user_id) VALUES (?, ?, ?, ?)", ("Admin Flyer", "adminflyer", "x", master_id))
        html = self.login("master").get("/flight/my-account").get_data(as_text=True)
        self.assertNotIn("My Account is for student accounts", html)
        self.assertIn("Unpaid", html)

    def test_login_with_no_student_record_still_bounced(self):
        r = self.login("master").get("/flight/my-account", follow_redirects=True)
        self.assertIn("My Account is for student accounts", r.get_data(as_text=True))
