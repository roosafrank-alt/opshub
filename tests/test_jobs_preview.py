"""Preview build, Winds Aloft jobs / aircraft / squawks / manuals (JOBS-xx ids).

One small test per change. The id is in each test name so a failure points at
the approved change it belongs to."""
import re

import db
from harness import OpsHubTestCase


def get(client, url):
    return client.get(url).get_data(as_text=True)


class IntakeTest(OpsHubTestCase):
    """JOBS-01, JOBS-02: every shop role does the Intake Check; apprentice work is verified."""

    def new_job(self):
        pid = self.make_project()
        self.exec("UPDATE projects SET intake_status='pending' WHERE id=?", (pid,))
        return pid

    def intake_form(self):
        d = {"notes": "ok"}
        for k in ("mags", "oil", "brakes", "gauges"):
            d[k + "_status"] = "ok"
        return d

    def test_jobs_01_apprentice_and_inspector_land_on_the_intake_not_the_dashboard(self):
        for role in ("shop_student", "inspector"):
            pid = self.new_job()
            c = self.login(role)
            r = c.get(f"/projects/{pid}")
            self.assertEqual(r.status_code, 302, role)
            self.assertIn(f"/projects/{pid}/intake", r.headers["Location"], role)
            self.assertEqual(c.get(f"/projects/{pid}/intake").status_code, 200, role)

    def test_jobs_01_apprentice_intake_needs_verification_then_a_tech_verifies(self):
        pid = self.new_job()
        c = self.login("shop_student")
        r = c.post(f"/projects/{pid}/intake", data=self.intake_form())
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM projects WHERE id=?", (pid,))
        self.assertEqual((row["intake_status"], row["intake_by_role"]), ("done", "apprentice"))
        body = get(c, f"/projects/{pid}")
        self.assertIn("Needs verification", body)
        self.assertNotIn(f"/projects/{pid}/intake/verify", body)  # an apprentice cannot verify
        c.post(f"/projects/{pid}/intake/verify")
        self.assertIsNone(self.q1("SELECT intake_verified_at a FROM projects WHERE id=?", (pid,))["a"])
        c = self.login("tech")
        body = get(c, f"/projects/{pid}")
        self.assertIn(f"/projects/{pid}/intake/verify", body)
        c.post(f"/projects/{pid}/intake/verify")
        row = self.q1("SELECT * FROM projects WHERE id=?", (pid,))
        self.assertEqual(row["intake_verified_by"], "Tech")
        self.assertTrue(row["intake_verified_at"])
        body = get(c, f"/projects/{pid}")
        self.assertIn("Verified by Tech", body)
        self.assertNotIn("Needs verification", body)

    def test_jobs_01_a_tech_intake_needs_no_verification(self):
        pid = self.new_job()
        c = self.login("tech")
        c.post(f"/projects/{pid}/intake", data=self.intake_form())
        self.assertEqual(self.q1("SELECT intake_by_role r FROM projects WHERE id=?", (pid,))["r"], "tech")
        self.assertNotIn("Needs verification", get(c, f"/projects/{pid}"))

    def test_jobs_02_pending_intake_has_no_jump_to_project_but_a_skip_button(self):
        pid = self.new_job()
        c = self.login("tech")
        body = get(c, f"/projects/{pid}/intake")
        self.assertNotIn("Jump to Project", body)
        self.assertNotIn("(shortcut: P)", body)
        self.assertIn("Skip for now - open project", body)
        c.post(f"/projects/{pid}/intake", data=self.intake_form())
        body = get(c, f"/projects/{pid}/intake")
        self.assertIn("Back to project", body)
        self.assertNotIn("Skip for now", body)

    def test_jobs_01_skip_records_who_and_apprentice_role(self):
        pid = self.new_job()
        c = self.login("shop_student")
        c.post(f"/projects/{pid}/intake", data={"action": "skip"})
        row = self.q1("SELECT intake_status s, intake_by_role r FROM projects WHERE id=?", (pid,))
        self.assertEqual((row["s"], row["r"]), ("skipped", "apprentice"))


