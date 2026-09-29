"""Project lifecycle and sub-area flows: status changes, trash / restore /
renumber, adding and renaming sub-areas, ready-for-inspection and sign-off.

Tests marked @open_finding("qa-...") describe the CORRECT behavior for a
problem reported on the Idea Queue page but not fixed yet.
"""
from harness import OpsHubTestCase, seed_row, open_finding


class ProjectStatusTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.pid = self.make_project()

    def status(self):
        return self.q1("SELECT * FROM projects WHERE id=?", (self.pid,))

    def test_admin_and_tech_can_change_status(self):
        for role in ("shop_admin", "tech"):
            self.login(role)
            r = self.client.post(f"/projects/{self.pid}/status", data=dict(status="on_hold"))
            self.assertEqual(r.status_code, 302, role)
            self.assertEqual(self.status()["status"], "on_hold", role)

    def test_other_roles_cannot_change_status(self):
        for role in ("shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            self.client.post(f"/projects/{self.pid}/status", data=dict(status="archived"))
            self.assertEqual(self.status()["status"], "active", role)

    def test_bad_or_blank_status_is_rejected(self):
        self.login("shop_admin")
        for s in ("", "bogus", "COMPLETED", None):
            data = {} if s is None else dict(status=s)
            r = self.client.post(f"/projects/{self.pid}/status", data=data)
            self.assertEqual(r.status_code, 400, s)
        self.assertEqual(self.status()["status"], "active")

    def test_missing_project_is_404(self):
        self.login("shop_admin")
        r = self.client.post("/projects/99999/status", data=dict(status="completed"))
        self.assertEqual(r.status_code, 404)

    def test_completing_stamps_who_and_when_and_reopening_clears_it(self):
        self.login("tech")
        self.client.post(f"/projects/{self.pid}/status", data=dict(status="completed"))
        p = self.status()
        self.assertEqual(p["status"], "completed")
        self.assertTrue(p["completed_at"] and p["completed_by"])
        self.client.post(f"/projects/{self.pid}/status", data=dict(status="active"))
        p = self.status()
        self.assertIsNone(p["completed_at"])
        self.assertIsNone(p["completed_by"])

    def test_completing_twice_keeps_the_first_completion_stamp(self):
        self.login("shop_admin")
        self.client.post(f"/projects/{self.pid}/status", data=dict(status="completed"))
        first = self.status()["completed_at"]
        self.client.post(f"/projects/{self.pid}/status", data=dict(status="completed"))
        self.assertEqual(self.status()["completed_at"], first)


class ProjectTrashTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.make_project("Job A")
        self.b = self.make_project("Job B")
        self.c = self.make_project("Job C")
        self.login("shop_admin")

    def row(self, pid):
        return self.q1("SELECT * FROM projects WHERE id=?", (pid,))

    def test_trash_hides_and_restore_brings_back_with_same_code(self):
        code = self.row(self.c)["code"]
        r = self.client.post(f"/projects/{self.c}/trash")
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(self.row(self.c)["deleted_at"])
        self.assertNotIn(f'href="/projects/{self.c}"'.encode(), self.client.get("/projects").data)
        self.client.post(f"/projects/{self.c}/restore")
        self.assertIsNone(self.row(self.c)["deleted_at"])
        self.assertEqual(self.row(self.c)["code"], code)

    def test_only_admin_can_trash_restore_or_purge(self):
        for role in ("tech", "inspector", "shop_student", "cfi", "no_roles"):
            self.login(role)
            for path in ("trash", "delete"):
                self.client.post(f"/projects/{self.b}/{path}")
            self.assertIsNone(self.row(self.b)["deleted_at"], role)
            self.assertEqual(self.row(self.b)["status"], "active", role)

    def test_missing_projects_are_404_not_500(self):
        for path in ("trash", "delete", "restore", "purge"):
            r = self.client.post(f"/projects/99999/{path}")
            self.assertEqual(r.status_code, 404, path)

    def test_trashing_twice_is_harmless(self):
        self.client.post(f"/projects/{self.b}/trash")
        r = self.client.post(f"/projects/{self.b}/trash")
        self.assertIn(r.status_code, (200, 302))
        self.assertIsNotNone(self.row(self.b)["deleted_at"])

    def test_trashed_project_page_is_not_a_crash(self):
        self.client.post(f"/projects/{self.b}/trash")
        r = self.client.get(f"/projects/{self.b}")
        self.assertLess(r.status_code, 500)

    def test_archive_keeps_the_project_and_its_history(self):
        part = self.make_part(qty=5)
        self.client.post(f"/projects/{self.a}/delete")
        self.assertEqual(self.row(self.a)["status"], "archived")
        self.assertIsNone(self.row(self.a)["deleted_at"])

    def test_renumber_closes_the_gap_and_restore_gets_a_fresh_code(self):
        codes = {p: self.row(p)["code"] for p in (self.a, self.b, self.c)}
        self.client.post(f"/projects/{self.a}/trash")
        yy, num = codes[self.a].split("-")
        r = self.client.post("/projects/renumber", data=dict(yy=int(yy), num=int(num)))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.row(self.b)["code"], codes[self.a])
        self.assertEqual(self.row(self.c)["code"], codes[self.b])
        self.client.post(f"/projects/{self.a}/restore")
        restored = self.row(self.a)
        self.assertIsNone(restored["deleted_at"])
        live = [r["code"] for r in self.q("SELECT code FROM projects WHERE deleted_at IS NULL")]
        self.assertEqual(len(live), len(set(live)), "two live projects share a code")

    def test_renumber_double_submit_does_not_shift_twice(self):
        codes = {p: self.row(p)["code"] for p in (self.a, self.b, self.c)}
        self.client.post(f"/projects/{self.a}/trash")
        yy, num = codes[self.a].split("-")
        data = dict(yy=int(yy), num=int(num))
        self.client.post("/projects/renumber", data=data)
        after_first = [self.row(p)["code"] for p in (self.b, self.c)]
        r = self.client.post("/projects/renumber", data=data)
        self.assertLess(r.status_code, 500)
        self.assertEqual([self.row(p)["code"] for p in (self.b, self.c)], after_first)

    def test_renumber_needs_both_numbers(self):
        for data in ({}, dict(yy=26), dict(num=1), dict(yy="x", num="y")):
            r = self.client.post("/projects/renumber", data=data)
            self.assertEqual(r.status_code, 400, data)

    def test_purge_removes_an_unused_project_for_good(self):
        self.client.post(f"/projects/{self.b}/trash")
        r = self.client.post(f"/projects/{self.b}/purge")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.row(self.b))

    def test_purge_of_a_job_with_parts_or_hours_needs_a_choice(self):
        part = self.make_part(qty=5)
        self.client.post("/api/scan", json=dict(barcode=self.q1("SELECT barcode FROM parts WHERE id=?", (part,))["barcode"],
                                                action="out", qty=2, project_id=self.b, performed_by="Frank"))
        self.client.post(f"/projects/{self.b}/trash")
        self.client.post(f"/projects/{self.b}/purge")
        self.assertIsNotNone(self.row(self.b), "purged without saying what happens to the parts")
        self.assertEqual(self.qty(part), 3)
        self.client.post(f"/projects/{self.b}/purge", data=dict(parts_action="return"))
        self.assertIsNone(self.row(self.b))
        self.assertEqual(self.qty(part), 5, "returned parts must go back on the shelf")


class SubAreaTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.pid = self.make_project()
        self.login("tech")

    def seed(self, table, **values):
        import db
        conn = db.get_db()
        rid = seed_row(conn, table, **values)
        conn.commit()
        conn.close()
        return rid

    def add(self, name="Left wing"):
        return self.client.post(f"/projects/{self.pid}/add_section", data=dict(name=name))

    def sections(self):
        return self.q("SELECT * FROM project_sections WHERE project_id=? ORDER BY id", (self.pid,))

    def test_add_section_and_duplicates_are_ignored(self):
        self.add(); self.add(); self.add("  Left wing  ")
        self.assertEqual([s["name"] for s in self.sections()], ["Left wing"])

    def test_blank_name_is_rejected(self):
        for n in ("", "   "):
            r = self.add(n)
            self.assertEqual(r.status_code, 302)
        self.assertEqual(self.sections(), [])

    def test_add_section_to_missing_or_trashed_project_is_404(self):
        self.assertEqual(self.client.post("/projects/99999/add_section", data=dict(name="x")).status_code, 404)
        self.login("shop_admin")
        self.client.post(f"/projects/{self.pid}/trash")
        self.assertEqual(self.add().status_code, 404)

    def test_rename_moves_history_with_it(self):
        self.add()
        sid = self.sections()[0]["id"]
        part = self.make_part(qty=5)
        self.seed("transactions", part_id=part, project_id=self.pid, section="Left wing", type="out", qty=1)
        self.client.post(f"/projects/{self.pid}/sections/{sid}/rename", data=dict(name="Right wing"))
        self.assertEqual(self.sections()[0]["name"], "Right wing")
        self.assertEqual(self.q1("SELECT section FROM transactions WHERE project_id=?", (self.pid,))["section"], "Right wing")

    def test_rename_rejects_blank_and_clashing_names(self):
        self.add("A"); self.add("B")
        a, b = [s["id"] for s in self.sections()]
        self.client.post(f"/projects/{self.pid}/sections/{a}/rename", data=dict(name="  "))
        self.client.post(f"/projects/{self.pid}/sections/{a}/rename", data=dict(name="B"))
        self.assertEqual([s["name"] for s in self.sections()], ["A", "B"])

    def test_rename_or_complete_someone_elses_section_is_404(self):
        other = self.make_project("Other")
        self.seed("project_sections", project_id=other, name="Elsewhere")
        sid = self.q1("SELECT id FROM project_sections WHERE project_id=?", (other,))["id"]
        for path in ("rename", "complete", "confirm"):
            self.login("shop_admin")
            r = self.client.post(f"/projects/{self.pid}/sections/{sid}/{path}", data=dict(name="Hijack"))
            self.assertEqual(r.status_code, 404, path)

    def test_tech_marks_ready_but_only_inspector_or_admin_completes(self):
        self.add()
        sid = self.sections()[0]["id"]
        self.client.post(f"/projects/{self.pid}/sections/{sid}/complete", data=dict(completed="1"))
        s = self.sections()[0]
        self.assertTrue(s["confirm_requested_at"])
        self.assertIsNone(s["completed_at"], "a tech's check must wait for the inspector")
        # tech cannot confirm
        self.client.post(f"/projects/{self.pid}/sections/{sid}/confirm")
        self.assertIsNone(self.sections()[0]["completed_at"])
        self.login("inspector")
        self.client.post(f"/projects/{self.pid}/sections/{sid}/confirm")
        s = self.sections()[0]
        self.assertTrue(s["completed_at"] and s["completed_by"])

    def test_inspector_send_back_clears_ready_and_completion(self):
        self.add()
        sid = self.sections()[0]["id"]
        self.client.post(f"/projects/{self.pid}/sections/{sid}/complete", data=dict(completed="1"))
        self.login("inspector")
        self.client.post(f"/projects/{self.pid}/sections/{sid}/confirm", data=dict(action="send_back"))
        s = self.sections()[0]
        self.assertIsNone(s["confirm_requested_at"])
        self.assertIsNone(s["completed_at"])
        self.assertTrue(s["sent_back_at"])

    def test_unchecking_clears_everything(self):
        self.add()
        sid = self.sections()[0]["id"]
        self.client.post(f"/projects/{self.pid}/sections/{sid}/complete", data=dict(completed="1"))
        self.login("shop_admin")
        self.client.post(f"/projects/{self.pid}/sections/{sid}/confirm")
        self.login("tech")
        self.client.post(f"/projects/{self.pid}/sections/{sid}/complete", data=dict(completed="0"))
        s = self.sections()[0]
        self.assertIsNone(s["completed_at"])
        self.assertIsNone(s["confirm_requested_at"])

    def test_other_roles_cannot_touch_sections(self):
        self.add()
        sid = self.sections()[0]["id"]
        for role in ("shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            self.client.post(f"/projects/{self.pid}/add_section", data=dict(name="Sneaky"))
            self.client.post(f"/projects/{self.pid}/sections/{sid}/rename", data=dict(name="Sneaky"))
            self.client.post(f"/projects/{self.pid}/sections/{sid}/complete", data=dict(completed="1"))
        self.assertEqual(len(self.sections()), 1)
        self.assertEqual(self.sections()[0]["name"], "Left wing")
        self.assertIsNone(self.sections()[0]["confirm_requested_at"])


if __name__ == "__main__":
    import unittest
    unittest.main()
