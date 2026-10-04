"""Shop logbook entries: save from the starter page, edit, print, delete,
browse/filter, and the admin starter templates.

Tests marked @open_finding("qa-...") describe the CORRECT behavior for a
problem reported on the Idea Queue page but not fixed yet.
"""
from harness import OpsHubTestCase, open_finding


class LogbookBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset()
        self.project = self.make_project(asset_id=self.asset)

    def save(self, **form):
        data = dict(body="Replaced oil filter.", log_type="airframe", entry_date="2026-09-01")
        data.update(form)
        return self.client.post(f"/projects/{self.project}/logbook/save", data=data)

    def entries(self):
        return self.q("SELECT * FROM logbook_entries ORDER BY id")

    def make_entry(self, **kw):
        cols = dict(asset_id=self.asset, project_id=self.project, log_type="airframe",
                    entry_date="2026-09-01", body="Did the work.")
        cols.update(kw)
        return self.exec(f"INSERT INTO logbook_entries ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         list(cols.values()))


class SaveEntryTest(LogbookBase):
    def test_happy_path_saves_with_plane_project_and_author(self):
        self.login("tech")
        r = self.save(tach="1234.56", hobbs="1300", section="Engine")
        self.assertEqual(r.status_code, 302)
        e = self.entries()[0]
        self.assertEqual((e["asset_id"], e["project_id"], e["section"]), (self.asset, self.project, "Engine"))
        self.assertEqual((e["tach_hours"], e["hobbs_hours"]), (1234.6, 1300.0))
        self.assertEqual(e["created_by"], "Tech")

    def test_blank_body_saves_nothing(self):
        self.login("tech")
        self.save(body="   ")
        self.assertEqual(len(self.entries()), 0)

    def test_unknown_log_type_falls_back_to_airframe(self):
        self.login("tech")
        self.save(log_type="bogus")
        self.assertEqual(self.entries()[0]["log_type"], "airframe")

    def test_bad_date_falls_back_to_today(self):
        self.login("tech")
        self.save(entry_date="not-a-date")
        self.assertRegex(self.entries()[0]["entry_date"], r"^\d{4}-\d\d-\d\d$")

    def test_missing_project_is_404(self):
        self.login("tech")
        self.assertEqual(self.client.post("/projects/9999/logbook/save", data={"body": "x"}).status_code, 404)

    def test_roles_that_may_not_write_logbooks(self):
        for role in ("inspector", "shop_student", "cfi", "flight_student", "no_roles"):
            with self.subTest(role=role):
                self.login(role)
                r = self.save()
                self.assertIn(r.status_code, (302, 401, 403))
                self.assertEqual(len(self.entries()), 0, role)

    def test_double_submit_makes_two_entries_today(self):
        # Documents current behavior: no de-duplication on a double tap.
        self.login("tech")
        self.save()
        self.save()
        self.assertEqual(len(self.entries()), 2)

    @open_finding("qa-logbook-bad-hours")
    def test_nan_or_infinite_hours_are_not_stored(self):
        self.login("tech")
        self.save(tach="nan", hobbs="inf")
        e = self.entries()[0]
        for col in ("tach_hours", "hobbs_hours"):
            v = e[col]
            self.assertTrue(v is None or (v == v and abs(v) != float("inf")), f"{col}={v!r}")

    @open_finding("qa-logbook-bad-hours")
    def test_negative_hours_are_not_stored(self):
        self.login("tech")
        self.save(tach="-50")
        self.assertIsNone(self.entries()[0]["tach_hours"])


class EditPrintDeleteTest(LogbookBase):
    def test_edit_updates_fields(self):
        eid = self.make_entry()
        self.login("tech")
        r = self.client.post(f"/logbook/{eid}", data=dict(body="Changed", log_type="engine",
                             entry_date="2026-09-02", tach="10", signed_by="A. Mechanic", cert_number="A&P 1"))
        self.assertEqual(r.status_code, 302)
        e = self.q1("SELECT * FROM logbook_entries WHERE id=?", (eid,))
        self.assertEqual((e["body"], e["log_type"], e["entry_date"], e["signed_by"]),
                         ("Changed", "engine", "2026-09-02", "A. Mechanic"))

    def test_edit_blank_body_rejected(self):
        eid = self.make_entry()
        self.login("tech")
        self.client.post(f"/logbook/{eid}", data=dict(body=" ", log_type="engine"))
        self.assertEqual(self.q1("SELECT body FROM logbook_entries WHERE id=?", (eid,))["body"], "Did the work.")

    def test_missing_entry_is_404_everywhere(self):
        self.login("tech")
        self.assertEqual(self.client.get("/logbook/9999").status_code, 404)
        self.assertEqual(self.client.get("/logbook/9999/print").status_code, 404)
        self.assertEqual(self.client.post("/logbook/9999", data={"body": "x"}).status_code, 404)
        self.login("shop_admin")
        self.assertEqual(self.client.post("/logbook/9999/delete").status_code, 404)

    def test_print_page_shows_entry(self):
        eid = self.make_entry(body="Torqued the cowling screws")
        self.login("tech")
        r = self.client.get(f"/logbook/{eid}/print")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Torqued the cowling screws", r.data)

    def test_only_admin_deletes_and_deleted_entry_disappears(self):
        eid = self.make_entry()
        self.login("tech")
        self.assertIn(self.client.post(f"/logbook/{eid}/delete").status_code, (302, 401, 403))
        self.assertIsNone(self.q1("SELECT deleted_at FROM logbook_entries WHERE id=?", (eid,))["deleted_at"])
        self.login("shop_admin")
        self.assertEqual(self.client.post(f"/logbook/{eid}/delete").status_code, 302)
        self.assertEqual(self.client.get(f"/logbook/{eid}").status_code, 404)
        self.assertEqual(self.client.post(f"/logbook/{eid}/delete").status_code, 404)  # double delete
        self.assertNotIn(b"Did the work.", self.client.get("/logbook").data)

    def test_customer_cannot_open_shop_logbook(self):
        eid = self.make_entry()
        self.login("customer")
        for url in (f"/logbook/{eid}", f"/logbook/{eid}/print", "/logbook", f"/assets/{self.asset}/logbook"):
            r = self.client.get(url)
            self.assertNotEqual(r.status_code, 200, url)


class BrowseTest(LogbookBase):
    def test_filters(self):
        a2 = self.make_asset("N999ZZ")
        self.make_entry(body="Alpha engine job", log_type="engine", entry_date="2026-08-01")
        self.make_entry(body="Bravo prop job", log_type="propeller", entry_date="2026-09-15", asset_id=a2)
        self.login("tech")
        def get(qs):
            return self.client.get("/logbook?" + qs).data
        self.assertIn(b"Alpha", get("q=alpha"))
        self.assertNotIn(b"Bravo", get("q=alpha"))
        self.assertNotIn(b"Alpha", get("log_type=propeller"))
        self.assertNotIn(b"Alpha", get("from=2026-09-01"))
        self.assertNotIn(b"Bravo", get("to=2026-08-31"))
        self.assertNotIn(b"Alpha", get(f"asset_id={a2}"))

    def test_garbage_filters_do_not_crash(self):
        self.login("tech")
        for qs in ("from=zzz", "to=9999-99-99", "asset_id=abc", "log_type=%27", "q=%25", "project=_"):
            self.assertEqual(self.client.get("/logbook?" + qs).status_code, 200, qs)

    def test_asset_logbook_tabs(self):
        self.make_entry(log_type="propeller", body="Prop balance")
        self.login("tech")
        self.assertIn(b"Prop balance", self.client.get(f"/assets/{self.asset}/logbook").data)
        self.assertEqual(self.client.get(f"/assets/{self.asset}/logbook?log=zzz").status_code, 200)
        self.assertEqual(self.client.get("/assets/9999/logbook").status_code, 404)


class StarterAndTemplatesTest(LogbookBase):
    def test_starter_page_renders_for_each_project_type_and_bad_input(self):
        self.login("tech")
        for qs in ("", "?type=annual", "?type=bogus", "?date=garbage", "?tach=abc&hobbs=xyz"):
            self.assertEqual(self.client.get(f"/projects/{self.project}/logbook{qs}").status_code, 200, qs)
        self.assertEqual(self.client.get("/projects/9999/logbook").status_code, 404)

    def test_templates_admin_only(self):
        self.login("tech")
        self.assertNotEqual(self.client.get("/logbook/templates").status_code, 200)
        self.login("shop_admin")
        self.assertEqual(self.client.get("/logbook/templates").status_code, 200)

    def test_template_save_reset_and_bad_type(self):
        self.login("shop_admin")
        self.client.post("/logbook/templates", data=dict(project_type="annual", log_type="airframe", body="Custom words"))
        self.assertEqual(self.q1("SELECT body FROM logbook_templates")["body"], "Custom words")
        self.client.post("/logbook/templates", data=dict(project_type="annual", log_type="airframe", action="reset"))
        self.assertIsNone(self.q1("SELECT body FROM logbook_templates"))
        self.assertEqual(self.client.post("/logbook/templates", data=dict(project_type="x", log_type="y")).status_code, 400)

    def test_template_upload_binary_does_not_crash(self):
        import io
        self.login("shop_admin")
        r = self.client.post("/logbook/templates", data=dict(project_type="annual", log_type="airframe",
                             file=(io.BytesIO(b"\xff\xfe\x00bin"), "t.txt")), content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