class JobPageButtonsTest(OpsHubTestCase):
    """JOBS-03, JOBS-38."""

    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N1")
        self.pid = self.make_project(asset_id=self.asset)
        self.exec("UPDATE projects SET intake_status='done' WHERE id=?", (self.pid,))

    def test_jobs_03_apprentice_sees_no_buttons_that_would_refuse_them(self):
        body = get(self.login("shop_student"), f"/projects/{self.pid}")
        for gone in (f"/projects/{self.pid}/edit", "Mark Completed", "Materials CSV", "Invoice", "Archive",
                     "trash-form", f"/projects/{self.pid}/label", "/assets/"):
            self.assertNotIn(gone, body, gone)
        self.assertIn("Labor Codes", body)
        self.assertIn("Job Sheet", body)
        self.assertIn("Intake", body)

    def test_jobs_03_inspector_keeps_the_plane_link_and_confirm_controls(self):
        self.exec("INSERT INTO project_sections (project_id, name, created_at, confirm_requested_at, confirm_requested_by) "
                  "VALUES (?, 'Brakes', ?, ?, 'Tech')", (self.pid, db.now_iso(), db.now_iso()))
        body = get(self.login("inspector"), f"/projects/{self.pid}")
        self.assertIn(f"/assets/{self.asset}", body)
        self.assertIn("Send Back", body)
        self.assertNotIn("Mark Completed", body)

    def test_jobs_03_admin_still_sees_everything(self):
        body = get(self.login("shop_admin"), f"/projects/{self.pid}")
        for here in (f"/projects/{self.pid}/edit", "Mark Completed", "Materials CSV", "Invoice", "Archive", "trash-form"):
            self.assertIn(here, body, here)

    def test_jobs_03_projects_list_buttons_follow_the_role(self):
        self.assertIn("New Project", get(self.login("tech"), "/projects"))
        body = get(self.login("shop_student"), "/projects")
        self.assertNotIn("New Project", body)
        self.assertNotIn("/trash", body)
        self.assertNotIn(f'href="/assets/{self.asset}"', body)

    def test_jobs_38_deleted_job_shows_a_banner_and_refuses_changes(self):
        self.exec("UPDATE projects SET deleted_at=? WHERE id=?", (db.now_iso(), self.pid))
        c = self.login("shop_admin")
        body = get(c, f"/projects/{self.pid}")
        self.assertIn("This job is in Recently Deleted", body)
        self.assertIn(f"/projects/{self.pid}/restore", body)
        self.assertNotIn("Mark Completed", body)
        self.assertNotIn(f"/projects/{self.pid}/edit", body)
        c.post(f"/projects/{self.pid}/status", data={"status": "on_hold"})
        self.assertEqual(self.q1("SELECT status FROM projects WHERE id=?", (self.pid,))["status"], "active")
        c.post(f"/projects/{self.pid}/edit", data={"name": "Changed"})
        self.assertNotEqual(self.q1("SELECT name FROM projects WHERE id=?", (self.pid,))["name"], "Changed")

    def test_jobs_38_restore_from_the_job_comes_back_to_the_job(self):
        self.exec("UPDATE projects SET deleted_at=? WHERE id=?", (db.now_iso(), self.pid))
        r = self.login("shop_admin").post(f"/projects/{self.pid}/restore", data={"next": "project"})
        self.assertTrue(r.headers["Location"].endswith(f"/projects/{self.pid}"))

    def test_jobs_25_restore_from_the_deleted_pill_comes_back_to_the_pill(self):
        self.exec("UPDATE projects SET deleted_at=? WHERE id=?", (db.now_iso(), self.pid))
        c = self.login("shop_admin")
        self.assertIn("Recently Deleted", get(c, "/projects?status=deleted"))
        r = c.post(f"/projects/{self.pid}/restore", data={"next": "projects_deleted"})
        self.assertIn("/projects?status=deleted", r.headers["Location"])


class CostsAndHistoryTest(OpsHubTestCase):
    """JOBS-06, JOBS-07."""

    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N1")
        self.live = self.make_project("Live job", asset_id=self.asset)
        self.gone = self.make_project("Binned job", asset_id=self.asset)
        self.exec("UPDATE projects SET deleted_at=? WHERE id=?", (db.now_iso(), self.gone))

    def test_jobs_06_deleted_jobs_stay_out_of_history_and_the_badge(self):
        body = get(self.login("shop_admin"), f"/assets/{self.asset}")
        self.assertIn("Live job", body)
        self.assertNotIn("Binned job", body)
        self.assertIn("(1 project)", body)
        self.assertIn("1 project<", get(self.login("shop_admin"), "/assets").replace("</span>", "<"))

    def test_jobs_07_parts_cost_only_for_admin(self):
        pid = self.live
        part = self.make_part()
        self.exec("INSERT INTO transactions (part_id, project_id, type, qty, performed_by, source, created_at) "
                  "VALUES (?, ?, 'out', 2, 'Frank', 'assigned', ?)", (part, pid, db.now_iso()))
        self.assertIn("Parts cost so far", get(self.login("shop_admin"), "/projects"))
        self.assertIn("Total parts cost across all years", get(self.login("shop_admin"), f"/assets/{self.asset}"))
        for role in ("tech", "inspector"):
            c = self.login(role)
            self.assertNotIn("Parts cost so far", get(c, "/projects"), role)
            self.assertNotIn("Total parts cost across all years", get(c, f"/assets/{self.asset}"), role)


