"""Login lockout: 5 wrong passwords lock an account for a while (never for good),
on the hub form, /flight/login and the customer portal alike."""
import unittest
from unittest import mock

from harness import OpsHubTestCase
import auth
import db

PW = "whatever-the-fast-hash-accepts"


def _post(client, url, user, pw, field="username", **kw):
    return client.post(url, data={field: user, "password": pw}, follow_redirects=True, **kw)


class LoginLockoutTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        # harness hashes are fast placeholders; make the check pass only for PW
        self._p = mock.patch("auth.check_password_hash", lambda h, p: p == PW)
        self._p.start()
        self.addCleanup(self._p.stop)

    def wrong(self, n, user="tech", url="/", field="username"):
        for _ in range(n):
            r = _post(self.client, url, user, "bad", field)
        return r

    def is_in(self, r):
        return b"Choose a program" in r.data or b"Welcome" in r.data or r.request.path != "/"

    def test_hub_locks_after_five_and_correct_password_is_refused(self):
        self.wrong(5)
        r = _post(self.client, "/", "tech", PW)
        self.assertIn(b"try again later", r.data)
        with self.client.session_transaction() as s:
            self.assertNotIn("user_id", s)

    def test_four_failures_do_not_lock(self):
        self.wrong(4)
        _post(self.client, "/", "tech", PW)
        with self.client.session_transaction() as s:
            self.assertIn("user_id", s)

    def test_correct_login_resets_count(self):
        self.wrong(4)
        _post(self.client, "/", "tech", PW)
        self.client = self.app.test_client()
        self.wrong(4)
        _post(self.client, "/", "tech", PW)
        with self.client.session_transaction() as s:
            self.assertIn("user_id", s)

    def test_unlocks_after_the_wait_and_repeat_lockout_is_longer(self):
        self.wrong(5)
        self.exec("UPDATE login_lockouts SET locked_until = '2000-01-01T00:00:00Z'")
        self.wrong(5)
        row = self.q1("SELECT * FROM login_lockouts WHERE account='tech'")
        self.assertEqual(row["lockouts"], 2)
        self.exec("UPDATE login_lockouts SET locked_until = '2000-01-01T00:00:00Z'")
        _post(self.client, "/", "tech", PW)
        with self.client.session_transaction() as s:
            self.assertIn("user_id", s)

    def test_staff_login_route_locks(self):
        self.wrong(5, url="/flight/login")
        _post(self.client, "/flight/login", "tech", PW)
        with self.client.session_transaction() as s:
            self.assertNotIn("user_id", s)

    def test_customer_portal_locks_and_message_is_generic(self):
        r1 = self.wrong(1, "nobody@example.com", "/customer/login", "email")
        r2 = self.wrong(1, "owner@example.com", "/customer/login", "email")
        self.assertEqual(r1.data.count(b"try again later"), r2.data.count(b"try again later"))
        self.wrong(3, "owner@example.com", "/customer/login", "email")
        _post(self.client, "/customer/login", "owner@example.com", PW, "email")
        with self.client.session_transaction() as s:
            self.assertNotIn("customer_id", s)

    def test_admin_page_lists_and_unlocks(self):
        self.wrong(5)
        c = self.login("master")
        r = c.get("/admin/login-attempts")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"tech", r.data)
        self.assertIn(b"Unlock", r.data)
        c.post("/admin/login-attempts", data={"account": "tech"})
        self.assertIsNone(self.q1("SELECT * FROM login_lockouts WHERE account='tech'"))
        c2 = self.app.test_client()
        _post(c2, "/", "tech", PW)
        with c2.session_transaction() as s:
            self.assertIn("user_id", s)

    def test_non_admin_cannot_open_admin_page(self):
        c = self.login("shop_student")
        self.assertNotEqual(c.get("/admin/login-attempts").status_code, 200)

    def test_one_source_many_names_is_slowed_but_proxy_is_not(self):
        hdr = {"X-Forwarded-For": "203.0.113.9"}
        for i in range(10):
            _post(self.client, "/", "guess%d" % i, "bad", environ_base={"REMOTE_ADDR": "127.0.0.1"}, headers=hdr)
        r = _post(self.client, "/", "tech", PW, environ_base={"REMOTE_ADDR": "127.0.0.1"}, headers=hdr)
        self.assertIn(b"try again later", r.data)
        self.assertEqual(self.q1("SELECT source FROM login_attempts")["source"], "203.0.113.9")
        # a different visitor behind the same proxy is fine
        r = _post(self.client, "/", "tech", PW, environ_base={"REMOTE_ADDR": "127.0.0.1"},
                  headers={"X-Forwarded-For": "198.51.100.1"})
        with self.client.session_transaction() as s:
            self.assertIn("user_id", s)

    def test_forwarded_header_ignored_from_outside(self):
        with self.app.test_request_context("/", headers={"X-Forwarded-For": "1.2.3.4"},
                                           environ_base={"REMOTE_ADDR": "10.0.0.5"}):
            self.assertEqual(auth.login_source(), "10.0.0.5")

    def test_proxy_address_itself_never_throttled(self):
        for i in range(15):
            _post(self.client, "/", "guess%d" % i, "bad", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        _post(self.client, "/", "tech", PW, environ_base={"REMOTE_ADDR": "127.0.0.1"})
        with self.client.session_transaction() as s:
            self.assertIn("user_id", s)


if __name__ == "__main__":
    unittest.main()
