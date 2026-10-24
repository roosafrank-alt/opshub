"""AD compliance per aircraft (QA feat-ad-compliance-tracker)."""
import io
from datetime import date, timedelta

from harness import OpsHubTestCase


class AdsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N123")
        self.exec("UPDATE assets SET tach_hours = 1000 WHERE id = ?", (self.asset,))
        self.login("tech")

    def add(self, **form):
        data = dict(ad_number="2011-10-09", subject="Seat rails", kind="one_time")
        data.update(form)
        return self.client.post(f"/assets/{self.asset}/ads", data=data)

    def ads(self):
        return self.q("SELECT * FROM ads WHERE active = 1 ORDER BY id")

    def test_one_time_ad_open_then_complied(self):
        self.add()
        ad = self.ads()[0]
        html = self.client.get(f"/assets/{self.asset}").get_data(as_text=True)
        self.assertIn("AD 2011-10-09", html)
        self.assertIn(">Open<", html)
        self.client.post(f"/ads/{ad['id']}/comply", data=dict(complied_date=date.today().isoformat(), tach_hours="1000",
                                                               method="inspection", signed_by="IA Bob",
                                                               photo=(io.BytesIO(b"img"), "log.jpg")),
                         content_type="multipart/form-data")
        c = self.q1("SELECT * FROM ad_compliance")
        self.assertEqual((c["method"], c["signed_by"], c["tach_hours"]), ("inspection", "IA Bob", 1000))
        self.assertTrue(c["photo"])
        self.assertIn("Complied", self.client.get(f"/assets/{self.asset}").get_data(as_text=True))

    def test_recurring_ad_feeds_maintenance_tracking(self):
        self.add(ad_number="2020-01-01", kind="recurring_hours", interval_hours="100")
        ad = self.ads()[0]
        item = self.q1("SELECT * FROM maintenance_items WHERE id = ?", (ad["maintenance_item_id"],))
        self.assertEqual((item["type"], item["interval_hours"], item["active"]), ("hours", 100, 1))
        self.client.post(f"/ads/{ad['id']}/comply", data=dict(tach_hours="950", method="inspection"))
        item = self.q1("SELECT * FROM maintenance_items WHERE id = ?", (ad["maintenance_item_id"],))
        self.assertEqual(item["last_done_hours"], 950)
        self.assertEqual(len(self.q("SELECT id FROM maintenance_log WHERE item_id = ?", (item["id"],))), 1)
        html = self.client.get(f"/assets/{self.asset}").get_data(as_text=True)
        self.assertIn("50", html)  # 50 hours remaining shows as due soon/ok
        # Terminating action ends the recurring tracking.
        self.client.post(f"/ads/{ad['id']}/comply", data=dict(method="terminated"))
        self.assertEqual(self.q1("SELECT active FROM maintenance_items WHERE id = ?", (item["id"],))["active"], 0)

    def test_recurring_by_date_and_na(self):
        self.add(ad_number="2019-05-05", kind="recurring_date", interval_days="365")
        self.add(ad_number="2018-02-02", kind="not_applicable", na_reason="S/N not affected")
        self.assertEqual(len(self.ads()), 2)
        item = self.q1("SELECT * FROM maintenance_items WHERE name LIKE 'AD 2019-05-05%'")
        self.assertEqual((item["type"], item["interval_days"]), ("calendar", 365))

    def test_validation(self):
        for form in (dict(ad_number=""), dict(kind="bogus"), dict(kind="recurring_hours", interval_hours="-5"),
                     dict(kind="recurring_date", interval_days="0"), dict(kind="not_applicable")):
            with self.subTest(**form):
                self.add(**form)
        self.assertEqual(self.ads(), [])
        self.add()
        self.add()  # duplicate number
        self.assertEqual(len(self.ads()), 1)
        ad = self.ads()[0]
        self.client.post(f"/ads/{ad['id']}/comply", data=dict(method="inspection",
                                                               complied_date=(date.today() + timedelta(days=3)).isoformat()))
        self.client.post(f"/ads/{ad['id']}/comply", data=dict(method="nope"))
        self.assertEqual(self.q("SELECT id FROM ad_compliance"), [])

    def test_print_sheet_and_access(self):
        self.add()
        html = self.client.get(f"/assets/{self.asset}/ads/print").get_data(as_text=True)
        self.assertIn("2011-10-09", html)
        for role in ("cfi", "flight_student", "no_roles"):
            with self.subTest(role=role):
                c = self.login(role)
                c.post(f"/assets/{self.asset}/ads", data=dict(ad_number="9999-99-99", kind="one_time"))
                self.assertNotEqual(c.get(f"/assets/{self.asset}/ads/print").status_code, 200)
        self.assertEqual(len(self.ads()), 1)

    def test_compliance_note_prints_on_ad_sheet(self):
        self.add()
        ad = self.ads()[0]
        self.client.post(f"/ads/{ad['id']}/comply", data=dict(method="inspection",
                                                               note="Rails inspected, no cracks; see logbook p.42"))
        html = self.client.get(f"/assets/{self.asset}/ads/print").get_data(as_text=True)
        self.assertIn("<th>Notes</th>", html)
        self.assertIn("Rails inspected, no cracks; see logbook p.42", html)

    def test_annual_project_gets_ads_on_job_sheet(self):
        self.add()
        self.add(ad_number="2018-02-02", kind="not_applicable", na_reason="n/a")
        self.client.post("/projects/new", data=dict(name="Annual - N123", asset_id=str(self.asset)))
        p = self.q1("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
        self.assertIn("AD 2011-10-09", p["standard_items"] or "")
        self.assertNotIn("2018-02-02", p["standard_items"] or "")
        self.client.post("/projects/new", data=dict(name="Oil change - N123", asset_id=str(self.asset)))
        p = self.q1("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
        self.assertNotIn("AD 2011", p["standard_items"] or "")