class AircraftPageRolesTest(OpsHubTestCase):
    """JOBS-04, JOBS-05, JOBS-15, JOBS-16, JOBS-17, JOBS-22, JOBS-23, JOBS-24."""

    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N1")
        self.exec("UPDATE assets SET hobbs_hours=10, hobbs_updated_at='2026-09-30 14:22:11', "
                  "hobbs_updated_by='Shop - Frank' WHERE id=?", (self.asset,))
        self.exec("INSERT INTO maintenance_items (asset_id, name, type, interval_hours, hour_type, active, created_at, updated_at) "
                  "VALUES (?, 'Oil change', 'hours', 50, 'tach', 1, ?, ?)", (self.asset, db.now_iso(), db.now_iso()))

    def test_jobs_04_tech_sees_edit_and_maintenance_buttons_but_not_delete(self):
        body = get(self.login("tech"), f"/assets/{self.asset}")
        self.assertIn(f"/assets/{self.asset}/edit", body)
        self.assertIn(f"/assets/{self.asset}/maintenance/new", body)
        self.assertNotIn("asset-trash-form", body)
        self.assertNotIn("/maintenance/1/delete", body)
        self.assertIn("asset-trash-form", get(self.login("shop_admin"), f"/assets/{self.asset}"))

    def test_jobs_04_aircraft_list_new_button_is_admin_only(self):
        self.assertNotIn("New Aircraft", get(self.login("tech"), "/assets"))
        self.assertIn("New Aircraft", get(self.login("shop_admin"), "/assets"))

    def test_jobs_05_inspector_page_has_no_buttons_that_refuse_them(self):
        self.exec("INSERT INTO plane_todos (asset_id, description, created_at) VALUES (?, 'Fix seat', ?)",
                  (self.asset, db.now_iso()))
        body = get(self.login("inspector"), f"/assets/{self.asset}")
        for gone in ("update_hours", "Add Photo(s)", "/todo/new", "Mark done", "maintenance/new", "Complete</button>",
                     "Start job", "/assets/%d/edit" % self.asset, "Log new reading"):
            self.assertNotIn(gone, body, gone)
        self.assertIn("Fix seat", body)
        self.assertIn("View Checks", body)

    def test_jobs_05_16_inspector_can_read_logbook_oil_and_compression(self):
        c = self.login("inspector")
        for url in (f"/assets/{self.asset}/logbook", "/logbook", f"/assets/{self.asset}/oil",
                    f"/assets/{self.asset}/compression", "/assets", "/my-tasks"):
            self.assertEqual(c.get(url).status_code, 200, url)
        # reading is allowed; logging a check is not
        c.post(f"/assets/{self.asset}/compression", data={"cyl1": "70", "cyl2": "71"})
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM compression_checks")["n"], 0)

    def test_jobs_16_inspector_can_open_an_entry_and_the_starter_but_not_save(self):
        pid = self.make_project(asset_id=self.asset)
        eid = self.exec("INSERT INTO logbook_entries (asset_id, project_id, entry_date, body, log_type, created_at, updated_at) "
                        "VALUES (?, ?, '2026-09-01', 'Did it', 'airframe', ?, ?)", (self.asset, pid, db.now_iso(), db.now_iso()))
        c = self.login("inspector")
        for url in (f"/logbook/{eid}", f"/logbook/{eid}/print", f"/projects/{pid}/logbook"):
            self.assertEqual(c.get(url).status_code, 200, url)
        self.assertNotIn("Save changes", get(c, f"/logbook/{eid}"))
        c.post(f"/logbook/{eid}", data={"body": "Changed"})
        self.assertEqual(self.q1("SELECT body FROM logbook_entries WHERE id=?", (eid,))["body"], "Did it")

    def test_jobs_17_manuals_open_for_shop_roles_but_only_admin_manages(self):
        mid = self.exec("INSERT INTO manuals (title, manual_type, filename, page_count, created_at) "
                        "VALUES ('Cessna 172 IPC', 'parts', 'x.pdf', 0, ?)", (db.now_iso(),))
        for role in ("tech", "inspector", "shop_student"):
            c = self.login(role)
            self.assertEqual(c.get("/manuals").status_code, 200, role)
            lst = get(c, "/manuals")
            self.assertNotIn("Upload Manual", lst, role)
            self.assertNotIn(f"/manuals/{mid}/edit", lst, role)
            self.assertNotIn(f"/manuals/{mid}/delete", lst, role)
            page = get(c, f"/manuals/{mid}")
            self.assertIn("Open PDF", page, role)
            self.assertNotIn(f"/manuals/{mid}/edit", page, role)
        c = self.login("shop_admin")
        self.assertIn("Upload Manual", get(c, "/manuals"))
        self.assertIn(f"/manuals/{mid}/edit", get(c, f"/manuals/{mid}"))
        c = self.login("tech")
        c.post(f"/manuals/{mid}/delete")
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM manuals")["n"], 1)

    def test_jobs_22_completed_by_defaults_to_the_logged_in_person(self):
        body = get(self.login("tech"), f"/assets/{self.asset}")
        self.assertIn('var loggedIn = "Tech"', body)
        self.assertIn("var sharedShopLogin = false", body)

    def test_jobs_22_on_the_shared_shop_login_it_uses_the_last_name_typed_on_the_device(self):
        uid = self.exec("INSERT INTO users (name, username, password_hash, active, shop_role, created_at) "
                        "VALUES ('Shop', 'shop', 'x', 1, 'tech', ?)", (db.now_iso(),))
        self.users["shop"] = self.q1("SELECT * FROM users WHERE id=?", (uid,))
        body = get(self.login("shop"), f"/assets/{self.asset}")
        self.assertIn("var sharedShopLogin = true", body)

    def test_jobs_23_timestamps_use_the_app_date_format(self):
        body = get(self.login("shop_admin"), f"/assets/{self.asset}")
        self.assertRegex(body, r"Updated \w{3}, Sep 30, 2026 (14:22|2:22 PM) by Shop - Frank")  # FLY-12 format
        self.exec("UPDATE assets SET deleted_at='2026-09-30 14:22:11' WHERE id=?", (self.asset,))
        trash = get(self.login("shop_admin"), "/trash")
        self.assertRegex(trash, r"Sep 30, 2026 (14:22|2:22 PM)")  # FLY-12 format
        self.assertNotIn("14:22:11", trash)

    def test_jobs_24_never_says_asset(self):
        c = self.login("shop_admin")
        self.exec("DELETE FROM maintenance_items")
        self.exec("DELETE FROM assets")
        self.assertIn("No aircraft yet", get(c, "/assets"))
        self.assertIn("No deleted aircraft", get(c, "/trash"))
        a = self.make_asset("N9")
        self.assertIn("Delete this aircraft?", get(c, f"/assets/{a}"))
        r = c.post("/assets/new", data={"tag": "N9"})
        self.assertIn("An aircraft with tail number &#39;N9&#39; already exists.", r.get_data(as_text=True))
        form = get(c, "/projects/new")
        self.assertNotIn("asset profile", form)
        self.assertNotIn("Aircraft / Plane", form)
        self.assertIn("Link this job to a plane", form)
        self.assertIn("Annual inspection, replace left brake pads", form)

    def test_jobs_26_orphan_history_template_is_gone(self):
        import os
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(db.__file__), "templates", "asset_history.html")))


