"""Project create / edit / intake flows: the job form (blank name, missing
plane, odd dates, who may use it), the intake check (skip, save, re-save,
verify) and the pages that show a job afterwards.

Tests marked @open_finding("qa-...") describe the CORRECT behavior for a
problem reported on the Idea Queue page but not fixed yet.
"""
from harness import OpsHubTestCase, open_finding


class ProjectNewTest(OpsHubTestCase):
    def count(self):
        return self.q1("SELECT COUNT(*) c FROM projects")["c"]

    def test_admin_and_tech_create_a_job_with_the_next_code(self):
        for role in ("shop_admin", "tech"):
            self.login(role)
            before = self.count()
            r = self.client.post("/projects/new", data=dict(name="Oil change " + role))
            self.assertEqual(r.status_code, 302, role)
            self.assertEqual(self.count(), before + 1, role)
        codes = [r["code"] for r in self.q("SELECT code FROM projects ORDER BY id")]
        self.assertEqual(len(set(codes)), len(codes))

    def test_new_job_starts_active_with_intake_pending(self):
        self.login("shop_admin")
        self.client.post("/projects/new", data=dict(name="Fresh job"))
        p = self.q1("SELECT * FROM projects WHERE name='Fresh job'")
        self.assertEqual(p["status"], "active")
        self.assertEqual(p["intake_status"], "pending")

    def test_other_roles_cannot_create_jobs(self):
        for role in ("shop_student", "cfi", "flight_student", "no_roles", "customer", None):
            self.login(role)
            before = self.count()
            self.client.post("/projects/new", data=dict(name="Sneaky"))
            self.assertEqual(self.count(), before, role)

    def test_blank_or_spaces_only_name_is_refused(self):
        self.login("shop_admin")
        for name in ("", "   ", "\t\n"):
            r = self.client.post("/projects/new", data=dict(name=name))
            self.assertEqual(r.status_code, 200, repr(name))
            self.assertEqual(self.count(), 0, repr(name))

    def test_missing_plane_is_a_message_not_a_crash(self):
        self.login("shop_admin")
        r = self.client.post("/projects/new", data=dict(name="Ghost plane", asset_id="99999"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.count(), 0)

    def test_non_numeric_plane_is_not_a_server_error(self):
        self.login("shop_admin")
        r = self.client.post("/projects/new", data=dict(name="Odd plane", asset_id="abc"))
        self.assertLess(r.status_code, 500)

    def test_end_date_before_start_is_pulled_up_to_the_start(self):
        self.login("shop_admin")
        self.client.post("/projects/new", data=dict(name="Backwards", scheduled_date="2026-11-10",
                                                    scheduled_end_date="2026-11-01"))
        p = self.q1("SELECT * FROM projects WHERE name='Backwards'")
        self.assertEqual(p["scheduled_end_date"], "2026-11-10")

    def test_start_date_alone_gets_a_one_day_job(self):
        self.login("shop_admin")
        self.client.post("/projects/new", data=dict(name="One day", scheduled_date="2026-11-10"))
        p = self.q1("SELECT * FROM projects WHERE name='One day'")
        self.assertEqual(p["scheduled_end_date"], "2026-11-10")

    @open_finding("qa-project-bad-dates-saved")
    def test_a_made_up_start_date_is_not_saved(self):
        self.login("shop_admin")
        self.client.post("/projects/new", data=dict(name="Bad date", scheduled_date="not-a-date"))
        p = self.q1("SELECT * FROM projects WHERE name='Bad date'")
        # Either the form is refused or the junk date is dropped.
        self.assertTrue(p is None or p["scheduled_date"] in (None, ""), p and p["scheduled_date"])

    def test_job_with_a_made_up_date_still_opens_everywhere(self):
        self.login("shop_admin")
        self.client.post("/projects/new", data=dict(name="Bad date page", scheduled_date="not-a-date"))
        p = self.q1("SELECT * FROM projects WHERE name='Bad date page'")
        if p is None:
            return
        for url in (f"/projects/{p['id']}", "/projects", "/schedule", "/shop/schedule"):
            r = self.client.get(url)
            self.assertLess(r.status_code, 500, url)

    def test_promised_date_junk_is_dropped_and_owner_switch_needs_a_date(self):
        self.login("shop_admin")
        self.client.post("/projects/new", data=dict(name="Promise", promised_date="soon", promised_show_owner="1"))
        p = self.q1("SELECT * FROM projects WHERE name='Promise'")
        self.assertIsNone(p["promised_date"])
        self.assertEqual(p["promised_show_owner"], 0)

    def test_annual_on_a_plane_is_created_and_intake_page_opens(self):
        aid = self.make_asset("N777QA")
        self.login("shop_admin")
        r = self.client.post("/projects/new", data=dict(name="Annual - N777QA", asset_id=str(aid)))
        self.assertEqual(r.status_code, 302)
        p = self.q1("SELECT * FROM projects WHERE name='Annual - N777QA'")
        self.assertEqual(p["asset_id"], aid)
        self.assertEqual(self.client.get(f"/projects/{p['id']}/intake").status_code, 200)


class ProjectEditTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.pid = self.make_project("Original job")

    def row(self):
        return self.q1("SELECT * FROM projects WHERE id=?", (self.pid,))

    def test_edit_changes_name_and_keeps_status_and_code(self):
        self.login("tech")
        code = self.row()["code"]
        self.exec("UPDATE projects SET status='on_hold' WHERE id=?", (self.pid,))
        r = self.client.post(f"/projects/{self.pid}/edit", data=dict(name="Renamed job", description="x"))
        self.assertEqual(r.status_code, 302)
        p = self.row()
        self.assertEqual((p["name"], p["status"], p["code"]), ("Renamed job", "on_hold", code))

    def test_blank_name_keeps_the_old_name(self):
        self.login("shop_admin")
        r = self.client.post(f"/projects/{self.pid}/edit", data=dict(name="  "))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.row()["name"], "Original job")

    def test_missing_job_and_missing_plane_are_handled(self):
        self.login("shop_admin")
        self.assertEqual(self.client.get("/projects/99999/edit").status_code, 404)
        self.assertEqual(self.client.post("/projects/99999/edit", data=dict(name="x")).status_code, 404)
        r = self.client.post(f"/projects/{self.pid}/edit", data=dict(name="Moved", asset_id="99999"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.row()["name"], "Original job")

    def test_other_roles_cannot_edit(self):
        for role in ("shop_student", "cfi", "flight_student", "no_roles", "customer", None):
            self.login(role)
            self.client.post(f"/projects/{self.pid}/edit", data=dict(name="Hacked " + str(role)))
            self.assertEqual(self.row()["name"], "Original job", role)

    def test_a_trashed_job_cannot_be_edited(self):
        self.login("shop_admin")
        self.exec("UPDATE projects SET deleted_at='2026-01-01T00:00:00' WHERE id=?", (self.pid,))
        r = self.client.post(f"/projects/{self.pid}/edit", data=dict(name="Edited in trash"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.row()["name"], "Original job")

    def test_clearing_dates_and_end_before_start(self):
        self.login("shop_admin")
        self.client.post(f"/projects/{self.pid}/edit", data=dict(name="Original job", scheduled_date="2026-12-05",
                                                                  scheduled_end_date="2026-12-01"))
        self.assertEqual(self.row()["scheduled_end_date"], "2026-12-05")
        self.client.post(f"/projects/{self.pid}/edit", data=dict(name="Original job"))
        self.assertIsNone(self.row()["scheduled_date"])

    @open_finding("qa-project-bad-dates-saved")
    def test_edit_does_not_save_a_made_up_date(self):
        self.login("shop_admin")
        self.client.post(f"/projects/{self.pid}/edit", data=dict(name="Original job", scheduled_date="31/31/2026"))
        self.assertIn(self.row()["scheduled_date"], (None, ""))


class ProjectIntakeTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.pid = self.make_project("Intake job")
        self.exec("UPDATE projects SET intake_status='pending' WHERE id=?", (self.pid,))

    def row(self):
        return self.q1("SELECT * FROM projects WHERE id=?", (self.pid,))

    def test_skip_marks_skipped_once_and_never_overwrites_a_done_check(self):
        self.login("tech")
        r = self.client.post(f"/projects/{self.pid}/intake", data=dict(action="skip"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.row()["intake_status"], "skipped")
        self.exec("UPDATE projects SET intake_status='done' WHERE id=?", (self.pid,))
        self.client.post(f"/projects/{self.pid}/intake", data=dict(action="skip"))
        self.assertEqual(self.row()["intake_status"], "done")

    def test_missing_and_trashed_jobs(self):
        self.login("tech")
        self.assertEqual(self.client.get("/projects/99999/intake").status_code, 404)
        self.assertEqual(self.client.post("/projects/99999/intake", data=dict(action="skip")).status_code, 404)
        self.exec("UPDATE projects SET deleted_at='2026-01-01T00:00:00' WHERE id=?", (self.pid,))
        self.client.post(f"/projects/{self.pid}/intake", data=dict(action="skip"))
        self.assertEqual(self.row()["intake_status"], "pending")

    def test_roles_that_may_not_touch_intake(self):
        for role in ("shop_student", "cfi", "flight_student", "no_roles", "customer", None):
            self.login(role)
            self.client.post(f"/projects/{self.pid}/intake", data=dict(action="skip"))
            self.assertEqual(self.row()["intake_status"], "pending", role)

    def test_verify_only_after_the_check_is_done(self):
        self.login("tech")
        self.client.post(f"/projects/{self.pid}/intake/verify")
        self.assertIsNone(self.row()["intake_verified_by"])
        self.exec("UPDATE projects SET intake_status='done' WHERE id=?", (self.pid,))
        self.client.post(f"/projects/{self.pid}/intake/verify")
        self.assertIsNotNone(self.row()["intake_verified_by"])

    def test_verify_roles_and_missing_job(self):
        self.exec("UPDATE projects SET intake_status='done' WHERE id=?", (self.pid,))
        for role in ("shop_student", "cfi", "flight_student", "no_roles", None):
            self.login(role)
            self.client.post(f"/projects/{self.pid}/intake/verify")
            self.assertIsNone(self.row()["intake_verified_by"], role)
        self.login("shop_admin")
        self.assertEqual(self.client.post("/projects/99999/intake/verify").status_code, 404)

    def test_empty_save_does_not_crash(self):
        self.login("tech")
        r = self.client.post(f"/projects/{self.pid}/intake", data={})
        self.assertLess(r.status_code, 500)

    def test_intake_page_opens_for_every_allowed_role_and_with_junk_saved_data(self):
        self.exec("UPDATE projects SET intake_json='{not json' WHERE id=?", (self.pid,))
        for role in ("shop_admin", "tech", "inspector"):
            if role not in self.users:
                continue
            self.login(role)
            self.assertEqual(self.client.get(f"/projects/{self.pid}/intake").status_code, 200, role)
