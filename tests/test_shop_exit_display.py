"""The shop TV's own "Shop" login gets an "Exit to Desktop" item in its account
menu that closes the full-screen browser on the Pi. Nobody else sees it, and
nobody else can use it."""
from harness import OpsHubTestCase
import app as app_module

ITEM = "Exit to Desktop"


class ShopExitDisplayTest(OpsHubTestCase):
    def _shop_client(self):
        c = self.login("cfi")
        with c.session_transaction() as s:
            s["user_name"] = "Shop"
        return c

    def _post(self, client):
        calls = []
        saved = app_module._run_delayed_command
        app_module._run_delayed_command = lambda args, delay=2.0: calls.append(args)
        try:
            resp = client.post("/shop/exit-display")
        finally:
            app_module._run_delayed_command = saved
        return resp, calls

    def test_shop_login_sees_the_item_and_can_use_it(self):
        c = self._shop_client()
        self.assertIn(ITEM, c.get("/flight/dashboard").data.decode())
        resp, calls = self._post(c)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls, [["pkill", "-x", "chromium"]])

    def test_other_logins_do_not_see_it_or_use_it(self):
        for role in ("cfi", "flight_admin", "master"):
            with self.subTest(role=role):
                c = self.login(role)
                self.assertNotIn("/shop/exit-display", c.get("/flight/dashboard").data.decode())
                _, calls = self._post(c)
                self.assertEqual(calls, [])


if __name__ == "__main__":
    import unittest
    unittest.main()