class SquawkTest(OpsHubTestCase):
    """JOBS-08, JOBS-09, JOBS-29, JOBS-35."""

    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N1")
        self.tech = self.users["tech"]["id"]

    def squawk(self, **cols):
        base = dict(asset_id=self.asset, notes="Left brake soft", reported_by="Frank", reported_at=db.now_iso())
        base.update(cols)
        names = ",".join(base)
        return self.exec(f"INSERT INTO plane_squawks ({names}) VALUES ({','.join('?' * len(base))})", list(base.values()))

    def test_jobs_08_a_signed_off_squawk_can_be_reopened_to_new(self):
        sid = self.squawk(acknowledged_at=db.now_iso(), assigned_to=self.tech, repaired_at=db.now_iso(), repaired_by="Pat")
        for role in ("shop_admin", "tech", "inspector"):
            self.assertIn(f"/squawks/quick/{sid}/reopen", get(self.login(role), "/squawks?step=done"), role)
        c = self.login("inspector")
        r = c.post(f"/squawks/quick/{sid}/reopen")
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM plane_squawks WHERE id=?", (sid,))
        self.assertIsNone(row["repaired_at"])
        self.assertIsNone(row["acknowledged_at"])
        self.assertIsNone(row["assigned_to"])
        self.assertEqual(row["reopened_by"], "Inspector")
        page = get(c, "/squawks?step=new")
        self.assertIn("Reopened by Inspector on", page)
        self.assertIn("I'll take it", get(self.login("shop_admin"), "/squawks?step=new"))

    def test_jobs_08_apprentice_cannot_reopen(self):
        sid = self.squawk(acknowledged_at=db.now_iso(), assigned_to=self.tech, repaired_at=db.now_iso())
        self.login("shop_student").post(f"/squawks/quick/{sid}/reopen")
        self.assertIsNotNone(self.q1("SELECT repaired_at r FROM plane_squawks WHERE id=?", (sid,))["r"])

    def test_jobs_09_acknowledged_squawk_with_nobody_on_it_is_new_not_working(self):
        self.squawk(acknowledged_at=db.now_iso(), acknowledged_by="Admin")
        c = self.login("shop_admin")
        body = get(c, "/squawks")
        self.assertIn("New 1", body)
        self.assertIn("Working 0", body)
        self.assertIn("Assign to...", body)
        self.assertNotIn("Mark as Repaired", body)

    def test_jobs_09_working_shows_once_someone_accepted_it(self):
        self.squawk(acknowledged_at=db.now_iso(), assigned_to=self.tech, worker_acknowledged_at=db.now_iso())
        body = get(self.login("shop_admin"), "/squawks?step=working")
        self.assertIn("Mark as Repaired", body)

    def test_jobs_35_new_squawk_only_in_the_banner_assigned_one_only_in_the_todo_card(self):
        self.squawk(notes="Radio static")
        self.squawk(notes="Flat tire", acknowledged_at=db.now_iso(), assigned_to=self.tech)
        body = get(self.login("shop_admin"), f"/assets/{self.asset}")
        self.assertEqual(body.count("Radio static"), 1)
        self.assertEqual(body.count("Flat tire"), 1)
        self.assertIn("1 New Squawk", body)

    def test_jobs_29_squawk_wording(self):
        c = self.login("shop_admin")
        self.assertIn("Report a Squawk", get(c, f"/assets/{self.asset}"))
        r = c.post(f"/assets/{self.asset}/squawk", data={"notes": "Cracked light"}, follow_redirects=True)
        self.assertIn("Squawk reported - it&#39;s on the Squawks page as New.", r.get_data(as_text=True))
        r = c.post("/squawks/new", data={"asset_id": self.asset, "notes": "Dent"}, follow_redirects=True)
        self.assertIn("Squawk reported - it&#39;s on the Squawks page as New.", r.get_data(as_text=True))
        self.assertIn("Report squawk", r.get_data(as_text=True))
        self.assertNotIn("Issue reported", r.get_data(as_text=True))


