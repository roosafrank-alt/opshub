"""Preview build, Winds Aloft daily work: one small test per fix (SHOP-xx ids
are from the approved UI report)."""
from datetime import datetime, timedelta

from harness import OpsHubTestCase, seed_row
import db


class ShopPreviewFixesTest(OpsHubTestCase):
    def laborer(self, name="Joe Tech", code="LABOR-JOE", user_id=None):
        conn = db.get_db()
        lid = seed_row(conn, "laborers", name=name, code=code, rate=30, active=1, user_id=user_id)
        conn.commit()
        conn.close()
        return lid

    # ---- SHOP-02: General Shop timers on the dashboard
    def test_dashboard_lists_general_shop_timer_and_clock_out(self):
        joe = self.laborer()
        self.exec("INSERT INTO labor_sessions (laborer_id, project_id, started_at, rate, created_at) VALUES (?,?,?,?,?)",
                  (joe, None, db.now_iso(), 30, db.now_iso()))
        html = self.login("shop_admin").get("/shop").get_data(as_text=True)
        self.assertIn("Currently Clocked In", html)
        self.assertIn('id="open-sessions-table"', html)
        self.assertIn("General Shop", html)
        today = db.now_iso()[:10]
        self.exec("UPDATE labor_sessions SET ended_at = ?, hours = 1.5, cost = 45 WHERE laborer_id = ?",
                  (today + " 12:00:00", joe))
        html = self.client.get("/shop").get_data(as_text=True)
        self.assertIn("1.50 hrs", html)  # shows in Recent Activity

    # ---- SHOP-03: techs don't see activity-log costs
    def test_activity_log_cost_is_admin_only(self):
        pid = self.make_part()
        self.exec("INSERT INTO transactions (part_id, type, qty, performed_by, created_at) VALUES (?, 'in', 3, 'Frank', ?)",
                  (pid, db.now_iso()))
        admin = self.login("shop_admin").get("/activity").get_data(as_text=True)
        tech = self.login("tech").get("/activity").get_data(as_text=True)
        self.assertIn("sort=cost", admin)
        self.assertNotIn("sort=cost", tech)
        self.assertNotIn("$37.50", tech)
        self.assertIn("$37.50", admin)
        self.assertEqual(self.client.get("/activity?sort=cost").status_code, 200)

    # ---- SHOP-24: newest 200 plus Show more
    def test_activity_log_pages_in_200s(self):
        pid = self.make_part()
        conn = db.get_db()
        for i in range(205):
            conn.execute("INSERT INTO transactions (part_id, type, qty, performed_by, created_at) VALUES (?, 'in', 1, 'Frank', ?)",
                         (pid, "2026-01-01 00:00:%02d" % (i % 60)))
        conn.commit()
        conn.close()
        c = self.login("shop_admin")
        html = c.get("/activity").get_data(as_text=True)
        self.assertEqual(html.count('data-search="'), 200)
        self.assertIn("Show more", html)
        self.assertEqual(c.get("/activity?limit=400").get_data(as_text=True).count('data-search="'), 205)

    # ---- SHOP-04: retired parts are not low stock
    def test_retired_part_not_in_low_stock(self):
        a = self.make_part("Live Part", "P-A", qty=0, reorder=2)
        b = self.make_part("Gone Part", "P-B", qty=0, reorder=2)
        self.exec("UPDATE parts SET retired_at = ? WHERE id = ?", (db.now_iso(), b))
        html = self.login("shop_admin").get("/shop").get_data(as_text=True)
        self.assertIn("Live Part", html)
        self.assertNotIn("Gone Part", html)
        self.assertNotIn("Gone Part", self.client.get("/parts?low_stock=1").get_data(as_text=True))

    # ---- SHOP-05: Add Part / Export CSV are admin-only buttons
    def test_parts_page_buttons_by_role(self):
        self.make_part()
        admin = self.login("shop_admin").get("/parts").get_data(as_text=True)
        tech = self.login("tech").get("/parts").get_data(as_text=True)
        self.assertIn("Add Part", admin)
        self.assertIn("Export CSV", admin)
        self.assertNotIn("Add Part", tech)
        self.assertNotIn("Export CSV", tech)

    # ---- SHOP-07: unknown scan for non-admins
    def test_scan_page_add_new_part_only_for_admins(self):
        admin = self.login("shop_admin").get("/scan").get_data(as_text=True)
        tech = self.login("tech").get("/scan").get_data(as_text=True)
        self.assertIn("const CAN_ADD_PART = true", admin)
        self.assertIn("const CAN_ADD_PART = false", tech)

    # ---- SHOP-09: System Page button only for master admins
    def test_pi_alert_system_page_button_master_only(self):
        conn = db.get_db()
        seed_row(conn, "system_alerts", level="warning", message="Pi is warm", created_at=db.now_iso())
        conn.commit()
        conn.close()
        shop = self.login("shop_admin").get("/shop").get_data(as_text=True)
        master = self.login("master").get("/shop").get_data(as_text=True)
        self.assertIn("Acknowledge", shop)
        self.assertNotIn("System Page", shop)
        self.assertIn("System Page", master)

    # ---- SHOP-10: My Pay follows the linked badge
    def test_my_pay_uses_the_linked_badge(self):
        tech = self.users["tech"]
        lid = self.laborer("Michael R.", "LABOR-MIKE", user_id=tech["id"])
        today = db.now_iso()[:10]
        self.exec("""INSERT INTO labor_sessions (laborer_id, project_id, section, started_at, ended_at, hours, rate, cost, created_at)
                     VALUES (?,?,?,?,?,?,?,?,?)""", (lid, None, None, today + " 08:00:00", today + " 09:00:00", 1.0, 30, 30.0, db.now_iso()))
        html = self.login("tech").get("/shop/pay?period=all").get_data(as_text=True)
        self.assertIn("Michael R.", html)
        # nothing linked and no matching name: reworded empty state
        self.exec("UPDATE laborers SET user_id = NULL")
        html = self.client.get("/shop/pay?period=all").get_data(as_text=True)
        self.assertIn("ask an admin to link your badge", html)

    # ---- SHOP-42: All workers link
    def test_one_workers_pay_page_links_back_to_all(self):
        lid = self.laborer()
        html = self.login("shop_admin").get(f"/shop/pay?laborer_id={lid}&period=all").get_data(as_text=True)
        self.assertIn("All workers", html)

    # ---- SHOP-12: General Shop is not a project on Stats
    def test_stats_projects_worked_ignores_general_shop(self):
        lid = self.laborer()
        pid = self.make_project()
        today = db.now_iso()[:10]
        for proj in (pid, None):
            self.exec("""INSERT INTO labor_sessions (laborer_id, project_id, started_at, ended_at, hours, rate, cost, created_at)
                         VALUES (?,?,?,?,?,?,?,?)""", (lid, proj, today + " 08:00:00", today + " 09:00:00", 1.0, 30, 30.0, db.now_iso()))
        html = self.login("shop_admin").get("/shop/stats?period=all").get_data(as_text=True)
        self.assertRegex(html, r"Projects Worked[\s\S]{0,200}?>\s*1\s*<")

    # ---- SHOP-13: duplicate Task Template area
    def test_duplicate_template_area_says_already_there(self):
        c = self.login("shop_admin")
        c.post("/manage/task-templates", data={"quick_type": "Annual Inspection", "name": "Brakes"})
        r = c.post("/manage/task-templates", data={"quick_type": "Annual Inspection", "name": "Brakes"}, follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("is already on Annual", html)
        self.assertEqual(len(self.q("SELECT 1 FROM task_template_areas WHERE name = 'Brakes' AND quick_type = 'Annual Inspection'")), 1)

    # ---- SHOP-14: order actions return to the tab the person was on
    def test_order_actions_return_to_the_same_tab_and_view(self):
        pid = self.make_part()
        oid = self.exec("""INSERT INTO orders (part_id, description, qty_ordered, supplier, unit_cost, status, ordered_date, created_at)
                           VALUES (?, 'Filter', 2, 'Spruce', 5, 'pending', ?, ?)""", (pid, db.now_iso(), db.now_iso()))
        c = self.login("shop_admin")
        r = c.post(f"/orders/{oid}/receive", data={"status": "pending", "view": "part"})
        self.assertIn("status=pending", r.headers["Location"])
        self.assertIn("view=part", r.headers["Location"])
        oid2 = self.exec("""INSERT INTO orders (part_id, description, qty_ordered, supplier, unit_cost, status, ordered_date, created_at)
                            VALUES (?, 'Filter', 2, 'Spruce', 5, 'pending', ?, ?)""", (pid, db.now_iso(), db.now_iso()))
        r = c.post(f"/orders/{oid2}/cancel", data={"status": "pending", "view": "vendor"})
        self.assertIn("status=pending", r.headers["Location"])
        # the buttons carry the tab
        self.exec("""INSERT INTO orders (part_id, description, qty_ordered, supplier, unit_cost, status, ordered_date, created_at)
                     VALUES (?, 'Filter', 2, 'Spruce', 5, 'pending', ?, ?)""", (pid, db.now_iso(), db.now_iso()))
        html = c.get("/orders?status=pending&view=part").get_data(as_text=True)
        self.assertIn('name="status" value="pending"', html)
        self.assertIn("Edit</a>", html)

    def test_new_order_lands_on_pending(self):
        c = self.login("shop_admin")
        r = c.post("/orders/new", data={"description": "Gasket", "qty_ordered": "1", "unit_cost": "2", "supplier": "X"})
        self.assertIn("status=pending", r.headers["Location"])

    # ---- SHOP-16: undo invoiced / paid
    def test_undo_steps_back_one_state(self):
        pid = self.make_project()
        c = self.login("shop_admin")
        c.post(f"/shop/billing/{pid}/mark-invoiced")
        c.post(f"/shop/billing/{pid}/mark-paid", data={"paid_method": "Cash"})
        c.post(f"/shop/billing/{pid}/undo-payment")
        row = self.q1("SELECT payment_status, paid_at, paid_method FROM projects WHERE id = ?", (pid,))
        self.assertEqual(row["payment_status"], "invoiced")
        self.assertIsNone(row["paid_at"])
        c.post(f"/shop/billing/{pid}/undo-payment")
        self.assertEqual(self.q1("SELECT payment_status FROM projects WHERE id = ?", (pid,))["payment_status"], "not_invoiced")
        self.login("tech").post(f"/shop/billing/{pid}/mark-invoiced")
        self.assertEqual(self.q1("SELECT payment_status FROM projects WHERE id = ?", (pid,))["payment_status"], "not_invoiced")

    # ---- SHOP-17: Worker wording
    def test_worker_wording(self):
        c = self.login("shop_admin")
        html = c.get("/laborers/new").get_data(as_text=True)
        self.assertIn("Add Worker", html)
        self.assertNotIn("Laborer", html.split("<main")[-1] if "<main" in html else html.split("<h4")[1])
        r = c.post("/laborers/new", data={"name": "Sam", "rate": "20"}, follow_redirects=True)
        self.assertIn("Worker &#39;Sam&#39; added", r.get_data(as_text=True))

    # ---- SHOP-29: retired parts aren't orderable
    def test_retired_part_not_in_order_dropdowns(self):
        a = self.make_part("Live Part", "P-A")
        b = self.make_part("Gone Part", "P-B")
        self.exec("UPDATE parts SET retired_at = ? WHERE id = ?", (db.now_iso(), b))
        c = self.login("shop_admin")
        html = c.get("/orders/new").get_data(as_text=True)
        self.assertIn("Live Part", html)
        self.assertNotIn("Gone Part", html)
        self.assertNotIn("Gone Part", c.get("/orders?status=to_order").get_data(as_text=True))

    # ---- SHOP-28 / SHOP-45
    def test_new_order_wording_and_supplier_suggestions(self):
        self.make_part()  # supplier "Aircraft Spruce"
        html = self.login("shop_admin").get("/orders/new").get_data(as_text=True)
        self.assertIn("<h4", html)
        self.assertIn("Add Order</h4>", html)
        self.assertIn("Save Order", html)
        self.assertIn('<option value="Aircraft Spruce">', html)

    # ---- SHOP-27: no New Project on Orders
    def test_orders_header_has_no_new_project(self):
        html = self.login("shop_admin").get("/orders").get_data(as_text=True)
        self.assertNotIn("New Project", html)

    # ---- SHOP-30 / 32: part page
    def test_part_page_delete_only_when_never_on_a_job_and_order_more(self):
        pid = self.make_part()
        c = self.login("shop_admin")
        html = c.get(f"/parts/{pid}").get_data(as_text=True)
        self.assertIn("Delete Forever</button>", html)
        self.assertIn("Order more", html)
        self.assertNotIn("Order more", self.login("tech").get(f"/parts/{pid}").get_data(as_text=True))
        job = self.make_project()
        self.exec("INSERT INTO transactions (part_id, project_id, type, qty, performed_by, created_at) VALUES (?,?,'out',1,'F',?)",
                  (pid, job, db.now_iso()))
        html = self.login("shop_admin").get(f"/parts/{pid}").get_data(as_text=True)
        self.assertNotIn("Delete Forever</button>", html)
        self.assertIn("is on 1 job", html)

    # ---- SHOP-31
    def test_adjust_form_says_counted_by(self):
        pid = self.make_part()
        html = self.login("shop_admin").get(f"/parts/{pid}").get_data(as_text=True)
        self.assertIn("Counted by", html)
        self.assertNotIn("Scanning as", html)
        self.assertNotIn("making this adjustment", html)

    # ---- SHOP-33: calibration history
    def test_tools_page_lists_calibration_history(self):
        tid = self.exec("INSERT INTO shop_tools (name, calibration_interval_days, created_at, updated_at) VALUES ('Torque wrench', 365, ?, ?)",
                        (db.now_iso(), db.now_iso()))
        c = self.login("shop_admin")
        c.post(f"/shop/tools/{tid}/calibrate", data={"calibrated_at": "2026-03-04", "note": "Passed on bench"})
        html = c.get("/shop/tools").get_data(as_text=True)
        self.assertIn("History", html)
        self.assertIn("Passed on bench", html)

    # ---- SHOP-40: undo a scan
    def test_scan_can_be_undone_by_the_person_who_made_it(self):
        pid = self.make_part(qty=10)
        job = self.make_project()
        c = self.login("tech")
        r = self.scan("PART-001", "out", qty=2, project_id=job)
        tx = r.get_json()["transaction_id"]
        self.assertEqual(self.qty(pid), 8)
        r = c.post(f"/api/scan/undo/{tx}")
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.qty(pid), 10)
        self.assertIsNone(self.q1("SELECT 1 FROM transactions WHERE id = ?", (tx,)))

    def test_scan_undo_refused_for_someone_else_or_after_changes_or_too_late(self):
        pid = self.make_part(qty=10)
        job = self.make_project()
        c = self.login("tech")
        tx = self.scan("PART-001", "out", qty=1, project_id=job).get_json()["transaction_id"]
        # a different login can't undo it
        other = self.login("shop_admin")
        self.assertEqual(other.post(f"/api/scan/undo/{tx}").status_code, 403)
        self.assertEqual(self.qty(pid), 9)
        # something else touched the part since
        c = self.login("tech")
        tx1 = self.scan("PART-001", "out", qty=1, project_id=job).get_json()["transaction_id"]
        self.scan("PART-001", "out", qty=1, project_id=job)
        r = c.post(f"/api/scan/undo/{tx1}")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.qty(pid), 7)
        # too late
        c = self.login("tech")
        tx2 = self.scan("PART-001", "in", qty=1).get_json()["transaction_id"]
        old = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        self.exec("UPDATE transactions SET created_at = ? WHERE id = ?", (old, tx2))
        self.assertEqual(c.post(f"/api/scan/undo/{tx2}").status_code, 409)

    # ---- SHOP-48: confirm from the dashboard returns to the dashboard
    def test_section_confirm_from_dashboard_returns_to_dashboard(self):
        job = self.make_project()
        sid = self.exec("INSERT INTO project_sections (project_id, name, created_at, confirm_requested_at, confirm_requested_by) VALUES (?, 'Brakes', ?, ?, 'Tech')",
                        (job, db.now_iso(), db.now_iso()))
        r = self.login("inspector").post(f"/projects/{job}/sections/{sid}/confirm", data={"return_to": "dashboard"})
        self.assertTrue(r.headers["Location"].endswith("/shop"))

    # ---- SHOP-49: print badge stays on the sheet
    def test_print_badge_from_sheet_stays_on_sheet_for_admins(self):
        lid = self.laborer()
        r = self.login("shop_admin").post(f"/laborers/{lid}/print-label", data={"back": "badges"})
        self.assertIn("/labor/badges", r.headers["Location"])

    # ---- SHOP-41: account opened from Workers returns to Workers
    def test_account_saved_from_workers_returns_to_workers(self):
        u = self.users["tech"]
        r = self.login("master").post(f"/admin/users/{u['id']}/edit?back=workers",
                                      data={"name": "Tech", "shop_role": "tech", "active": "1"})
        self.assertIn("/laborers", r.headers["Location"])

    # ---- SHOP-15: stopping a timer can carry the General Shop note
    def test_stop_keeps_the_general_shop_note(self):
        lid = self.laborer()
        sid = self.exec("INSERT INTO labor_sessions (laborer_id, project_id, started_at, rate, created_at) VALUES (?,?,?,?,?)",
                        (lid, None, db.now_iso(), 30, db.now_iso()))
        r = self.login("tech").post(f"/api/labor/stop/{sid}", json={"note": "swept the floor"})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.q1("SELECT note FROM labor_sessions WHERE id = ?", (sid,))["note"], "swept the floor")

    # ---- SHOP-47
    def test_old_labor_template_is_gone(self):
        import os
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(__file__), "..", "templates", "labor.html")))
