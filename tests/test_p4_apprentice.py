"""SHOP-08: what an Apprentice sees in Winds Aloft - only the tasks assigned
to them, a limited aircraft page, a calendar the admin can limit, stock-in
at the Scan page, and nothing outside Winds Aloft when it is their only role."""
from harness import OpsHubTestCase
import db


class ApprenticeBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.me = self.users["shop_student"]["id"]
        self.asset = self.make_asset("N111AA")
        self.other_asset = self.make_asset("N222BB")
        self.mine = self.make_project("Mine - N111AA", asset_id=self.asset)
        self.theirs = self.make_project("Secret job - N222BB", asset_id=self.other_asset)
        self.exec("UPDATE projects SET description='PRIVATE JOB NOTE', scheduled_date=?, intake_status='done' WHERE id IN (?,?)",
                  (db.now_iso()[:10], self.mine, self.theirs))
        self.sec_mine = self.exec("INSERT INTO project_sections (project_id,name,created_at,assigned_to,notes) VALUES (?,?,?,?,?)",
                                  (self.mine, "Brakes", db.now_iso(), self.me, "INTERNAL NOTE"))
        self.sec_other_task = self.exec("INSERT INTO project_sections (project_id,name,created_at,notes) VALUES (?,?,?,?)",
                                        (self.mine, "Engine", db.now_iso(), "ENGINE NOTE"))
        self.sec_theirs = self.exec("INSERT INTO project_sections (project_id,name,created_at) VALUES (?,?,?)",
                                    (self.theirs, "Avionics", db.now_iso()))
        self.part = self.make_part("Brake Pad", "BP-1", qty=10, unit_cost=77.77)
        self.part2 = self.make_part("Spark Plug", "SP-1", qty=10, unit_cost=33.33)
        for part, proj, sec, q in ((self.part, self.mine, "Brakes", 2), (self.part2, self.mine, "Engine", 4)):
            self.exec("INSERT INTO transactions (part_id,project_id,type,qty,note,performed_by,section,source,created_at) "
                      "VALUES (?,?, 'out',?, 'x','Frank',?, 'assigned', ?)", (part, proj, q, sec, db.now_iso()))