class DiscrepancyTest(OpsHubTestCase):
    """JOBS-10, JOBS-11, JOBS-12, JOBS-13, JOBS-14."""

    def setUp(self):
        super().setUp()
        self.pid = self.make_project()
        self.exec("UPDATE projects SET intake_status='done' WHERE id=?", (self.pid,))
        self.sid = self.exec("INSERT INTO project_sections (project_id, name, created_at) VALUES (?, 'Brakes', ?)",
                             (self.pid, db.now_iso()))

    def section(self):
        return self.q1("SELECT * FROM project_sections WHERE id=?", (self.sid,))

    def test_jobs_10_duplicate_discrepancy_says_so(self):
        c = self.login("tech")
        r = c.post(f"/projects/{self.pid}/add_section", data={"name": "Brakes"}, follow_redirects=True)
        self.assertIn("A discrepancy named &#39;Brakes&#39; is already on this job.", r.get_data(as_text=True))
        self.assertNotIn("Discrepancy &#39;Brakes&#39; added", r.get_data(as_text=True))

    def test_jobs_11_one_word_for_the_folders(self):
        part = self.make_part()
        self.exec("INSERT INTO transactions (part_id, project_id, type, qty, performed_by, section, source, created_at) "
                  "VALUES (?, ?, 'out', 1, 'Frank', 'Brakes', 'assigned', ?)", (part, self.pid, db.now_iso()))
        lab = self.exec("INSERT INTO laborers (name, code) VALUES ('Joe', 'L-1')")
        self.exec("INSERT INTO labor_sessions (laborer_id, project_id, section, started_at, ended_at, hours, rate, cost) "
                  "VALUES (?, ?, 'Brakes', ?, ?, 1, 10, 10)", (lab, self.pid, db.now_iso(), db.now_iso()))
        body = get(self.login("tech"), f"/projects/{self.pid}")
        for new in ("Rename this discrepancy", "Print this discrepancy's QR code", "+ Add new discrepancy...",
                    ">Discrepancy</th>"):
            self.assertIn(new, body, new)
        for old in ("Rename this area", "Area / Sub-System", "Add new area", ">Area</th>", ">Task</th>"):
            self.assertNotIn(old, body, old)
        self.assertIn("Scan a discrepancy's code", get(self.login("tech"), f"/projects/{self.pid}/labor_codes"))
        self.assertIn("Discrepancy entries", get(self.login("tech"), f"/projects/{self.pid}/logbook"))

    def test_jobs_12_ticking_asks_first_and_says_what_happened(self):
        c = self.login("tech")
        body = get(c, f"/projects/{self.pid}")
        self.assertIn("Mark 'Brakes' ready for confirmation? An Inspector or admin will still need to confirm it.", body)
        self.assertNotIn("onchange=\"this.form.submit()\"", body)
        r = c.post(f"/projects/{self.pid}/sections/{self.sid}/complete", data={"completed": "1"}, follow_redirects=True)
        self.assertIn("Marked ready - an Inspector or admin needs to confirm it.", r.get_data(as_text=True))
        r = c.post(f"/projects/{self.pid}/sections/{self.sid}/complete", data={"completed": "0"}, follow_redirects=True)
        self.assertIn("Back to open.", r.get_data(as_text=True))
        self.assertIsNone(self.section()["confirm_requested_at"])

    def confirm(self):
        self.login("inspector").post(f"/projects/{self.pid}/sections/{self.sid}/confirm")
        self.assertIsNotNone(self.section()["completed_at"])

    def test_jobs_13_a_tech_cannot_undo_an_inspectors_sign_off(self):
        self.confirm()
        c = self.login("tech")
        body = get(c, f"/projects/{self.pid}")
        self.assertIn("only an admin or inspector can reopen it", body)
        c.post(f"/projects/{self.pid}/sections/{self.sid}/complete", data={"completed": "0"})
        self.assertIsNotNone(self.section()["completed_at"])

    def test_jobs_13_admin_or_inspector_can_reopen_with_a_note(self):
        self.confirm()
        c = self.login("inspector")
        self.assertIn("Undo the sign-off on 'Brakes'?", get(c, f"/projects/{self.pid}"))
        c.post(f"/projects/{self.pid}/sections/{self.sid}/complete", data={"completed": "0"})
        row = self.section()
        self.assertIsNone(row["completed_at"])
        self.assertEqual(row["reopened_by"], "Inspector")
        self.assertIn("reopened by Inspector", get(c, f"/projects/{self.pid}"))

    def test_jobs_13_reopening_also_reopens_the_linked_squawk(self):
        a = self.make_asset("N2")
        qid = self.exec("INSERT INTO plane_squawks (asset_id, notes, reported_at, acknowledged_at, assigned_to, repaired_at) "
                        "VALUES (?, 'x', ?, ?, ?, ?)", (a, db.now_iso(), db.now_iso(), self.users["tech"]["id"], db.now_iso()))
        self.exec("UPDATE project_sections SET linked_squawk_kind='quick', linked_squawk_id=?, completed_at=? WHERE id=?",
                  (qid, db.now_iso(), self.sid))
        self.login("shop_admin").post(f"/projects/{self.pid}/sections/{self.sid}/complete", data={"completed": "0"})
        self.assertIsNone(self.q1("SELECT repaired_at r FROM plane_squawks WHERE id=?", (qid,))["r"])

    def test_jobs_14_every_status_change_says_so_and_on_hold_asks(self):
        c = self.login("shop_admin")
        code = self.q1("SELECT code FROM projects WHERE id=?", (self.pid,))["code"]
        body = get(c, f"/projects/{self.pid}")
        self.assertIn("Put this job on hold?", body)
        for status, words in (("on_hold", "put on hold."), ("active", "marked active."), ("completed", "marked completed.")):
            r = c.post(f"/projects/{self.pid}/status", data={"status": status}, follow_redirects=True)
            self.assertIn(f"Job {code} {words}", r.get_data(as_text=True), status)

    def test_jobs_14_dismissing_a_reschedule_request_names_the_job(self):
        self.exec("UPDATE projects SET customer_reschedule_requested_at=? WHERE id=?", (db.now_iso(), self.pid))
        code = self.q1("SELECT code FROM projects WHERE id=?", (self.pid,))["code"]
        r = self.login("shop_admin").post(f"/projects/{self.pid}/reschedule/dismiss", follow_redirects=True)
        self.assertIn(f"Reschedule request for {code} cleared.", r.get_data(as_text=True))


