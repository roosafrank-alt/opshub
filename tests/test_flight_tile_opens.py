"""The Fly with Kate! tile on the launcher must open Flight School even when
the session has no cfi_id / student_id yet (they are filled in by the session
check that Flight School pages run, so a fresh or older session lacked them
and the tile bounced back to the launcher until another page was opened)."""
from harness import OpsHubTestCase


def goes_to(r, path):
    return r.status_code in (301, 302, 303) and r.headers["Location"].split("?")[0].endswith(path)


class FlightTileOpensTest(OpsHubTestCase):
    def drop_profile_ids(self, client):
        with client.session_transaction() as s:
            s.pop("cfi_id", None)
            s.pop("student_id", None)

    def test_tile_opens_dashboard_without_profile_ids(self):
        for who in ("master", "cfi", "flight_admin", "flight_student"):
            c = self.login(who)
            self.drop_profile_ids(c)
            r = c.get("/flight/")
            self.assertTrue(goes_to(r, "/flight/dashboard"), f"{who}: tile should go to the dashboard")
            # ...and the dashboard itself then opens (it restores the ids).
            self.assertEqual(c.get("/flight/dashboard").status_code, 200, who)

    def test_tile_still_works_with_profile_ids(self):
        r = self.login("cfi").get("/flight/")
        self.assertTrue(goes_to(r, "/flight/dashboard"))

    def test_shop_only_account_goes_back_to_launcher(self):
        for who in ("shop_admin", "tech"):
            r = self.login(who).get("/flight/")
            self.assertTrue(goes_to(r, "/"), f"{who} has no flight role")
            self.assertFalse(goes_to(r, "/flight/dashboard"), who)

    def test_signed_out_goes_to_launcher(self):
        r = self.login(None).get("/flight/")
        self.assertTrue(goes_to(r, "/"))
        self.assertFalse(goes_to(r, "/flight/dashboard"))
