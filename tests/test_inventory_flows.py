"""End-to-end logic tests for the Shop Inventory core: scanning in/out,
parts, manual adjustments, project assignment, and receiving orders.

Every test drives the app through its real HTTP routes (the same requests
the Scan page / forms send) and then checks the database for the right
outcome - both the "happy path" and the ways people actually fat-finger
things at a parts counter.

Tests marked @open_finding("qa-...") describe the CORRECT behavior for a
bug that has been reported on the Idea Queue page's QA findings but not
fixed yet, so they're expected to fail. When Frank approves the finding and
the fix is made, the decorator comes off in the same change.
"""
import random
import unittest

from harness import OpsHubTestCase, seed_row, open_finding, GC_AFTER_REQUEST
import db


def ledger_qty(tc, part_id):
    """What qty_on_hand SHOULD be according to the audit trail."""
    row = tc.q1("""SELECT COALESCE(SUM(CASE type WHEN 'in' THEN qty WHEN 'out' THEN -qty
                                         WHEN 'adjust' THEN qty ELSE 0 END), 0) AS q
                    FROM transactions WHERE part_id = ?""", (part_id,))
    return row["q"]


class ScanFlowTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.part = self.make_part(qty=10)
        self.project = self.make_project()
        self.login("tech")

    # --- happy paths -----------------------------------------------------
    def test_scan_in_adds_stock_and_logs_it(self):
        r = self.scan("PART-001", "in", 5)
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(self.qty(self.part), 15)
        tx = self.q1("SELECT * FROM transactions WHERE part_id=? ORDER BY id DESC", (self.part,))
        self.assertEqual((tx["type"], tx["qty"], tx["performed_by"], tx["project_id"]), ("in", 5, "Frank", None))

    def test_scan_out_charges_the_project_and_section(self):
        r = self.scan("PART-001", "out", 3, self.project, section="Brakes")
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(self.qty(self.part), 7)
        tx = self.q1("SELECT * FROM transactions WHERE part_id=? ORDER BY id DESC", (self.part,))
        self.assertEqual((tx["type"], tx["project_id"], tx["section"]), ("out", self.project, "Brakes"))

    def test_scan_in_never_attaches_a_project(self):
        self.scan("PART-001", "in", 1, self.project, section="Brakes")
        tx = self.q1("SELECT * FROM transactions WHERE part_id=? ORDER BY id DESC", (self.part,))
        self.assertIsNone(tx["project_id"])
        self.assertIsNone(tx["section"])

    def test_barcode_with_scanner_whitespace_still_matches(self):
        # USB scanners often send a trailing newline/tab
        self.assertEqual(self.scan("  PART-001\n", "in", 1).status_code, 200)

    def test_fractional_quantities_work(self):
        self.scan("PART-001", "out", 0.5, self.project)
        self.assertAlmostEqual(self.qty(self.part), 9.5)

    def test_exact_remaining_stock_can_be_scanned_out(self):
        self.assertEqual(self.scan("PART-001", "out", 10, self.project).status_code, 200)
        self.assertEqual(self.qty(self.part), 0)

    # --- rejections: nothing should change -------------------------------
    def _assert_rejected(self, resp, status=400):
        self.assertEqual(resp.status_code, status, resp.get_data(as_text=True)[:300])
        self.assertEqual(self.qty(self.part), 10, "stock changed on a rejected scan")
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM transactions")["c"], 0, "rejected scan was logged")

    def test_cannot_scan_out_more_than_on_hand(self):
        self._assert_rejected(self.scan("PART-001", "out", 11, self.project))

    def test_stock_out_requires_a_project(self):
        self._assert_rejected(self.scan("PART-001", "out", 1, None))

    def test_stock_out_requires_scanning_as(self):
        self._assert_rejected(self.scan("PART-001", "in", 1, performed_by="  "))

    def test_unknown_barcode(self):
        self._assert_rejected(self.scan("NOT-A-PART", "in", 1), status=404)

    def test_empty_barcode(self):
        self._assert_rejected(self.scan("", "in", 1))

    def test_zero_negative_and_text_quantities(self):
        for bad in (0, -3, "abc", None, ""):
            with self.subTest(qty=bad):
                self._assert_rejected(self.scan("PART-001", "in", bad))

    def test_bad_action(self):
        self._assert_rejected(self.scan("PART-001", "sideways", 1))

    def test_scan_out_to_trashed_project_rejected(self):
        self.exec("UPDATE projects SET deleted_at=? WHERE id=?", (db.now_iso(), self.project))
        self._assert_rejected(self.scan("PART-001", "out", 1, self.project), status=404)

    def test_scan_out_to_completed_or_archived_project_rejected(self):
        # Marking a project completed/archived used to have no effect on
        # scanning - stock could still be charged to it, silently changing
        # the total on a job that's already been billed.
        for status in ("completed", "archived"):
            with self.subTest(status=status):
                self.exec("UPDATE projects SET status=? WHERE id=?", (status, self.project))
                self._assert_rejected(self.scan("PART-001", "out", 1, self.project))
        self.exec("UPDATE projects SET status='on_hold' WHERE id=?", (self.project,))
        self.assertEqual(self.scan("PART-001", "out", 1, self.project).status_code, 200)

    def test_scan_out_to_nonexistent_project_rejected(self):
        self._assert_rejected(self.scan("PART-001", "out", 1, 9999), status=404)

    def test_nan_and_infinity_quantities_rejected(self):
        # float("nan") slipped past the `qty <= 0`
        # check and wrote NaN into qty_on_hand, permanently corrupting the part.
        for bad in ("nan", "NaN", "inf", "-inf", "1e309"):
            with self.subTest(qty=bad):
                self._assert_rejected(self.scan("PART-001", "in", bad))

    def test_non_object_json_body_is_a_400_not_a_crash(self):
        r = self.client.post("/api/scan", json=["PART-001", "in"])
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/scan", data="not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    # --- who may scan -----------------------------------------------------
    def test_accounts_without_shop_access_cannot_change_inventory(self):
        # /api/scan was @login_required only, so a
        # flight student (or any account with no Shop role) could add/remove
        # stock and charge parts to jobs.
        for role in ("flight_student", "cfi", "no_roles"):
            with self.subTest(role=role):
                self.login(role)
                r = self.scan("PART-001", "in", 5)
                self.assertIn(r.status_code, (302, 403))
                self.assertEqual(self.qty(self.part), 10)

    def test_shop_roles_can_scan(self):
        for role in ("tech", "shop_admin", "master", "shop_student", "inspector"):
            with self.subTest(role=role):
                self.login(role)
                self.assertEqual(self.scan("PART-001", "in", 1).status_code, 200)

    def test_costs_hidden_from_non_admins_on_scan_and_lookup(self):
        # A tech doesn't see prices on screen, but the scan response and the
        # scanner's own barcode lookup used to send unit_cost/sell_price to
        # anyone logged in regardless of role.
        for role, visible in (("tech", False), ("shop_student", False), ("shop_admin", True), ("master", True)):
            with self.subTest(role=role):
                self.login(role)
                r = self.scan("PART-001", "in", 1)
                self.assertEqual(("unit_cost" in r.json), visible)
                self.assertEqual(("sell_price" in r.json), visible)
                r2 = self.client.get("/api/lookup/PART-001")
                self.assertEqual(("unit_cost" in r2.json), visible)
                self.assertEqual(("sell_price" in r2.json), visible)


class PartFlowTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("shop_admin")

    def _new(self, **form):
        data = dict(name="Brake Pad", barcode="", qty_on_hand="4", reorder_point="1", unit_cost="30",
                    sell_price="45", unit="ea")
        data.update(form)
        return self.client.post("/parts/new", data=data)

    def test_new_part_with_generated_barcode_and_initial_stock(self):
        self._new(generate_barcode="on")
        p = self.q1("SELECT * FROM parts WHERE name='Brake Pad'")
        self.assertTrue(p["barcode"].startswith("SHOP-"))
        self.assertEqual(p["qty_on_hand"], 4)
        self.assertEqual(ledger_qty(self, p["id"]), 4, "initial stock not logged as an 'in' transaction")

    def test_duplicate_barcode_rejected(self):
        self.make_part(barcode="DUP-1")
        self._new(barcode="DUP-1")
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM parts WHERE barcode='DUP-1'")["c"], 1)

    def test_part_name_required(self):
        self._new(name="  ")
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM parts")["c"], 0)

    def test_new_part_rejects_nan_or_negative_stock(self):
        for bad in ("nan", "inf", "-5"):
            with self.subTest(qty=bad):
                self._new(name="Bad " + bad, qty_on_hand=bad)
                self.assertIsNone(self.q1("SELECT * FROM parts WHERE name=?", ("Bad " + bad,)))

    def test_edit_cannot_blank_the_name(self):
        # Edit saved an empty name, leaving a
        # part with no visible label anywhere in the app.
        pid = self.make_part()
        self.client.post(f"/parts/{pid}/edit", data=dict(name="", unit="ea"))
        self.assertEqual(self.q1("SELECT name FROM parts WHERE id=?", (pid,))["name"], "Oil Filter")

    def test_count_adjustment_sets_qty_and_logs_delta(self):
        pid = self.make_part(qty=10)
        self.exec("INSERT INTO transactions (part_id, type, qty, source, created_at) VALUES (?, 'in', 10, 'assigned', ?)",
                  (pid, db.now_iso()))
        self.client.post(f"/parts/{pid}/adjust", data=dict(new_qty="7", performed_by="Frank"))
        self.assertEqual(self.qty(pid), 7)
        self.assertEqual(ledger_qty(self, pid), 7)

    def test_count_adjustment_rejects_negative(self):
        # A physical count can't be negative.
        pid = self.make_part(qty=10)
        self.client.post(f"/parts/{pid}/adjust", data=dict(new_qty="-2", performed_by="Frank"))
        self.assertEqual(self.qty(pid), 10)

    def test_count_adjustment_rejects_nan_or_infinity(self):
        pid = self.make_part(qty=10)
        for bad in ("nan", "inf"):
            with self.subTest(new_qty=bad):
                self.client.post(f"/parts/{pid}/adjust", data=dict(new_qty=bad, performed_by="Frank"))
                self.assertEqual(self.qty(pid), 10)

    def test_count_adjustment_requires_scanning_as(self):
        pid = self.make_part(qty=10)
        self.client.post(f"/parts/{pid}/adjust", data=dict(new_qty="3"))
        self.assertEqual(self.qty(pid), 10)

    def test_tech_cannot_adjust_or_delete(self):
        pid = self.make_part(qty=10)
        self.login("tech")
        self.client.post(f"/parts/{pid}/adjust", data=dict(new_qty="0", performed_by="x"))
        self.client.post(f"/parts/{pid}/delete")
        self.assertEqual(self.qty(pid), 10)

    def test_deleting_a_part_keeps_project_parts_history(self):
        # NEEDS FRANK'S DECISION: Delete Part also runs
        # DELETE FROM transactions WHERE part_id = ?, which silently erases
        # that part from every project's parts list and parts cost - including
        # completed jobs already billed. Options: block delete once a part has
        # been used on a project, or soft-delete parts like projects.
        pid = self.make_part(qty=10)
        proj = self.make_project()
        self.login("tech")
        self.scan("PART-001", "out", 2, proj)
        self.login("shop_admin")
        self.client.post(f"/parts/{pid}/delete")
        used = self.q1("SELECT COUNT(*) c FROM transactions WHERE project_id=?", (proj,))["c"]
        self.assertEqual(used, 1, "project lost its parts usage when the part was deleted")


class ProjectPartsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.part = self.make_part(qty=5)
        self.project = self.make_project()
        self.login("tech")

    def _assign(self, qty, project=None, **extra):
        data = dict(part_id=self.part, qty=str(qty), performed_by="Frank")
        data.update(extra)
        return self.client.post(f"/projects/{project or self.project}/add_part", data=data)

    def test_assign_part_from_project_page(self):
        self._assign(2, section="Engine")
        self.assertEqual(self.qty(self.part), 3)
        tx = self.q1("SELECT * FROM transactions ORDER BY id DESC")
        self.assertEqual((tx["project_id"], tx["section"], tx["type"]), (self.project, "Engine", "out"))

    def test_assign_more_than_on_hand_rejected(self):
        self._assign(6)
        self.assertEqual(self.qty(self.part), 5)

    def test_assign_nan_rejected(self):
        # - same NaN hole as the scan API
        self._assign("nan")
        self.assertEqual(self.qty(self.part), 5)

    def test_assign_to_trashed_project_rejected(self):
        # the project page's Add Part didn't
        # check deleted_at, unlike the Scan page, so stock could be charged to
        # a job sitting in Recently Deleted.
        self.exec("UPDATE projects SET deleted_at=? WHERE id=?", (db.now_iso(), self.project))
        self._assign(1)
        self.assertEqual(self.qty(self.part), 5)

    def test_parts_used_report_matches_what_was_charged(self):
        self._assign(2, section="Engine")
        self.scan("PART-001", "out", 1, self.project, section="Brakes")
        self.login("shop_admin")
        html = self.client.get("/projects/parts-used").get_data(as_text=True)
        self.assertIn("Engine", html)
        self.assertIn("Brakes", html)

    def test_parts_used_hides_cost_from_tech_page_source(self):
        # The Cost column was already hidden on screen for a tech, but its
        # value still leaked into the page source via a data-cost attribute.
        self._assign(2, section="Engine")
        self.login("tech")
        html = self.client.get("/projects/parts-used").get_data(as_text=True)
        self.assertNotIn("data-cost", html)
        self.login("shop_admin")
        html = self.client.get("/projects/parts-used").get_data(as_text=True)
        self.assertIn("data-cost", html)

    @open_finding("qa-status-missing-project")
    def test_status_change_on_missing_project_is_404(self):
        r = self.client.post("/projects/9999/status", data=dict(status="completed"))
        self.assertEqual(r.status_code, 404)


class OrderReceiveTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.part = self.make_part(qty=2)
        conn = db.get_db()
        self.order = seed_row(conn, "orders", description="Oil Filter", part_id=self.part, qty_ordered=6,
                              unit_cost=12.5, status="ordered")
        self.new_item_order = seed_row(conn, "orders", description="Tire 5.00-5", part_id=None, qty_ordered=2,
                                       unit_cost=90, status="ordered")
        conn.commit()
        conn.close()
        self.login("shop_admin")

    def test_receiving_adds_stock_once(self):
        self.client.post(f"/orders/{self.order}/receive")
        self.assertEqual(self.qty(self.part), 8)
        self.assertEqual(ledger_qty(self, self.part), 6)

    def test_receiving_twice_does_not_double_count(self):
        # Receive had no status check - a
        # double-click, a Back-button resubmit, or two people receiving the
        # same box added the stock twice.
        self.client.post(f"/orders/{self.order}/receive")
        self.client.post(f"/orders/{self.order}/receive")
        self.assertEqual(self.qty(self.part), 8)

    def test_cancelled_order_cannot_be_received(self):
        self.client.post(f"/orders/{self.order}/cancel")
        self.client.post(f"/orders/{self.order}/receive")
        self.assertEqual(self.qty(self.part), 2)
        self.assertEqual(self.q1("SELECT status FROM orders WHERE id=?", (self.order,))["status"], "cancelled")

    def test_received_order_cannot_be_cancelled(self):
        # cancelling after receiving left the
        # stock in place but showed the order as cancelled.
        self.client.post(f"/orders/{self.order}/receive")
        self.client.post(f"/orders/{self.order}/cancel")
        self.assertEqual(self.q1("SELECT status FROM orders WHERE id=?", (self.order,))["status"], "received")

    def test_receiving_an_item_not_in_inventory_creates_the_part(self):
        self.client.post(f"/orders/{self.new_item_order}/receive")
        o = self.q1("SELECT * FROM orders WHERE id=?", (self.new_item_order,))
        self.assertIsNotNone(o["part_id"])
        self.assertEqual(self.qty(o["part_id"]), 2)
        self.client.post(f"/orders/{self.new_item_order}/receive")
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM parts WHERE name='Tire 5.00-5'")["c"], 1)

    def test_export_csv_hides_costs_from_techs(self):
        # A tech's CSV export used to include Unit Cost/Est. Total columns
        # even though techs don't see prices on screen.
        self.login("tech")
        csv_text = self.client.get("/orders/export?status=all").get_data(as_text=True)
        self.assertNotIn("Unit Cost", csv_text)
        self.assertNotIn("12.50", csv_text)
        self.login("shop_admin")
        csv_text = self.client.get("/orders/export?status=all").get_data(as_text=True)
        self.assertIn("Unit Cost", csv_text)
        self.assertIn("12.50", csv_text)