class JobFormTest(OpsHubTestCase):
    """JOBS-19, JOBS-20, JOBS-39."""

    def create(self, **data):
        data.setdefault("name", "Test job")
        c = self.login("shop_admin")
        c.post("/projects/new", data=data)
        return self.q1("SELECT * FROM projects ORDER BY id DESC")

    def test_jobs_39_through_defaults_follow_the_job_type(self):
        oil = self.create(scheduled_date="2026-10-05", quick_types="Oil Change", description="Oil Change")
        self.assertEqual(oil["scheduled_end_date"], "2026-10-05")
        annual = self.create(scheduled_date="2026-10-05", quick_types="Annual Inspection", description="Annual Inspection")
        self.assertEqual(annual["scheduled_end_date"], "2026-10-11")
        plain = self.create(scheduled_date="2026-10-05")
        self.assertEqual(plain["scheduled_end_date"], "2026-10-05")
        typed = self.create(scheduled_date="2026-10-05", scheduled_end_date="2026-10-08", quick_types="Oil Change")
        self.assertEqual(typed["scheduled_end_date"], "2026-10-08")

    def test_jobs_39_templates_page_sets_the_default_days(self):
        c = self.login("shop_admin")
        body = get(c, "/manage/task-templates")
        self.assertIn("Default time block", body)
        self.assertIn('name="days_Oil Change" value="1"', body)
        self.assertIn('name="days_Annual Inspection" value="7"', body)
        c.post("/manage/task-templates/days", data={"days_Oil Change": "2", "days_Annual Inspection": "10",
                                                    "days_100hr Inspection": "3", "days_Maintenance": ""})
        self.assertEqual(self.q1("SELECT days FROM task_template_days WHERE quick_type='100hr Inspection'")["days"], 3)
        job = self.create(scheduled_date="2026-10-05", quick_types="Annual Inspection,Oil Change")
        self.assertEqual(job["scheduled_end_date"], "2026-10-14")
        c.post("/manage/task-templates/days", data={"days_Oil Change": "abc"})
        self.assertEqual(self.q1("SELECT days FROM task_template_days WHERE quick_type='Oil Change'")["days"], 2)
        self.login("tech")
        self.assertNotEqual(self.client.post("/manage/task-templates/days", data={"days_Oil Change": "9"}).status_code, 200)
        self.assertEqual(self.q1("SELECT days FROM task_template_days WHERE quick_type='Oil Change'")["days"], 2)

    def test_jobs_39_edit_with_blank_through_uses_the_type_found_in_the_description(self):
        pid = self.make_project()
        c = self.login("shop_admin")
        c.post(f"/projects/{pid}/edit", data={"name": "Annual", "description": "Annual Inspection",
                                              "scheduled_date": "2026-10-05"})
        self.assertEqual(self.q1("SELECT scheduled_end_date e FROM projects WHERE id=?", (pid,))["e"], "2026-10-11")

    def test_jobs_19_edit_form_presses_buttons_found_in_the_description_and_hides_the_preset_note(self):
        pid = self.make_project()
        self.exec("UPDATE projects SET description='Annual Inspection, new tires' WHERE id=?", (pid,))
        body = get(self.login("shop_admin"), f"/projects/{pid}/edit")
        self.assertIn('data-editing="1"', body)
        self.assertIn("tap to add or remove just that word", body)
        self.assertNotIn("also adds its Required preset Discrepancies", body)
        new_body = get(self.client, "/projects/new")
        self.assertIn("also adds its Required preset Discrepancies", new_body)


