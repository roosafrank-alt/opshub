"""PW-1 current password, PW-2 no readable copy, PW-3 forgot password by email."""
import re
from unittest import mock

from werkzeug.security import check_password_hash

from harness import OpsHubTestCase, PASSWORD
import notify


class CurrentPasswordTest(OpsHubTestCase):
    def post(self, c, current, new="brandnew123"):
        return c.post("/account", data={"name": "Tech", "current_password": current, "password": new, "password_confirm": new})

    def test_wrong_or_missing_current_password_changes_nothing(self):
        c = self.login("tech")
        before = self.q1("SELECT password_hash FROM users WHERE id = ?", (self.users["tech"]["id"],))["password_hash"]
        for bad in ("", "nope-nope"):
            html = self.post(c, bad).get_data(as_text=True)
            self.assertIn("password was not changed", html)
        self.assertEqual(self.q1("SELECT password_hash FROM users WHERE id = ?", (self.users["tech"]["id"],))["password_hash"], before)

    def test_right_current_password_changes_it_and_keeps_no_readable_copy(self):
        self.exec("UPDATE users SET password_plain = 'old-readable' WHERE id = ?", (self.users["tech"]["id"],))
        c = self.login("tech")
        self.assertEqual(self.post(c, PASSWORD).status_code, 302)
        row = self.q1("SELECT * FROM users WHERE id = ?", (self.users["tech"]["id"],))
        self.assertTrue(check_password_hash(row["password_hash"], "brandnew123"))
        self.assertIsNone(row["password_plain"])

    def test_five_wrong_guesses_lock_the_account(self):
        c = self.login("tech")
        for _ in range(5):
            self.post(c, "wrong-guess")
        html = self.post(c, PASSWORD).get_data(as_text=True)
        self.assertIn("Too many wrong tries", html)

    def test_no_current_password_needed_when_not_changing_it(self):
        c = self.login("tech")
        self.assertEqual(c.post("/account", data={"name": "Tech Two"}).status_code, 302)

    def test_box_is_on_both_my_account_pages(self):
        self.assertIn('name="current_password"', self.login("tech").get("/account").get_data(as_text=True))
        self.assertIn('name="current_password"', self.login("customer").get("/portal/account").get_data(as_text=True))

    def test_owner_must_type_current_password_too(self):
        c = self.login("customer")
        data = {"name": "Owner Customer", "email": "owner@example.com", "password": "newpass123", "password_confirm": "newpass123"}
        self.assertIn("password was not changed", c.post("/portal/account", data=data).get_data(as_text=True))
        data["current_password"] = PASSWORD
        self.assertEqual(c.post("/portal/account", data=data).status_code, 302)


class ForgotPasswordTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.exec("UPDATE users SET email = 'tech@example.com' WHERE id = ?", (self.users["tech"]["id"],))
        self.sent = []

        def fake_send(settings, to_addr, subject, body, brand=""):
            self.sent.append((to_addr, subject, body))
            return True, None
        p = mock.patch.object(notify, "send_email", fake_send)
        p.start()
        self.addCleanup(p.stop)

    def request_link(self, who):
        return self.login(None).post("/forgot-password", data={"who": who}, follow_redirects=True)

    def token(self):
        return re.search(r"/reset-password/([\w-]+)", self.sent[-1][2]).group(1)

    def test_login_page_links_to_it(self):
        self.assertIn("/forgot-password", self.login(None).get("/").get_data(as_text=True))

    def test_same_answer_whether_or_not_the_account_exists(self):
        a = self.request_link("tech@example.com").get_data(as_text=True)
        b = self.request_link("nobody@nowhere.example").get_data(as_text=True)
        self.assertIn("emailed a link", a)
        self.assertIn("emailed a link", b)
        self.assertEqual(len(self.sent), 1)

    def test_link_by_username_goes_to_the_email_on_file(self):
        self.request_link(self.users["tech"]["username"])
        self.assertEqual(self.sent[0][0], "tech@example.com")

    def test_account_without_email_gets_nothing(self):
        self.request_link(self.users["cfi"]["username"])
        self.assertEqual(self.sent, [])

    def test_full_flow_changes_password_once_and_clears_lockout_and_plain(self):
        self.exec("UPDATE users SET password_plain = 'readable' WHERE id = ?", (self.users["tech"]["id"],))
        self.request_link("tech@example.com")
        tok = self.token()
        c = self.login(None)
        self.assertEqual(c.get(f"/reset-password/{tok}").status_code, 200)
        r = c.post(f"/reset-password/{tok}", data={"password": "short", "password_confirm": "short"})
        self.assertIn("at least 8 characters", r.get_data(as_text=True))
        r = c.post(f"/reset-password/{tok}", data={"password": "freshpass99", "password_confirm": "freshpass99"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM users WHERE id = ?", (self.users["tech"]["id"],))
        self.assertTrue(check_password_hash(row["password_hash"], "freshpass99"))
        self.assertIsNone(row["password_plain"])
        self.assertEqual(c.get(f"/reset-password/{tok}").status_code, 410)   # used once
        r = c.post("/", data={"username": self.users["tech"]["username"], "password": "freshpass99"})
        self.assertEqual(r.status_code, 302)

    def test_expired_or_made_up_links_are_refused(self):
        self.request_link("tech@example.com")
        tok = self.token()
        self.exec("UPDATE password_resets SET expires_at = '2000-01-01T00:00:00Z'")
        self.assertEqual(self.login(None).get(f"/reset-password/{tok}").status_code, 410)
        self.assertEqual(self.login(None).get("/reset-password/not-a-real-token").status_code, 410)

    def test_only_three_links_an_hour_per_account(self):
        for _ in range(5):
            self.request_link("tech@example.com")
        self.assertEqual(len(self.sent), 3)

    def test_owner_reset_and_matching_staff_login_move_together(self):
        self.exec("UPDATE users SET email = 'owner@example.com', password_hash = (SELECT password_hash FROM customers WHERE id = ?) WHERE id = ?",
                  (self.customer_id, self.users["tech"]["id"]))
        self.request_link("owner@example.com")
        self.assertEqual(len(self.sent), 2)
        tok = [re.search(r"/reset-password/([\w-]+)", m[2]).group(1) for m in self.sent]
        self.login(None).post(f"/reset-password/{tok[0]}", data={"password": "sharedpass1", "password_confirm": "sharedpass1"})
        u = self.q1("SELECT password_hash FROM users WHERE id = ?", (self.users["tech"]["id"],))
        c = self.q1("SELECT password_hash FROM customers WHERE id = ?", (self.customer_id,))
        self.assertTrue(check_password_hash(u["password_hash"], "sharedpass1"))
        self.assertTrue(check_password_hash(c["password_hash"], "sharedpass1"))

    def test_customer_send_login_is_disabled_after_they_choose_their_own(self):
        self.exec("UPDATE customers SET password_plain = NULL WHERE id = ?", (self.customer_id,))
        html = self.login("master").get(f"/customers/{self.customer_id}").get_data(as_text=True)
        self.assertIn("They have chosen their own password", html)
