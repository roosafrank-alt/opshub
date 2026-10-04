"""Preview phase 2, Winds Aloft shop area: SHOP-06, SHOP-08, SHOP-20, SHOP-26,
JOBS-37 and the app-wide sweeps (DESIGN-1/2/3/4/5/8, FLY-12, SEAM-15) as they
apply to the shop pages. Date text is compared through usdate() so it holds
whatever the app-wide format is."""
import os
import re

from harness import OpsHubTestCase, seed_row
import db

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def body(client, url):
    return client.get(url).get_data(as_text=True)


class ShopSix(OpsHubTestCase):
    # ---- SHOP-06
    def test_part_page_hides_edit_retire_delete_and_photo_x_for_a_tech(self):
        pid = self.make_part()
        self.exec("INSERT INTO photos (part_id, filename, created_at) VALUES (?, 'x.jpg', ?)", (pid, db.now_iso()))
        tech = body(self.login("tech"), f"/parts/{pid}")
        for gone in (f"/parts/{pid}/edit", f"/parts/{pid}/retire", f"/parts/{pid}/delete", "/photos/1/delete"):
            self.assertNotIn(gone, tech, gone)
        self.assertIn(f"/parts/{pid}/adjust", tech)         # a Tech may correct a count
        self.assertIn("Save Adjustment", tech)
        self.assertIn("/photos/1/set_cover", tech)
        admin = body(self.login("shop_admin"), f"/parts/{pid}")
        for here in (f"/parts/{pid}/edit", f"/parts/{pid}/retire", f"/parts/{pid}/delete", "/photos/1/delete"):
            self.assertIn(here, admin, here)

    def test_tech_count_correction_is_allowed_on_the_server_but_not_retire(self):
        pid = self.make_part(qty=10)
        c = self.login("tech")
        c.post(f"/parts/{pid}/adjust", data=dict(new_qty="7", performed_by="Tech"))
        self.assertEqual(self.qty(pid), 7)
        self.assertEqual(self.q1("SELECT type, qty, performed_by FROM transactions WHERE part_id = ?", (pid,))["performed_by"], "Tech")
        c.post(f"/parts/{pid}/retire")
        self.assertIsNone(self.q1("SELECT retired_at FROM parts WHERE id = ?", (pid,))["retired_at"])

    # ---- SHOP-08
    def test_create_links_only_for_admin_and_tech(self):
        for role, sees in (("shop_admin", True), ("tech", True), ("shop_student", False), ("inspector", False)):
            c = self.login(role)
            cal = body(c, "/calendar?view=month")
            scan = body(c, "/scan")
            dash = body(c, "/shop")
            self.assertEqual("/projects/new" in cal, sees, (role, "calendar"))
            self.assertEqual("/projects/new" in scan, sees, (role, "scan"))
            self.assertEqual("/projects/new" in dash, sees, (role, "dashboard"))
            self.assertEqual("Ask an admin to create one" in cal, not sees, (role, "calendar"))
            self.assertEqual("Ask an admin to create one" in scan, not sees, (role, "scan"))
            self.assertEqual("Ask an admin to create one" in dash, not sees, (role, "dashboard"))

    # ---- SHOP-20
    def test_scan_page_wording_replaces_labor_page(self):
        pid = self.make_project()
        conn = db.get_db()
        lid = seed_row(conn, "laborers", name="Joe", code="LABOR-JOE", rate=30, active=1)
        conn.commit()
        conn.close()
        c = self.login("shop_admin")
        label = body(c, f"/laborers/{lid}/label")
        self.assertIn("Scan this on the Scan page", label)
        for url in (f"/projects/{pid}/labor_codes",):
            page = body(c, url)
            self.assertIn(">Scan</a> page", page)
            self.assertNotIn(">Labor</a> page", page)
        self.assertNotIn(">Labor</a> page", body(c, f"/projects/{pid}"))
        self.assertNotIn(">Labor</a>", body(c, "/laborers").split("<h4", 1)[1].split("Workers", 1)[0])

    # ---- SHOP-26
    def test_calendar_phone_toolbar_and_list_default_script(self):
        c = self.login("shop_admin")
        html = body(c, "/calendar")
        self.assertIn("max-width: 767.98px", html)
        self.assertIn("searchParams.set('view', 'list')", html)
        # Year is hidden on phones; Add Project and Archived sit on their own line below.
        self.assertRegex(html, r'view=year[^>]*class="btn d-none d-md-inline-block')
        self.assertIn("Add Project", html)
        self.assertIn("Archived", html)
        self.assertEqual(c.get("/calendar?view=list").status_code, 200)

    # ---- JOBS-37
    def make_project_photo(self, pid):
        self.exec("INSERT INTO photos (project_id, filename, created_at) VALUES (?, 'p.jpg', ?)", (pid, db.now_iso()))
        return self.q1("SELECT id FROM photos WHERE project_id = ?", (pid,))["id"]

    def test_project_photo_buttons_follow_the_role(self):
        pid = self.make_project()
        ph = self.make_project_photo(pid)
        admin = body(self.login("shop_admin"), f"/projects/{pid}")
        tech = body(self.login("tech"), f"/projects/{pid}")
        insp = body(self.login("inspector"), f"/projects/{pid}")
        for page, add, star, x in ((admin, 1, 1, 1), (tech, 1, 1, 0), (insp, 0, 0, 0)):
            self.assertEqual(f"/projects/{pid}/photos" in page, bool(add))
            self.assertEqual(f"/photos/{ph}/set_cover" in page, bool(star))
            self.assertEqual(f"/photos/{ph}/delete" in page, bool(x))

    def test_deleting_a_photo_says_so_and_only_an_admin_can(self):
        pid = self.make_project()
        ph = self.make_project_photo(pid)
        self.login("tech").post(f"/photos/{ph}/delete")
        self.assertIsNotNone(self.q1("SELECT id FROM photos WHERE id = ?", (ph,)))
        r = self.login("shop_admin").post(f"/photos/{ph}/delete", follow_redirects=True)
        self.assertIn("Photo deleted", r.get_data(as_text=True))
        self.assertIsNone(self.q1("SELECT id FROM photos WHERE id = ?", (ph,)))

    def test_aircraft_photo_x_is_admin_only(self):
        a = self.make_asset("N77")
        self.exec("INSERT INTO photos (asset_id, filename, created_at) VALUES (?, 'a.jpg', ?)", (a, db.now_iso()))
        ph = self.q1("SELECT id FROM photos WHERE asset_id = ?", (a,))["id"]
        self.assertIn(f"/photos/{ph}/delete", body(self.login("shop_admin"), f"/assets/{a}"))
        self.assertNotIn(f"/photos/{ph}/delete", body(self.login("tech"), f"/assets/{a}"))


