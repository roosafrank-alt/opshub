"""End-to-end logic tests for Orders (new, edit, wishlist, export CSV) and
Labor tracking (scan-to-clock-in/out, manual stop, pay and billing totals).

Every test drives the app through its real HTTP routes and then checks the
database. Tests marked @open_finding("qa-...") describe the CORRECT behavior
for a problem reported on the Idea Queue page's QA findings but not fixed
yet, so they're expected to fail until the fix is made.
"""
import csv
import io
from datetime import datetime, timedelta

from harness import OpsHubTestCase, seed_row, open_finding
import db


def ago(hours):
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


# ===========================================================================
# Orders
# ===========================================================================
class OrderFormTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.part = self.make_part(qty=2)
        self.project = self.make_project()
        self.login("shop_admin")

    def new_order(self, **form):
        base = dict(description="Spark plug", qty_ordered="4", unit_cost="20", supplier="Aircraft Spruce")
        base.update(form)
        return self.client.post("/orders/new", data=base)

    def orders(self):
        return self.q("SELECT * FROM orders ORDER BY id")

    # --- happy paths -----------------------------------------------------
    def test_new_order_is_saved_as_pending(self):
        r = self.new_order(project_id=str(self.project), expected_date="2026-10-01", note="rush")
        self.assertEqual(r.status_code, 302)
        o = self.orders()[-1]
        self.assertEqual((o["description"], o["qty_ordered"], o["unit_cost"], o["status"], o["project_id"]),
                         ("Spark plug", 4, 20, "pending", self.project))

    def test_picking_a_part_fills_in_the_description(self):
        self.new_order(description="", part_id=str(self.part))
        self.assertEqual(self.orders()[-1]["description"], "Oil Filter")

    def test_edit_updates_a_pending_order(self):
        self.new_order()
        oid = self.orders()[-1]["id"]
        r = self.client.post(f"/orders/{oid}/edit", data=dict(description="Spark plug", qty_ordered="8",
                                                               unit_cost="21.5", supplier="Spruce"))
        self.assertEqual(r.status_code, 302)
        o = self.q1("SELECT * FROM orders WHERE id=?", (oid,))
        self.assertEqual((o["qty_ordered"], o["unit_cost"], o["supplier"]), (8, 21.5, "Spruce"))

    def test_edit_missing_order_does_not_crash(self):
        self.assertIn(self.client.get("/orders/9999/edit").status_code, (302, 404))
        self.assertIn(self.client.post("/orders/9999/edit", data=dict(description="x")).status_code, (302, 404))

    def test_only_shop_admins_manage_orders(self):
        for role in ("tech", "shop_student", "inspector", "cfi", "flight_student", "no_roles"):
            with self.subTest(role=role):
                c = self.login(role)
                before = len(self.orders())
                self.assertNotEqual(c.get("/orders").status_code, 200)
                c.post("/orders/new", data=dict(description="Sneaky", qty_ordered="1"))
                c.post("/orders/wishlist/new", data=dict(description="Sneaky"))
                self.assertEqual(len(self.orders()), before)
                self.assertIsNone(self.q1("SELECT id FROM order_wishlist WHERE description='Sneaky'"))

    # --- rejections ------------------------------------------------------
    def test_blank_new_order_shows_the_form_again_not_a_crash(self):
        r = self.new_order(description="", part_id="")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.orders(), [])

    def test_non_number_quantity_shows_the_form_again_not_a_crash(self):
        r = self.new_order(qty_ordered="four")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.orders(), [])

    def test_edit_with_non_number_quantity_is_rejected(self):
        self.new_order()
        oid = self.orders()[-1]["id"]
        r = self.client.post(f"/orders/{oid}/edit", data=dict(description="Spark plug", qty_ordered="four"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.q1("SELECT qty_ordered FROM orders WHERE id=?", (oid,))["qty_ordered"], 4)

    def test_new_order_rejects_zero_negative_nan_quantities_and_costs(self):
        bad = [dict(qty_ordered=q) for q in ("0", "-3", "nan", "inf")] + \
              [dict(unit_cost=c) for c in ("-5", "nan", "inf")]
        for fields in bad:
            with self.subTest(**fields):
                self.new_order(**fields)
        self.assertEqual(self.orders(), [])

    def test_edit_rejects_negative_or_nan_quantity(self):
        self.new_order()
        oid = self.orders()[-1]["id"]
        for q in ("-3", "nan", "0"):
            with self.subTest(qty=q):
                self.client.post(f"/orders/{oid}/edit", data=dict(description="Spark plug", qty_ordered=q))
                self.assertEqual(self.q1("SELECT qty_ordered FROM orders WHERE id=?", (oid,))["qty_ordered"], 4)

    def test_receiving_can_never_remove_stock(self):
        # A negative order quantity typed by mistake turns "Receive" into a
        # silent stock removal.
        self.new_order(description="", part_id=str(self.part), qty_ordered="-5")
        o = self.q1("SELECT id FROM orders ORDER BY id DESC LIMIT 1")
        if o:
            self.client.post(f"/orders/{o['id']}/receive")
        self.assertGreaterEqual(self.qty(self.part), 2)

    def test_received_or_cancelled_orders_cannot_be_edited(self):
        self.new_order(description="", part_id=str(self.part), qty_ordered="6")
        received = self.orders()[-1]["id"]
        self.client.post(f"/orders/{received}/receive")
        self.assertEqual(self.qty(self.part), 8)
        self.new_order()
        cancelled = self.orders()[-1]["id"]
        self.client.post(f"/orders/{cancelled}/cancel")
        for oid in (received, cancelled):
            with self.subTest(order=oid):
                before = dict(self.q1("SELECT * FROM orders WHERE id=?", (oid,)))
                self.client.post(f"/orders/{oid}/edit", data=dict(description="Changed", qty_ordered="60",
                                                                   unit_cost="1"))
                self.assertEqual(dict(self.q1("SELECT * FROM orders WHERE id=?", (oid,))), before)
        # The received order and the shelf still agree.
        self.assertEqual(self.qty(self.part), 8)

    def test_deleting_a_part_that_is_on_an_order_does_not_crash(self):
        # The database refuses to delete a part an order, a To-Order list
        # entry or a photo still points at, and the Delete button showed an
        # error page instead of a message. Either blocking with a message
        # or unlinking is fine, as long as the order can still be received.
        spare = self.make_part(name="Gasket", barcode="PART-002", qty=0)
        self.new_order(description="", part_id=str(spare), qty_ordered="3")
        oid = self.orders()[-1]["id"]
        r = self.client.post(f"/parts/{spare}/delete")
        self.assertNotEqual(r.status_code, 500)
        self.client.post(f"/orders/{oid}/receive")
        o = self.q1("SELECT * FROM orders WHERE id=?", (oid,))
        part = self.q1("SELECT * FROM parts WHERE id=?", (o["part_id"],))
        self.assertIsNotNone(part)
        self.assertEqual(part["qty_on_hand"], 3)

    def test_deleting_a_part_on_the_to_order_list_or_with_a_photo_does_not_crash(self):
        for i, table in enumerate(("order_wishlist", "photos")):
            with self.subTest(linked_from=table):
                pid = self.make_part(name=f"Thing {i}", barcode=f"PART-10{i}", qty=0)
                conn = db.get_db()
                if table == "order_wishlist":
                    seed_row(conn, table, description="Thing", part_id=pid)
                else:
                    seed_row(conn, table, part_id=pid, filename="x.jpg")
                conn.commit()
                conn.close()
                self.assertNotEqual(self.client.post(f"/parts/{pid}/delete").status_code, 500)


class WishlistTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.part = self.make_part()
        self.login("shop_admin")

    def test_add_with_bad_urgency_defaults_to_no_rush(self):
        self.client.post("/orders/wishlist/new", data=dict(description="Safety wire", urgency="yesterday"))
        w = self.q1("SELECT * FROM order_wishlist")
        self.assertEqual((w["description"], w["urgency"], w["status"], w["requested_by"]),
                         ("Safety wire", "no_rush", "open", "Shop Admin"))

    def test_blank_entry_rejected_and_part_name_used(self):
        self.client.post("/orders/wishlist/new", data=dict(description=""))
        self.assertIsNone(self.q1("SELECT id FROM order_wishlist"))
        self.client.post("/orders/wishlist/new", data=dict(description="", part_id=str(self.part)))
        self.assertEqual(self.q1("SELECT description FROM order_wishlist")["description"], "Oil Filter")

    def test_placing_an_order_from_the_list_closes_the_entry_once(self):
        self.client.post("/orders/wishlist/new", data=dict(description="Tire", urgency="rush"))
        wid = self.q1("SELECT id FROM order_wishlist")["id"]
        page = self.client.get(f"/orders/new?wishlist_id={wid}").get_data(as_text=True)
        self.assertIn("Tire", page)
        self.client.post("/orders/new", data=dict(description="Tire", qty_ordered="2", wishlist_id=str(wid)))
        self.assertEqual(self.q1("SELECT status FROM order_wishlist WHERE id=?", (wid,))["status"], "ordered")
        # Dismissing it afterwards must not flip it back.
        self.client.post(f"/orders/wishlist/{wid}/dismiss")
        self.assertEqual(self.q1("SELECT status FROM order_wishlist WHERE id=?", (wid,))["status"], "ordered")

    def test_dismiss(self):
        self.client.post("/orders/wishlist/new", data=dict(description="Rags"))
        wid = self.q1("SELECT id FROM order_wishlist")["id"]
        self.client.post(f"/orders/wishlist/{wid}/dismiss")
        self.assertEqual(self.q1("SELECT status FROM order_wishlist WHERE id=?", (wid,))["status"], "dismissed")
        self.assertNotEqual(self.client.post("/orders/wishlist/9999/dismiss").status_code, 500)


class OrderListAndExportTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        conn = db.get_db()
        seed_row(conn, "orders", description="Plug", qty_ordered=4, unit_cost=20, supplier="Spruce",
                 status="pending", ordered_date="2026-09-20 10:00:00")
        seed_row(conn, "orders", description="Filter", qty_ordered=2, unit_cost=12.5, supplier="Spruce",
                 status="pending", ordered_date="2026-09-21 10:00:00")
        seed_row(conn, "orders", description="Tire", qty_ordered=1, unit_cost=90, supplier="Desser",
                 status="pending", ordered_date="2026-09-21 10:00:00")
        seed_row(conn, "orders", description="Old", qty_ordered=9, unit_cost=1, supplier="Spruce",
                 status="received", ordered_date="2026-09-01 10:00:00")
        conn.commit()
        conn.close()
        self.login("shop_admin")

    def rows(self, url):
        return list(csv.DictReader(io.StringIO(self.client.get(url).get_data(as_text=True))))

    def test_export_defaults_to_pending_and_totals_match(self):
        rows = self.rows("/orders/export")
        self.assertEqual(sorted(r["Item"] for r in rows), ["Filter", "Plug", "Tire"])
        self.assertAlmostEqual(sum(float(r["Est. Total"]) for r in rows), 4 * 20 + 2 * 12.5 + 90)
        for r in rows:
            self.assertAlmostEqual(float(r["Est. Total"]), float(r["Qty"]) * float(r["Unit Cost"]))

    def test_export_filters_by_vendor_and_status(self):
        self.assertEqual(sorted(r["Item"] for r in self.rows("/orders/export?vendor=Spruce")), ["Filter", "Plug"])
        self.assertEqual(sorted(r["Item"] for r in self.rows("/orders/export?vendor=Spruce&status=all")),
                         ["Filter", "Old", "Plug"])
        self.assertEqual(self.rows("/orders/export?vendor=Nobody"), [])

    def test_orders_page_vendor_totals_match_their_lines(self):
        for view in ("vendor", "part", "bogus"):
            with self.subTest(view=view):
                r = self.client.get(f"/orders?view={view}&status=pending")
                self.assertEqual(r.status_code, 200)
        html = self.client.get("/orders?view=vendor").get_data(as_text=True)
        self.assertIn("105.00", html)  # Spruce: 80 + 25
        self.assertIn("90.00", html)   # Desser


# ===========================================================================
# Labor tracking
# ===========================================================================
class LaborFlowTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        conn = db.get_db()
        self.laborer = seed_row(conn, "laborers", name="Tech", code="LABOR-AAAA0001", rate=40, active=1)
        self.other = seed_row(conn, "laborers", name="Joe", code="LABOR-BBBB0002", rate=25, active=1)
        conn.commit()
        conn.close()
        self.project = self.make_project()
        self.login("tech")

    def labor(self, code="LABOR-AAAA0001", **body):
        body["code"] = code
        return self.client.post("/api/labor/scan", json=body)

    def sessions(self, laborer=None):
        if laborer:
            return self.q("SELECT * FROM labor_sessions WHERE laborer_id=? ORDER BY id", (laborer,))
        return self.q("SELECT * FROM labor_sessions ORDER BY id")

    def backdate(self, session_id, hours):
        self.exec("UPDATE labor_sessions SET started_at=? WHERE id=?", (ago(hours), session_id))

    # --- happy paths -----------------------------------------------------
    def test_clock_in_then_out_records_hours_and_cost(self):
        r = self.labor(project_id=self.project, section="Brakes")
        self.assertEqual(r.json["action"], "clock_in")
        s = self.sessions()[0]
        self.assertEqual((s["project_id"], s["section"], s["rate"], s["ended_at"]), (self.project, "Brakes", 40, None))
        self.backdate(s["id"], 2)
        r = self.labor()
        self.assertEqual(r.json["action"], "clock_out")
        s = self.sessions()[0]
        self.assertAlmostEqual(s["hours"], 2, places=2)
        self.assertAlmostEqual(s["cost"], s["hours"] * 40, places=6)
        self.assertEqual(len(self.sessions()), 1)

    def test_second_scan_clocks_out_instead_of_double_starting(self):
        self.labor(project_id=self.project)
        self.labor(project_id=self.project)
        self.assertEqual(len(self.sessions()), 1)
        self.assertIsNotNone(self.sessions()[0]["ended_at"])
        self.labor(project_id=self.project)
        open_ = [s for s in self.sessions() if s["ended_at"] is None]
        self.assertEqual(len(open_), 1)

    def test_rate_is_snapshotted_at_clock_in(self):
        self.labor(project_id=self.project)
        self.exec("UPDATE laborers SET rate=100 WHERE id=?", (self.laborer,))
        sid = self.sessions()[0]["id"]
        self.backdate(sid, 1)
        self.labor()
        self.assertAlmostEqual(self.sessions()[0]["cost"], 40, places=1)

    def test_general_shop_time_needs_a_note_to_clock_out(self):
        self.labor(general=True)
        sid = self.sessions()[0]["id"]
        r = self.labor()
        self.assertEqual((r.status_code, r.json["error"]), (400, "note_required"))
        self.assertIsNone(self.sessions()[0]["ended_at"])
        r = self.labor(note="Swept hangar")
        self.assertEqual(r.json["action"], "clock_out")
        self.assertEqual(self.q1("SELECT note FROM labor_sessions WHERE id=?", (sid,))["note"], "Swept hangar")

    def test_manual_stop_and_stop_twice(self):
        self.labor(project_id=self.project)
        sid = self.sessions()[0]["id"]
        self.backdate(sid, 1.5)
        r = self.client.post(f"/api/labor/stop/{sid}")
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(r.json["cost"], 60, places=0)
        first = dict(self.sessions()[0])
        self.assertEqual(self.client.post(f"/api/labor/stop/{sid}").status_code, 404)
        self.assertEqual(dict(self.sessions()[0]), first)
        self.assertEqual(self.client.post("/api/labor/stop/9999").status_code, 404)

    # --- rejections ------------------------------------------------------
    def test_rejections(self):
        cases = [
            (dict(code="PART-001", project_id=self.project), 400, "not_a_laborer_code"),
            (dict(code="LABOR-NOPE", project_id=self.project), 404, "unknown_laborer"),
            (dict(code="LABOR-AAAA0001"), 400, "no_task_selected"),
            (dict(code="LABOR-AAAA0001", project_id=9999), 404, "unknown_project"),
            (dict(code="LABOR-AAAA0001", project_id="abc"), 404, "unknown_project"),
        ]
        for body, status, err in cases:
            with self.subTest(body=body):
                r = self.client.post("/api/labor/scan", json=body)
                self.assertEqual((r.status_code, r.json.get("error")), (status, err))
        self.assertEqual(self.sessions(), [])

    def test_trashed_project_rejected(self):
        self.exec("UPDATE projects SET deleted_at=? WHERE id=?", (db.now_iso(), self.project))
        self.assertEqual(self.labor(project_id=self.project).status_code, 404)
        self.assertEqual(self.sessions(), [])

    def test_inactive_laborer_cannot_clock_in(self):
        self.exec("UPDATE laborers SET active=0 WHERE id=?", (self.laborer,))
        r = self.labor(project_id=self.project)
        self.assertEqual((r.status_code, r.json["error"]), (400, "inactive_laborer"))
        self.assertEqual(self.sessions(), [])

    def test_deactivated_laborer_can_still_clock_out(self):
        # Clocked in, then deactivated (quit / let go that afternoon): their
        # badge is refused, so the timer - and their pay - keeps running.
        self.labor(project_id=self.project)
        self.login("shop_admin")
        self.client.post(f"/laborers/{self.laborer}/edit", data=dict(name="Tech", rate="40"))  # active unchecked
        self.assertEqual(self.q1("SELECT active FROM laborers WHERE id=?", (self.laborer,))["active"], 0)
        still_open = self.q("SELECT id FROM labor_sessions WHERE ended_at IS NULL")
        if still_open:
            r = self.labor()
            self.assertEqual(r.status_code, 200, r.json)
            self.assertEqual(r.json["action"], "clock_out")
        self.assertEqual(self.q("SELECT id FROM labor_sessions WHERE ended_at IS NULL"), [])

    @open_finding("qa-labor-malformed-body")
    def test_garbled_labor_scans_are_rejected_not_crashes(self):
        for body in (["LABOR-AAAA0001"], "LABOR-AAAA0001", {"code": 12345}, {"code": ["LABOR-AAAA0001"]},
                     {"code": "LABOR-AAAA0001", "project_id": {"id": 1}}):
            with self.subTest(body=body):
                r = self.client.post("/api/labor/scan", json=body)
                self.assertIn(r.status_code, (400, 404))

    def test_accounts_without_shop_access_cannot_clock_labor(self):
        self.login("tech")
        self.labor(code="LABOR-BBBB0002", project_id=self.project)
        sid = self.sessions(self.other)[0]["id"]
        for role in ("flight_student", "cfi", "no_roles"):
            with self.subTest(role=role):
                self.login(role)
                r = self.labor(project_id=self.project)
                self.assertIn(r.status_code, (302, 401, 403))
                r = self.client.post(f"/api/labor/stop/{sid}")
                self.assertIn(r.status_code, (302, 401, 403))
        self.assertEqual(self.sessions(self.laborer), [])
        self.assertIsNone(self.sessions(self.other)[0]["ended_at"])

    def test_shop_roles_can_clock_labor(self):
        for role in ("tech", "shop_admin", "shop_student", "inspector", "master"):
            with self.subTest(role=role):
                self.login(role)
                self.assertEqual(self.labor(project_id=self.project).status_code, 200)
                self.assertEqual(self.labor(project_id=self.project).status_code, 200)

    @open_finding("qa-labor-closed-projects")
    def test_clocking_in_to_a_completed_or_archived_project_is_blocked(self):
        # Same rule Frank chose for parts (qa-scanout-closed-projects).
        for status in ("completed", "archived"):
            with self.subTest(status=status):
                self.exec("UPDATE projects SET status=? WHERE id=?", (status, self.project))
                r = self.labor(project_id=self.project)
                self.assertNotEqual(r.status_code, 200)
                self.assertEqual(self.sessions(), [])


class LaborerAdminTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("shop_admin")

    def test_new_laborer_gets_a_unique_code(self):
        self.client.post("/laborers/new", data=dict(name="Ann", rate="30"))
        self.client.post("/laborers/new", data=dict(name="Bob", rate=""))
        rows = self.q("SELECT * FROM laborers ORDER BY id")
        self.assertEqual([(r["name"], r["rate"]) for r in rows], [("Ann", 30), ("Bob", 0)])
        self.assertTrue(all(r["code"].startswith("LABOR-") for r in rows))
        self.assertNotEqual(rows[0]["code"], rows[1]["code"])

    def test_name_required_and_missing_laborer_404(self):
        self.client.post("/laborers/new", data=dict(name="  ", rate="30"))
        self.assertEqual(self.q("SELECT id FROM laborers"), [])
        self.assertEqual(self.client.get("/laborers/9999/edit").status_code, 404)

    def test_pay_rate_rejects_negative_or_nan(self):
        for rate in ("-25", "nan", "inf"):
            with self.subTest(rate=rate):
                r = self.client.post("/laborers/new", data=dict(name=f"Bad {rate}", rate=rate))
                self.assertNotEqual(r.status_code, 500)
        self.client.post("/laborers/new", data=dict(name="Ok", rate="30"))
        lid = self.q1("SELECT id FROM laborers WHERE name='Ok'")["id"]
        r = self.client.post(f"/laborers/{lid}/edit", data=dict(name="Ok", rate="-30", active="on"))
        self.assertNotEqual(r.status_code, 500)
        rates = [row["rate"] for row in self.q("SELECT rate FROM laborers")]
        self.assertTrue(all(x is not None and 0 <= x < float("inf") for x in rates), rates)


class LaborTotalsTest(OpsHubTestCase):
    """Pay, billing and stats totals must equal the sessions they come from."""

    def setUp(self):
        super().setUp()
        conn = db.get_db()
        self.a = seed_row(conn, "laborers", name="Tech", code="LABOR-A", rate=40, active=1)
        self.b = seed_row(conn, "laborers", name="Joe", code="LABOR-B", rate=25, active=1)
        conn.commit()
        conn.close()
        self.p1 = self.make_project(name="Annual")
        self.p2 = self.make_project(name="Brakes")
        today = datetime.now().strftime("%Y-%m-%d")
        rows = [  # laborer, project, hours, rate, ended?
            (self.a, self.p1, 2.0, 40, True), (self.a, self.p2, 1.5, 40, True),
            (self.b, self.p1, 3.0, 25, True), (self.b, None, 1.0, 25, True),
            (self.b, self.p2, 0, 25, False),  # still running: not paid or billed yet
        ]
        for lab, proj, h, rate, ended in rows:
            self.exec("INSERT INTO labor_sessions (laborer_id, project_id, started_at, ended_at, hours, rate, cost, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (lab, proj, today + " 08:00:00", (today + " 12:00:00") if ended else None,
                       h if ended else None, rate, h * rate if ended else None, db.now_iso()))

    def test_admin_pay_totals_match_sessions(self):
        self.login("shop_admin")
        html = self.client.get("/shop/pay?period=all").get_data(as_text=True)
        # 2*40 + 1.5*40 + 3*25 + 1*25 = 240
        self.assertIn("240.00", html)
        self.assertIn("7.5", html)

    def test_tech_sees_only_their_own_pay(self):
        self.login("tech")  # user name "Tech" matches laborer "Tech"
        html = self.client.get("/shop/pay?period=all").get_data(as_text=True)
        self.assertIn("140.00", html)
        self.assertNotIn("Joe", html)

    def test_billing_labor_matches_sessions_per_project(self):
        self.login("shop_admin")
        html = self.client.get("/shop/billing?period=all").get_data(as_text=True)
        self.assertIn("155.00", html)   # Annual: 80 + 75
        self.assertIn("60.00", html)    # Brakes: 60 (running session excluded)
        self.assertIn("215.00", html)   # total project labor; General Shop is not billed

    def test_stats_page_renders_with_open_and_general_sessions(self):
        self.login("shop_admin")
        for period in ("this_week", "last_month", "all", "nonsense"):
            with self.subTest(period=period):
                self.assertEqual(self.client.get(f"/shop/stats?period={period}").status_code, 200)
        self.assertEqual(self.client.get("/shop/stats?from=2026-13-40&to=x").status_code, 200)