class LedgerInvariantTest(OpsHubTestCase):
    """The big one: after a long random mix of everything people do at the
    counter (scan in, scan out, assign from project page, recount, receive
    orders, plus a pile of invalid attempts), every part's qty_on_hand must
    still equal what its transaction history adds up to, and never go
    negative. Seeded, so a failure is reproducible."""

    def test_random_day_at_the_parts_counter(self):
        rnd = random.Random(1234)
        parts = [self.make_part(name=f"P{i}", barcode=f"B{i}", qty=rnd.randint(0, 20)) for i in range(5)]
        for pid in parts:
            q = self.qty(pid)
            if q:
                self.exec("INSERT INTO transactions (part_id, type, qty, source, created_at) VALUES (?, 'in', ?, 'assigned', ?)",
                          (pid, q, db.now_iso()))
        projects = [self.make_project(name=f"Job {i}") for i in range(3)]
        conn = db.get_db()
        orders = [seed_row(conn, "orders", description=f"O{i}", part_id=rnd.choice(parts), qty_ordered=rnd.randint(1, 5),
                           status="ordered") for i in range(6)]
        conn.commit()
        conn.close()

        for step in range(400):
            op = rnd.choice(["in", "out", "assign", "adjust", "receive", "junk"])
            i = rnd.randrange(5)
            qty = rnd.choice([1, 2, 3, 0.5, 7, 25, 0, -1, "x"])
            if op in ("in", "out"):
                self.login("tech")
                self.scan(f"B{i}", op, qty, rnd.choice(projects + [None]))
            elif op == "assign":
                self.login("tech")
                self.client.post(f"/projects/{rnd.choice(projects)}/add_part",
                                 data=dict(part_id=parts[i], qty=str(qty), performed_by="Rnd"))
            elif op == "adjust":
                self.login("shop_admin")
                self.client.post(f"/parts/{parts[i]}/adjust",
                                 data=dict(new_qty=str(rnd.choice([0, 3, 10, -4, "nan"])), performed_by="Rnd"))
            elif op == "receive":
                self.login("shop_admin")
                self.client.post(f"/orders/{rnd.choice(orders)}/receive")
            else:
                self.login("tech")
                self.scan(rnd.choice(["", "ZZZ", f"B{i}"]), rnd.choice(["in", "out", "bogus"]),
                          rnd.choice(["nan", "inf", -1, 0]), None)

        for pid in parts:
            q = self.qty(pid)
            self.assertEqual(q, q, f"part {pid} qty is NaN")
            self.assertGreaterEqual(q, 0, f"part {pid} went negative")
            self.assertAlmostEqual(q, ledger_qty(self, pid), msg=f"part {pid}: on-hand doesn't match its history")


class LoginFlowTest(OpsHubTestCase):
    def test_real_login_form(self):
        c = self.login(None)
        r = c.post("/", data=dict(username="tech", password="test-pass-123", remember="on"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get("/scan").status_code, 200)

    def test_wrong_password(self):
        c = self.login(None)
        c.post("/", data=dict(username="tech", password="nope"))
        with c.session_transaction() as s:
            self.assertNotIn("user_id", s)

    def test_deactivated_account_cannot_log_in(self):
        self.exec("UPDATE users SET active=0 WHERE username='tech'")
        c = self.login(None)
        c.post("/", data=dict(username="tech", password="test-pass-123"))
        with c.session_transaction() as s:
            self.assertNotIn("user_id", s)

    def test_customer_only_sees_their_own_aircraft(self):
        mine = self.make_asset("N111")
        theirs = self.make_asset("N222")
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, mine))
        c = self.login("customer")
        self.assertEqual(c.get(f"/portal/aircraft/{mine}").status_code, 200)
        self.assertIn(c.get(f"/portal/aircraft/{theirs}").status_code, (302, 403, 404))

    def test_flight_only_accounts_cannot_open_shop_pages(self):
        # A flight student or CFI (no Shop role) used to get a normal 200 on
        # every shop page - just @login_required, no role check.
        pid = self.make_project()
        for role in ("flight_student", "cfi", "no_roles"):
            with self.subTest(role=role):
                c = self.login(role)
                for url in ("/shop", "/scan", "/projects", f"/projects/{pid}",
                           "/projects/parts-used", "/calendar", "/shop/pay"):
                    self.assertIn(c.get(url).status_code, (302, 403), url)
        for role in ("tech", "shop_admin", "shop_student", "master", "inspector"):
            with self.subTest(role=role):
                self.assertEqual(self.login(role).get("/shop").status_code, 200)


class CrashCleanupTest(OpsHubTestCase):
    def test_db_not_left_locked_after_a_crash(self):
        """A route that crashes between writing and committing must not leave
        the database write-locked for everyone else. A temporary trigger makes
        the scan's INSERT fail after its UPDATE has already opened a write
        transaction, the same shape as any real crash mid-save."""
        import sqlite3
        GC_AFTER_REQUEST[0] = False  # the harness safety net would hide the bug
        self.make_part(qty=10)
        self.login("tech")
        conn = db.get_db()
        conn.execute("CREATE TRIGGER IF NOT EXISTS boom BEFORE INSERT ON transactions "
                     "BEGIN SELECT RAISE(ABORT, 'forced crash for test'); END")
        conn.commit(); conn.close()
        self.scan("PART-001", "in", 1)
        other = sqlite3.connect(db.DB_PATH, timeout=1)
        try:
            other.execute("UPDATE parts SET name = name")
            other.commit()
        except sqlite3.OperationalError as e:
            self.fail("database still locked after a crashed request: %s" % e)
        finally:
            other.close()