class ShopSweeps(OpsHubTestCase):
    def test_titles_are_page_then_program(self):
        pid = self.make_part()
        c = self.login("shop_admin")
        for url, want in (("/shop", "Dashboard · Winds Aloft"), ("/parts", "Parts · Winds Aloft"),
                          ("/calendar", "Calendar · Winds Aloft"), ("/shop/stats", "Stats · Winds Aloft"),
                          ("/orders/new", "Add Order · Winds Aloft"), ("/laborers", "Workers · Winds Aloft")):
            html = body(c, url)
            self.assertIn(f"<title>{want}</title>", html, url)
            self.assertNotIn("OpsHub</title>", html, url)

    def test_every_shop_template_title_has_the_program_name(self):
        bad = []
        mine = ("part", "project", "asset", "order", "calendar", "scan", "squawks", "laborer", "shop_", "tool",
                "task_templates", "manual", "logbook", "labels", "activity", "dashboard", "maintenance_", "trash",
                "general_shop_code", "tech_spot", "ad_status_print")
        for name in sorted(os.listdir(TEMPLATES)):
            if not name.endswith(".html") or not name.startswith(mine):
                continue
            with open(os.path.join(TEMPLATES, name)) as fh:
                src = fh.read()
            m = re.search(r"\{% block title %\}(.*?)\{% endblock %\}", src)
            if m and "Winds Aloft" not in m.group(1) and "OpsHub" in m.group(1):
                bad.append(name)
        self.assertEqual(bad, [])

    def test_create_buttons_say_add(self):
        c = self.login("shop_admin")
        self.make_part()
        for url, word in (("/projects", "Add Project"), ("/assets", "Add Aircraft"), ("/orders", "Add Order"),
                          ("/laborers", "Add Worker"), ("/parts", "Add Part"), ("/shop/tools", "Add Tool"),
                          ("/manuals", "Add Manual")):
            html = body(c, url)
            self.assertIn(word, html, url)
            self.assertNotRegex(html, r">\s*(<i[^>]*></i>\s*)?New (Project|Aircraft|Order|Worker|Part|Tool)\s*<", url)

    def test_main_save_buttons_use_the_dark_accent(self):
        c = self.login("shop_admin")
        for url in ("/parts/new", "/projects/new", "/assets/new", "/orders/new", "/laborers/new", "/shop/tools/new"):
            html = body(c, url)
            self.assertRegex(html, r'type="submit" class="btn btn-dark fw-bold"', url)
            self.assertNotIn("btn-warning fw-bold", html, url)

    def test_form_footers_are_save_then_cancel(self):
        c = self.login("shop_admin")
        for url in ("/parts/new", "/projects/new", "/assets/new", "/orders/new", "/laborers/new"):
            html = body(c, url)
            m = re.search(r'<div class="d-flex gap-2" data-change="DESIGN-8">\s*<button type="submit".*?</button>\s*<a .*?>Cancel</a>', html, re.S)
            self.assertTrue(m, url)

    def test_no_browser_confirm_boxes_are_left_in_shop_pages(self):
        # DESIGN-3: only the dialog fallbacks (used when Bootstrap itself did not load) may mention confirm(.
        for name in ("scan.html", "project_detail.html", "part_detail.html", "asset_detail.html", "orders.html"):
            with open(os.path.join(TEMPLATES, name)) as fh:
                src = fh.read()
            for m in re.finditer(r"(?<![\w.])(window\.)?confirm\(", src):
                line = src[:m.start()].count("\n") + 1
                ctx = src.splitlines()[line - 1]
                self.assertIn("OpsHubConfirm", ctx, f"{name}:{line}")
            self.assertNotIn("return confirm(", src)

    def test_delete_words(self):
        pid = self.make_part()
        a = self.make_asset("N5")
        c = self.login("shop_admin")
        part = body(c, f"/parts/{pid}")
        self.assertIn("Delete Forever</button>", part)
        self.assertIn('class="btn btn-danger btn-sm w-100"', part)
        asset = body(c, f"/assets/{a}")
        self.assertIn("Move to Trash", asset)
        r = c.post(f"/assets/{a}/trash", follow_redirects=True)
        self.assertIn("moved to the trash", r.get_data(as_text=True))
        trash = body(c, "/trash")
        self.assertIn("Delete Forever", trash)
        self.assertNotIn("btn-outline-danger\">Delete Forever", trash)
        r = c.post(f"/parts/{pid}/delete", follow_redirects=True)
        self.assertIn("Part deleted forever.", r.get_data(as_text=True))

    def test_tall_tables_stack_and_wide_ones_say_swipe(self):
        def src(name):
            with open(os.path.join(TEMPLATES, name)) as fh:
                return fh.read()
        for name in ("squawks.html", "manuals_list.html", "trash.html", "asset_oil.html", "project_detail.html",
                     "asset_detail.html"):
            self.assertIn("table-stack", src(name), name)
        for name in ("asset_compression.html", "projects_parts_used.html"):
            self.assertIn("Swipe sideways", src(name), name)

    def test_calendar_dates_use_the_short_app_format(self):
        from app import usdate_short
        self.exec("INSERT INTO projects (name, code, status, scheduled_date, created_at) VALUES ('Sched', 'P-1', 'active', ?, ?)",
                  (db.now_iso()[:10], db.now_iso()))
        html = body(self.login("shop_admin"), "/calendar?view=list")
        self.assertIn(usdate_short(db.now_iso()[:10]), html)