class AssignTaskTests(ApprenticeBase):
    def test_admin_assigns_and_it_shows(self):
        c = self.login("shop_admin")
        r = c.post(f"/projects/{self.mine}/sections/{self.sec_other_task}/assign",
                   data={"assigned_to": self.me}, follow_redirects=True)
        self.assertIn("is now assigned to", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT assigned_to FROM project_sections WHERE id=?", (self.sec_other_task,))["assigned_to"], self.me)
        body = c.get(f"/projects/{self.mine}").get_data(as_text=True)
        self.assertIn("Assigned to", body)
        self.assertIn('name="assigned_to"', body)

    def test_tech_can_assign_and_clear(self):
        c = self.login("tech")
        c.post(f"/projects/{self.mine}/sections/{self.sec_mine}/assign", data={"assigned_to": ""})
        self.assertIsNone(self.q1("SELECT assigned_to FROM project_sections WHERE id=?", (self.sec_mine,))["assigned_to"])

    def test_apprentice_and_inspector_cannot_assign(self):
        for role in ("shop_student", "inspector"):
            c = self.login(role)
            c.post(f"/projects/{self.mine}/sections/{self.sec_other_task}/assign", data={"assigned_to": self.me})
            self.assertIsNone(self.q1("SELECT assigned_to FROM project_sections WHERE id=?", (self.sec_other_task,))["assigned_to"])

    def test_cannot_assign_to_someone_without_shop_role(self):
        c = self.login("shop_admin")
        c.post(f"/projects/{self.mine}/sections/{self.sec_other_task}/assign",
               data={"assigned_to": self.users["flight_student"]["id"]})
        self.assertIsNone(self.q1("SELECT assigned_to FROM project_sections WHERE id=?", (self.sec_other_task,))["assigned_to"])

    def test_migration_columns_exist(self):
        cols = [r["name"] for r in self.q("PRAGMA table_info(project_sections)")]
        self.assertIn("assigned_to", cols)
        self.assertIn("apprentice_calendar", [r["name"] for r in self.q("PRAGMA table_info(users)")])
        self.assertEqual(self.q1("SELECT apprentice_calendar a FROM users WHERE id=?", (self.me,))["a"], "limited")


class ProjectPageTests(ApprenticeBase):
    def test_limited_project_page(self):
        body = self.login("shop_student").get(f"/projects/{self.mine}").get_data(as_text=True)
        self.assertIn("Mine - N111AA", body)
        self.assertIn("Brakes", body)
        self.assertIn("Brake Pad", body)
        for hidden in ("Engine", "Spark Plug", "INTERNAL NOTE", "ENGINE NOTE", "PRIVATE JOB NOTE", "77.77", "Invoice", "Trash"):
            self.assertNotIn(hidden, body)

    def test_project_without_my_task_is_refused(self):
        c = self.login("shop_student")
        r = c.get(f"/projects/{self.theirs}")
        self.assertEqual(r.status_code, 302)
        self.assertIn("Ask an admin", c.get(f"/projects/{self.theirs}", follow_redirects=True).get_data(as_text=True))

    def test_another_apprentice_cannot_open_it(self):
        uid = self.exec("INSERT INTO users (name,username,password_hash,active,shop_role,created_at) VALUES ('Other App','otherapp','x',1,'apprentice',?)", (db.now_iso(),))
        self.users["other"] = self.q1("SELECT * FROM users WHERE id=?", (uid,))
        c = self.login("other")
        self.assertEqual(c.get(f"/projects/{self.mine}").status_code, 302)

    def test_admin_and_tech_unchanged(self):
        for role in ("shop_admin", "tech"):
            body = self.login(role).get(f"/projects/{self.mine}").get_data(as_text=True)
            self.assertIn("Spark Plug", body)
            self.assertIn("Engine", body)

    def test_apprentice_cannot_use_other_job_endpoints(self):
        c = self.login("shop_student")
        self.assertEqual(c.get(f"/projects/{self.theirs}/labor_codes").status_code, 302)
        self.assertEqual(c.get(f"/projects/{self.mine}/checklist").status_code, 302)
        self.assertEqual(c.get(f"/projects/{self.theirs}/intake").status_code, 302)
        self.assertEqual(c.get(f"/api/sections/{self.theirs}").get_json(), [])
        proj = self.q1("SELECT code FROM projects WHERE id=?", (self.theirs,))["code"]
        self.assertFalse(c.get(f"/api/project_lookup/{proj}").get_json()["found"])
        mine_code = self.q1("SELECT code FROM projects WHERE id=?", (self.mine,))["code"]
        self.assertTrue(c.get(f"/api/project_lookup/{mine_code}").get_json()["found"])
        r = c.post(f"/projects/{self.theirs}/part-request", data={"description": "x"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM order_wishlist")["c"], 0)

    def test_lists_are_closed(self):
        c = self.login("shop_student")
        for url in ("/projects", "/projects/parts-used", "/parts", "/orders", "/customers",
                    "/laborers", "/squawks", "/activity", "/assets"):
            r = c.get(url)
            self.assertEqual(r.status_code, 302, url)


class MyTasksTests(ApprenticeBase):
    def test_my_tasks_grouped(self):
        body = self.login("shop_student").get("/my-tasks").get_data(as_text=True)
        self.assertIn("Mine - N111AA", body)
        self.assertIn("Brakes", body)
        self.assertIn("Brake Pad", body)
        self.assertNotIn("Spark Plug", body)
        self.assertNotIn("Secret job", body)
        self.assertNotIn("Fleet", body)
        self.assertIn(f"/projects/{self.mine}", body)

    def test_empty(self):
        self.exec("UPDATE project_sections SET assigned_to=NULL")
        body = self.login("shop_student").get("/my-tasks").get_data(as_text=True)
        self.assertIn("Nothing is assigned to you", body)

    def test_dashboard_lists_only_mine(self):
        body = self.login("shop_student").get("/shop").get_data(as_text=True)
        self.assertIn("Mine - N111AA", body)
        self.assertNotIn("Secret job", body)


class AircraftTests(ApprenticeBase):
    def setUp(self):
        super().setUp()
        self.exec("UPDATE assets SET owner='Owner Person Name' WHERE id=?", (self.asset,))
        self.exec("INSERT INTO maintenance_items (asset_id,name,type,interval_days,active) VALUES (?,?,?,?,1)",
                  (self.asset, "Annual inspection", "calendar", 365))

    def test_trimmed_page_for_my_aircraft(self):
        body = self.login("shop_student").get(f"/assets/{self.asset}").get_data(as_text=True)
        self.assertIn("N111AA", body)
        self.assertIn("Annual inspection", body)
        self.assertNotIn("Owner Person Name", body)

    def test_other_aircraft_refused(self):
        self.assertEqual(self.login("shop_student").get(f"/assets/{self.other_asset}").status_code, 302)

    def test_list_refused_and_header(self):
        c = self.login("shop_student")
        self.assertEqual(c.get("/assets").status_code, 302)
        body = c.get("/my-tasks").get_data(as_text=True)
        for gone in (">Aircraft<", ">Parts<", ">Projects<", ">Orders<", ">Squawks<", ">Activity<", ">Manage<"):
            self.assertNotIn(gone, body)
        for there in (">Scan<", ">My Tasks<", ">Calendar<"):
            self.assertIn(there, body)

    def test_admin_aircraft_page_unchanged(self):
        self.assertEqual(self.login("shop_admin").get(f"/assets/{self.other_asset}").status_code, 200)


class CalendarTests(ApprenticeBase):
    def test_limited_default(self):
        body = self.login("shop_student").get("/calendar?view=list").get_data(as_text=True)
        self.assertIn("Mine - N111AA", body)
        self.assertNotIn("Secret job", body)

    def test_basic_shows_all_but_no_link_in(self):
        self.exec("UPDATE users SET apprentice_calendar='basic' WHERE id=?", (self.me,))
        body = self.login("shop_student").get("/calendar?view=list").get_data(as_text=True)
        self.assertIn("Mine - N111AA", body)
        self.assertIn("Secret job", body)
        self.assertNotIn(f"/projects/{self.theirs}", body)
        self.assertNotIn("PRIVATE JOB NOTE", body)
        self.assertNotIn("INTERNAL NOTE", body)

    def test_admin_sees_everything(self):
        body = self.login("shop_admin").get("/calendar?view=list").get_data(as_text=True)
        self.assertIn("Secret job", body)
        self.assertIn(f"/projects/{self.theirs}", body)

    def test_admin_form_setting_saved_and_shown(self):
        c = self.login("master")
        page = c.get(f"/admin/users/{self.me}/edit").get_data(as_text=True)
        self.assertIn("Calendar for this apprentice", page)
        c.post(f"/admin/users/{self.me}/edit", data={"name": "Shop Student", "shop_roles": ["apprentice"],
                                                      "active": "on", "apprentice_calendar": "basic"})
        self.assertEqual(self.q1("SELECT apprentice_calendar a FROM users WHERE id=?", (self.me,))["a"], "basic")

    def test_bad_value_keeps_saved(self):
        c = self.login("master")
        c.post(f"/admin/users/{self.me}/edit", data={"name": "Shop Student", "shop_roles": ["apprentice"],
                                                      "active": "on", "apprentice_calendar": "everything"})
        self.assertEqual(self.q1("SELECT apprentice_calendar a FROM users WHERE id=?", (self.me,))["a"], "limited")


class ScanStockInTests(ApprenticeBase):
    def test_stock_in_works_and_is_logged(self):
        c = self.login("shop_student")
        r = c.post("/api/scan", json={"barcode": "BP-1", "action": "in", "qty": 5, "performed_by": "Shop Student"})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.qty(self.part), 10 + 5)
        tx = self.q1("SELECT * FROM transactions WHERE part_id=? AND type='in'", (self.part,))
        self.assertEqual(tx["performed_by"], "Shop Student")

    def test_response_has_no_prices_or_vendor(self):
        r = self.login("shop_student").post("/api/scan", json={"barcode": "BP-1", "action": "in", "qty": 1, "performed_by": "S"}).get_json()
        self.assertEqual(r["name"], "Brake Pad")
        for k in ("unit_cost", "sell_price", "supplier", "reorder_point", "location"):
            self.assertNotIn(k, r)
        look = self.login("shop_student").get("/api/lookup/BP-1").get_json()
        for k in ("unit_cost", "sell_price", "supplier", "reorder_point"):
            self.assertNotIn(k, look)

    def test_stock_out_onto_my_task_only(self):
        c = self.login("shop_student")
        ok = c.post("/api/scan", json={"barcode": "BP-1", "action": "out", "qty": 1, "project_id": self.mine,
                                       "section": "Brakes", "performed_by": "S"})
        self.assertTrue(ok.get_json()["ok"])
        bad = c.post("/api/scan", json={"barcode": "BP-1", "action": "out", "qty": 1, "project_id": self.theirs, "performed_by": "S"})
        self.assertEqual(bad.status_code, 403)

    def test_unknown_barcode_then_add_part_without_prices(self):
        c = self.login("shop_student")
        r = c.post("/api/scan", json={"barcode": "NEW-9", "action": "in", "qty": 1, "performed_by": "S"})
        self.assertEqual(r.status_code, 404)
        form = c.get("/parts/new?barcode=NEW-9").get_data(as_text=True)
        for gone in ("unit_cost", "sell_price", "reorder_point", "supplier", "expiration_date"):
            self.assertNotIn(f'name="{gone}"', form)
        r = c.post("/parts/new", data={"name": "New Washer", "barcode": "NEW-9", "category": "Hardware", "location": "A1",
                                       "unit": "ea", "qty_on_hand": "3", "unit_cost": "99", "sell_price": "150",
                                       "reorder_point": "7", "supplier": "ACME", "description": "d", "part_number": "PN"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/scan", r.headers["Location"])
        row = self.q1("SELECT * FROM parts WHERE barcode='NEW-9'")
        self.assertEqual(row["unit_cost"], 0)
        self.assertEqual(row["sell_price"], 0)
        self.assertEqual(row["reorder_point"], 0)
        self.assertFalse(row["supplier"])
        self.assertEqual(row["qty_on_hand"], 3)
        self.assertEqual(row["location"], "A1")
        ok = c.post("/api/scan", json={"barcode": "NEW-9", "action": "in", "qty": 2, "performed_by": "S"})
        self.assertTrue(ok.get_json()["ok"])
        self.assertEqual(self.q1("SELECT qty_on_hand q FROM parts WHERE barcode='NEW-9'")["q"], 5)

    def test_still_cannot_edit_retire_delete_or_see_part_pages(self):
        c = self.login("shop_student")
        for url in (f"/parts/{self.part}", f"/parts/{self.part}/edit", "/parts", "/parts/export.csv"):
            self.assertEqual(c.get(url).status_code, 302, url)
        for url in (f"/parts/{self.part}/retire", f"/parts/{self.part}/delete", f"/parts/{self.part}/adjust"):
            self.assertIn(c.post(url, data={}).status_code, (302, 404, 405), url)
        self.assertIsNotNone(self.q1("SELECT id FROM parts WHERE id=?", (self.part,)))
        self.assertIsNone(self.q1("SELECT retired_at r FROM parts WHERE id=?", (self.part,))["r"])

    def test_tech_still_cannot_add_part(self):
        self.assertEqual(self.login("tech").get("/parts/new").status_code, 302)

    def test_scan_page_projects_only_mine(self):
        body = self.login("shop_student").get("/scan").get_data(as_text=True)
        self.assertIn("Mine - N111AA", body)
        self.assertNotIn("Secret job", body)


class OnlyWindsAloftTests(ApprenticeBase):
    def test_apprentice_only_has_one_program(self):
        c = self.login("shop_student")
        c.get("/shop")
        for url in ("/flight/dashboard", "/flight/students", "/academy", "/customers", "/admin", "/admin/users", "/portal/"):
            r = c.get(url)
            self.assertIn(r.status_code, (302, 403, 404), url)
            self.assertNotIn("/flight/dashboard", r.headers.get("Location", ""), url)

    def test_programs_list(self):
        import auth
        self.login("shop_student")
        with self.app.test_request_context("/"):
            from flask import session
            session.update(user_id=self.me, shop_role="apprentice", is_master_admin=False, flight_role=None,
                           academy_access=False)
            self.assertEqual([p["key"] for p in auth.session_programs()], ["shop"])

    def test_with_flight_role_keeps_flight(self):
        self.exec("UPDATE users SET flight_role='student' WHERE id=?", (self.me,))
        c = self.login("shop_student")
        r = c.get("/flight/")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/shop", r.headers["Location"])


class GeneralPartsTests(ApprenticeBase):
    """Frank: an apprentice also sees the parts put on the job as a whole (General), name and quantity only."""
    def setUp(self):
        super().setUp()
        self.part3 = self.make_part("Safety Wire", "SW-1", qty=10, unit_cost=12.34)
        self.exec("INSERT INTO transactions (part_id,project_id,type,qty,note,performed_by,section,source,created_at) "
                  "VALUES (?,?, 'out', 3, 'x','Frank',NULL, 'assigned', ?)", (self.part3, self.mine, db.now_iso()))

    def test_project_page_shows_general_parts_without_prices(self):
        body = self.login("shop_student").get(f"/projects/{self.mine}").get_data(as_text=True)
        self.assertIn("General parts on this job", body)
        self.assertIn("Safety Wire", body)
        self.assertNotIn("12.34", body)
        self.assertNotIn("Spark Plug", body)   # still only their own task's parts besides General

    def test_my_tasks_shows_general_parts_for_their_jobs_only(self):
        body = self.login("shop_student").get("/my-tasks").get_data(as_text=True)
        self.assertIn("General parts on this job", body)
        self.assertIn("Safety Wire", body)
        self.exec("INSERT INTO transactions (part_id,project_id,type,qty,note,performed_by,section,source,created_at) "
                  "VALUES (?,?, 'out', 1, 'x','Frank',NULL, 'assigned', ?)", (self.part2, self.theirs, db.now_iso()))
        body = self.login("shop_student").get("/my-tasks").get_data(as_text=True)
        self.assertNotIn("Spark Plug", body)   # a job with no task for them is never listed