class CompressionTest(OpsHubTestCase):
    """JOBS-27."""

    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N1")
        self.cid = self.exec("INSERT INTO compression_checks (asset_id, checked_date, cyl1, cyl2, created_at) "
                             "VALUES (?, '2026-09-01', 70, 71, ?)", (self.asset, db.now_iso()))

    def test_jobs_27_edit_prefills_and_saves_the_correction(self):
        c = self.login("tech")
        body = get(c, f"/assets/{self.asset}/compression?edit={self.cid}")
        self.assertIn('name="check_id"', body)
        self.assertIn('value="70"', body)
        c.post(f"/assets/{self.asset}/compression", data={"check_id": self.cid, "checked_date": "2026-09-02",
                                                         "cyl1": "75", "cyl2": "71"})
        row = self.q1("SELECT * FROM compression_checks WHERE id=?", (self.cid,))
        self.assertEqual((row["cyl1"], row["checked_date"]), (75, "2026-09-02"))
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM compression_checks")["n"], 1)

    def test_jobs_27_remove_a_check_but_only_admin_or_tech(self):
        self.login("inspector").post(f"/assets/{self.asset}/compression/{self.cid}/delete")
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM compression_checks")["n"], 1)
        c = self.login("shop_admin")
        self.assertIn(f"/compression/{self.cid}/delete", get(c, f"/assets/{self.asset}/compression"))
        c.post(f"/assets/{self.asset}/compression/{self.cid}/delete")
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM compression_checks")["n"], 0)


