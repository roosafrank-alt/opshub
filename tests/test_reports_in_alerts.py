"""Reports tab folded into Alerts: no Reports tab in the Fly with Kate! menu,
a 'Report an issue' button on Alerts, open reports listed under the alerts
with Resolve (CFI/admin only), resolved ones in the archive, the Alerts badge
counting open reports, and old /flight/reports links going to Alerts."""
from harness import OpsHubTestCase


class ReportsInAlertsTest(OpsHubTestCase):
    def _file(self, c, **kw):
        data = dict(category="suggestion", notes="Add a coffee machine")
        data.update(kw)
        return c.post("/flight/reports", data=data, follow_redirects=True)

    def test_menu_has_no_reports_tab(self):
        for role in ("cfi", "flight_student"):
            body = self.login(role).get("/flight/dashboard").get_data(as_text=True)
            self.assertNotIn('id="tour-nav-flight-reports"', body)
            self.assertIn('id="tour-nav-flight-alerts"', body)

    def test_old_reports_link_goes_to_alerts(self):
        r = self.login("cfi").get("/flight/reports")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/flight/alerts"))
        r = self.login("flight_student").get("/flight/reports")
        self.assertTrue(r.headers["Location"].endswith("/flight/notifications"))

    def test_cfi_files_sees_and_resolves(self):
        c = self.login("cfi")
        body = c.get("/flight/alerts").get_data(as_text=True)
        self.assertIn("Report an issue", body)
        body = self._file(c).get_data(as_text=True)
        self.assertIn("Report filed.", body)
        self.assertIn("Reports from students &amp; CFIs (1)", body)
        self.assertIn("Add a coffee machine", body)
        self.assertIn("Resolve", body)
        rid = self.q1("SELECT id FROM flight_reports")["id"]
        body = c.post(f"/flight/reports/{rid}/resolve", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Reports from students &amp; CFIs (0)", body)
        self.assertIn("Resolved reports", body)

    def test_plane_issue_still_makes_squawk(self):
        aid = self.make_asset("N1FK")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (aid,))
        self._file(self.login("cfi"), category="plane_issue", asset_id=aid, notes="Flat tire")
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM plane_squawks WHERE asset_id = ?", (aid,))["c"], 1)

    def test_student_sees_reports_but_cannot_resolve(self):
        self._file(self.login("cfi"))
        s = self.login("flight_student")
        body = s.get("/flight/notifications").get_data(as_text=True)
        self.assertIn("Report an issue", body)
        self.assertIn("Add a coffee machine", body)
        self.assertNotIn("/resolve", body)
        rid = self.q1("SELECT id FROM flight_reports")["id"]
        s.post(f"/flight/reports/{rid}/resolve")
        self.assertIsNone(self.q1("SELECT resolved_at FROM flight_reports")["resolved_at"])

    def test_badge_counts_open_reports(self):
        c = self.login("cfi")
        self._file(c)
        self._file(c, notes="Another")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn('title="2 new, 2 open', body)

    def test_admin_who_is_not_a_cfi_gets_alerts(self):
        self.exec("UPDATE users SET is_master_admin = 1 WHERE username = 'shop_admin'")
        c = self.login("shop_admin")
        body = c.get("/flight/alerts").get_data(as_text=True)
        self.assertIn("Report an issue", body)
