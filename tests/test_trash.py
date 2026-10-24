"""Recently Deleted page: viewing, empty trash, restore and purge edge cases.

Tests marked @open_finding("qa-...") describe the CORRECT behavior for a
problem reported on the Idea Queue page but not fixed yet.
"""
from harness import OpsHubTestCase, open_finding


class TrashPageTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N111")
        self.live = self.make_project("Live job")
        self.gone = self.make_project("Gone job", asset_id=self.asset)
        self.gone_asset = self.make_asset("N222")
        self.login("shop_admin")
        self.client.post(f"/projects/{self.gone}/trash")
        self.client.post(f"/assets/{self.gone_asset}/trash")

    def proj(self, pid):
        return self.q1("SELECT * FROM projects WHERE id=?", (pid,))

    def asset_row(self, aid):
        return self.q1("SELECT * FROM assets WHERE id=?", (aid,))

    def test_page_lists_only_deleted_things(self):
        r = self.client.get("/trash")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Gone job", html)
        self.assertIn("N222", html)
        self.assertNotIn("Live job", html)

    def test_only_admins_see_or_use_trash(self):
        for role in ("tech", "inspector", "shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            self.assertIn(self.client.get("/trash").status_code, (302, 403), role)
            self.client.post("/trash/empty")
            self.assertIsNotNone(self.proj(self.gone), role)
            self.assertIsNotNone(self.asset_row(self.gone_asset), role)
        self.login(None)
        self.assertIn(self.client.get("/trash").status_code, (302, 401, 403))
        self.client.post("/trash/empty")
        self.assertIsNotNone(self.proj(self.gone))

    def test_empty_trash_removes_only_deleted_items(self):
        r = self.client.post("/trash/empty")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.proj(self.gone))
        self.assertIsNone(self.asset_row(self.gone_asset))
        self.assertIsNotNone(self.proj(self.live))
        self.assertIsNotNone(self.asset_row(self.asset))

    def test_empty_trash_twice_and_when_empty_is_harmless(self):
        self.client.post("/trash/empty")
        r = self.client.post("/trash/empty")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/trash").status_code, 200)

    def test_empty_trash_with_parts_needs_a_choice_then_returns_them(self):
        part = self.make_part(qty=5)
        bc = self.q1("SELECT barcode FROM parts WHERE id=?", (part,))["barcode"]
        self.client.post(f"/projects/{self.gone}/restore")
        self.client.post("/api/scan", json=dict(barcode=bc, action="out", qty=2,
                                                project_id=self.gone, performed_by="Frank"))
        self.client.post(f"/projects/{self.gone}/trash")
        self.client.post("/trash/empty")
        self.assertIsNotNone(self.proj(self.gone), "emptied without asking about the parts")
        self.client.post("/trash/empty", data=dict(parts_action="return"))
        self.assertIsNone(self.proj(self.gone))
        self.assertEqual(self.qty(part), 5)

    def test_restore_missing_items_is_404(self):
        self.assertEqual(self.client.post("/projects/99999/restore").status_code, 404)
        self.assertEqual(self.client.post("/assets/99999/restore").status_code, 404)
        self.assertEqual(self.client.post("/projects/99999/purge").status_code, 404)
        self.assertEqual(self.client.post("/assets/99999/purge").status_code, 404)

    def test_restore_brings_back_project_and_asset(self):
        self.client.post(f"/projects/{self.gone}/restore")
        self.client.post(f"/assets/{self.gone_asset}/restore")
        self.assertIsNone(self.proj(self.gone)["deleted_at"])
        self.assertIsNone(self.asset_row(self.gone_asset)["deleted_at"])
        self.assertEqual(self.client.get("/trash").status_code, 200)

    def test_restore_twice_is_harmless(self):
        self.client.post(f"/projects/{self.gone}/restore")
        r = self.client.post(f"/projects/{self.gone}/restore")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.proj(self.gone)["deleted_at"])

    def test_tag_of_deleted_aircraft_cannot_be_reused_without_a_crash(self):
        r = self.client.post("/assets/new", data=dict(tag="N222", name="Another"))
        self.assertLess(r.status_code, 500)

    @open_finding("qa-purge-live-project")
    def test_purge_of_a_job_that_is_not_in_trash_is_refused(self):
        r = self.client.post(f"/projects/{self.live}/purge")
        self.assertIsNotNone(self.proj(self.live), "a live job was permanently deleted without being in Recently Deleted")

    @open_finding("qa-purge-live-project")
    def test_purge_of_an_aircraft_that_is_not_in_trash_is_refused(self):
        self.client.post(f"/assets/{self.asset}/purge")
        self.assertIsNotNone(self.asset_row(self.asset), "a live aircraft was permanently deleted without being in Recently Deleted")

    def test_job_on_deleted_aircraft_keeps_working_after_aircraft_purge(self):
        self.client.post(f"/assets/{self.asset}/trash")
        self.client.post(f"/assets/{self.asset}/purge")
        self.assertIsNone(self.proj(self.gone)["asset_id"])
        self.client.post(f"/projects/{self.gone}/restore")
        r = self.client.get(f"/projects/{self.gone}")
        self.assertEqual(r.status_code, 200)