class PrintAndTodoTest(OpsHubTestCase):
    """JOBS-18, JOBS-21, JOBS-28, JOBS-36, JOBS-40, JOBS-41, JOBS-42."""

    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N7")
        self.pid = self.make_project(asset_id=self.asset)
        self.exec("UPDATE projects SET intake_status='done' WHERE id=?", (self.pid,))

    def test_jobs_18_print_pages_have_back_to_project(self):
        c = self.login("tech")
        for url in (f"/projects/{self.pid}/label", f"/projects/{self.pid}/labor_codes"):
            self.assertIn("Back to Project", get(c, url), url)

    def test_jobs_40_labor_codes_open_first_done_grey_and_the_plane_named(self):
        self.exec("INSERT INTO project_sections (project_id, name, created_at, completed_at) VALUES (?, 'Aaa done', ?, ?)",
                  (self.pid, db.now_iso(), db.now_iso()))
        self.exec("INSERT INTO project_sections (project_id, name, created_at) VALUES (?, 'Zzz open', ?)",
                  (self.pid, db.now_iso()))
        body = get(self.login("tech"), f"/projects/{self.pid}/labor_codes")
        self.assertIn("N7", body)
        self.assertLess(body.index("Zzz open"), body.index("Aaa done"))
        self.assertIn("opacity:.55", body)

    def test_jobs_21_closeout_lists_found_items_waiting_on_the_owner(self):
        c = self.login("shop_admin")
        self.assertNotIn("found item", get(c, f"/projects/{self.pid}").split('id="closeoutModal"')[1])
        self.exec("INSERT INTO found_items (project_id, asset_id, description, est_total, status, created_at) "
                  "VALUES (?, ?, 'Cracked stack', 900, 'waiting', ?)", (self.pid, self.asset, db.now_iso()))
        modal = get(c, f"/projects/{self.pid}").split('id="closeoutModal"')[1]
        self.assertIn("1 found item still waiting on the owner", modal)
        self.exec("UPDATE found_items SET status='approved'")
        modal = get(c, f"/projects/{self.pid}").split('id="closeoutModal"')[1]
        self.assertIn("All found items answered", modal)

    def test_jobs_41_found_item_form_asks_before_a_zero_price(self):
        body = get(self.login("tech"), f"/projects/{self.pid}")
        self.assertIn("Send this with no price? The owner will see $0.00.", body)

    def test_jobs_28_my_tasks_card_is_called_my_to_dos(self):
        body = get(self.login("tech"), "/my-tasks")
        self.assertIn("My To-Dos", body)
        self.assertEqual(body.count("My Tasks <span"), 0)

    def test_jobs_36_todo_text_is_plain_with_a_start_job_link(self):
        self.exec("INSERT INTO plane_todos (asset_id, description, created_at) VALUES (?, 'Replace seat belt', ?)",
                  (self.asset, db.now_iso()))
        body = get(self.login("tech"), f"/assets/{self.asset}")
        self.assertIn(">Start job</a>", body)
        self.assertNotIn('title="Start a project from this to-do">Replace seat belt', body)

    def test_jobs_42_reopening_a_todo_asks_and_says_back_to_not_done(self):
        tid = self.exec("INSERT INTO plane_todos (asset_id, description, done, completed_at, created_at) "
                        "VALUES (?, 'Replace seat belt', 1, ?, ?)", (self.asset, db.now_iso(), db.now_iso()))
        c = self.login("tech")
        self.assertIn("Reopen this to-do?", get(c, f"/assets/{self.asset}"))
        r = c.post(f"/assets/{self.asset}/todo/{tid}/toggle", follow_redirects=True)
        self.assertIn("Back to not done yet.", r.get_data(as_text=True))
        tid2 = self.exec("INSERT INTO plane_todos (asset_id, description, confirm_requested_at, created_at) "
                         "VALUES (?, 'Other', ?, ?)", (self.asset, db.now_iso(), db.now_iso()))
        r = c.post(f"/assets/{self.asset}/todo/{tid2}/toggle", follow_redirects=True)
        self.assertIn("Back to not done yet.", r.get_data(as_text=True))

    def test_jobs_15_inspector_my_tasks_shows_what_they_can_sign_off(self):
        self.exec("INSERT INTO plane_todos (asset_id, description, confirm_requested_at, confirm_requested_by, created_at) "
                  "VALUES (?, 'Torque wheel nuts', ?, 'Tech', ?)", (self.asset, db.now_iso(), db.now_iso()))
        body = get(self.login("inspector"), "/my-tasks")
        self.assertIn("Torque wheel nuts", body)
        self.assertIn("Sign off", body)
        self.assertNotIn("Maintenance Due Fleet-Wide", body)

    def test_jobs_32_deleting_a_customer_says_it_is_for_good(self):
        self.exec("INSERT INTO customers (name, email, password_hash, active) VALUES ('Pat', 'pat@example.com', 'x', 1)")
        body = get(self.login("shop_admin"), "/customers")
        self.assertIn("Delete this customer account for good? To just stop them logging in, untick Active", body)
        self.assertIn('data-confirm-style="danger"', body)
