"""New feature feat-promised-back-date: an optional 'Promised back' date on a
job, flagged late / due soon on the Projects list (shop only). The owner sees
the date on their portal only when 'Show to owner' was switched on."""
from datetime import date, timedelta
from harness import OpsHubTestCase


def _iso(days):
    return (date.today() + timedelta(days=days)).isoformat()


class PromisedBackDateTest(OpsHubTestCase):
    def test_defaults_shop_only_and_saves_date(self):
        c = self.login("shop_admin")
        r = c.post("/projects/new", data=dict(name="Annual - N1", promised_date=_iso(5)))
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT promised_date, promised_show_owner FROM projects WHERE name = 'Annual - N1'")
        self.assertEqual(row["promised_date"], _iso(5))
        self.assertEqual(row["promised_show_owner"], 0)

    def test_list_tags_and_filter(self):
        a = self.make_project(name="Late job")
        b = self.make_project(name="Soon job")
        d = self.make_project(name="Fine job")
        e = self.make_project(name="Nodate job")
        f = self.make_project(name="Done late job", status="completed")
        for pid, days in ((a, -3), (b, 1), (d, 9), (f, -3)):
            self.exec("UPDATE projects SET promised_date = ? WHERE id = ?", (_iso(days), pid))
        c = self.login("shop_admin")
        html = c.get("/projects").get_data(as_text=True)
        for t in ("3 days late", "Due tomorrow", "On track", "No date set", "Late / due soon"):
            self.assertIn(t, html)
        self.assertEqual(html.count("3 days late"), 1)  # completed job shows no tag
        late = c.get("/projects?status=late").get_data(as_text=True)
        self.assertIn("Late job", late)
        self.assertIn("Soon job", late)
        self.assertNotIn("Fine job", late)
        self.assertNotIn("Nodate job", late)
        self.assertNotIn("Done late job", late)

    def test_owner_sees_date_only_when_switched_on(self):
        asset = self.make_asset("N9PB")
        pid = self.make_project(name="Annual - N9PB", asset_id=asset)
        self.exec("UPDATE projects SET scheduled_date = ?, promised_date = ? WHERE id = ?",
                  (_iso(1), _iso(6), pid))
        self.exec("INSERT OR IGNORE INTO customer_assets (customer_id, asset_id) VALUES (?, ?)",
                  (self.customer_id, asset))
        url = f"/portal/aircraft/{asset}"
        html = self.login("customer").get(url).get_data(as_text=True)
        self.assertIn("Annual - N9PB", html)
        self.assertNotIn("Promised back", html)
        self.exec("UPDATE projects SET promised_show_owner = 1 WHERE id = ?", (pid,))
        html = self.login("customer").get(url).get_data(as_text=True)
        self.assertIn("Promised back", html)
