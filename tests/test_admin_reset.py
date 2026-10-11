"""Admin Reset Data page: only master admins can use it, and each button
clears only what it says and leaves everything else alone."""
from harness import OpsHubTestCase
import db

RESETS = ["activity_log", "orders", "squawks", "flights", "projects", "planes",
          "schedule", "billing", "students", "instructors", "flight_school"]


class AdminResetTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.part = self.make_part()
        self.asset = self.make_asset("N900AB")
        self.proj = self.make_project("Reset job", asset_id=self.asset)
        self.exec("INSERT INTO transactions (part_id, project_id, type, qty, source, created_at) "
                  "VALUES (?,?, 'out', 1, 'assigned', ?)", (self.part, self.proj, db.now_iso()))
        self.exec("INSERT INTO orders (part_id, description, qty_ordered, supplier, unit_cost, project_id, created_at) "
                  "VALUES (?,?,?,?,?,?,?)", (self.part, "Filter", 2, "Spruce", 5.0, self.proj, db.now_iso()))
        self.student = self.q1("SELECT id FROM students WHERE user_id=?",
                               (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE students SET balance = 150 WHERE id=?", (self.student,))
        self.exec("INSERT INTO student_ledger (student_id, entry_type, amount, created_at) VALUES (?,?,?,?)",
                  (self.student, "funds_added", 150, db.now_iso()))

    def n(self, table):
        return self.q1(f"SELECT COUNT(*) c FROM {table}")["c"]

    def test_only_master_admin_can_reset(self):
        for role in ("shop_admin", "tech", "inspector", "cfi", "flight_student", "no_roles"):
            c = self.login(role)
            for name in RESETS:
                c.post(f"/admin/reset/{name}", data={"confirm_text": "RESET"})
            self.assertEqual(self.n("transactions"), 1, role)
            self.assertEqual(self.n("orders"), 1, role)
            self.assertEqual(self.n("projects"), 1, role)
            self.assertEqual(self.n("student_ledger"), 1, role)
            self.assertIsNotNone(self.q1("SELECT id FROM students WHERE id=?", (self.student,)), role)

    def test_reset_page_is_master_only(self):
        for role in ("shop_admin", "tech", "no_roles"):
            self.assertIn(self.login(role).get("/admin/reset").status_code, (302, 403), role)
        self.assertEqual(self.login("master").get("/admin/reset").status_code, 200)

    def test_activity_log_reset_clears_only_the_log(self):
        self.login("master").post("/admin/reset/activity_log")
        self.assertEqual(self.n("transactions"), 0)
        self.assertEqual(self.n("orders"), 1)
        self.assertEqual(self.n("parts"), 1)
        self.assertEqual(self.n("projects"), 1)

    def test_orders_reset_clears_only_orders(self):
        self.login("master").post("/admin/reset/orders")
        self.assertEqual(self.n("orders"), 0)
        self.assertEqual(self.n("transactions"), 1)
        self.assertEqual(self.n("parts"), 1)
        self.assertEqual(self.n("projects"), 1)

    def test_projects_reset_keeps_parts_history_and_orders(self):
        self.login("master").post("/admin/reset/projects")
        self.assertEqual(self.n("projects"), 0)
        self.assertEqual(self.n("parts"), 1)
        self.assertEqual(self.n("transactions"), 1)
        self.assertEqual(self.n("orders"), 1)
        self.assertIsNone(self.q1("SELECT project_id FROM transactions")["project_id"])
        self.assertIsNone(self.q1("SELECT project_id FROM orders")["project_id"])
        self.assertEqual(self.n("assets"), 1)

    def test_projects_reset_restarts_numbering(self):
        self.login("master").post("/admin/reset/projects")
        pid = self.make_project("After reset")
        self.assertTrue(self.q1("SELECT code FROM projects WHERE id=?", (pid,))["code"].endswith("-001"))

    def test_billing_reset_zeroes_balances_but_keeps_students(self):
        self.login("master").post("/admin/reset/billing")
        self.assertEqual(self.n("student_ledger"), 0)
        self.assertEqual(self.q1("SELECT balance FROM students WHERE id=?", (self.student,))["balance"], 0)
        self.assertEqual(self.n("orders"), 1)

    def test_students_reset_removes_students_not_shop_data(self):
        self.login("master").post("/admin/reset/students")
        self.assertIsNone(self.q1("SELECT id FROM students WHERE id=?", (self.student,)))
        self.assertEqual(self.n("student_ledger"), 0)
        self.assertEqual(self.n("projects"), 1)
        self.assertEqual(self.n("parts"), 1)
        self.assertEqual(self.n("orders"), 1)

    def test_instructors_reset_keeps_master_admins_own_profile(self):
        master_cfi = self.q1("SELECT id FROM cfis WHERE user_id=?", (self.users["master"]["id"],))
        self.login("master").post("/admin/reset/instructors")
        self.assertIsNotNone(self.q1("SELECT id FROM cfis WHERE id=?", (master_cfi["id"],)))
        self.assertIsNone(self.q1("SELECT id FROM cfis WHERE user_id=?", (self.users["cfi"]["id"],)))
        self.assertIsNotNone(self.q1("SELECT id FROM students WHERE id=?", (self.student,)))

    def test_flight_school_reset_needs_the_word_reset(self):
        c = self.login("master")
        for text in ("", "reset now", "RESETT", None):
            c.post("/admin/reset/flight_school", data={} if text is None else {"confirm_text": text})
            self.assertIsNotNone(self.q1("SELECT id FROM students WHERE id=?", (self.student,)), text)
            self.assertEqual(self.n("student_ledger"), 1, text)

    def test_flight_school_reset_leaves_shop_side_alone(self):
        self.login("master").post("/admin/reset/flight_school", data={"confirm_text": " reset "})
        self.assertIsNone(self.q1("SELECT id FROM students WHERE id=?", (self.student,)))
        self.assertEqual(self.n("parts"), 1)
        self.assertEqual(self.n("transactions"), 1)
        self.assertEqual(self.n("orders"), 1)
        self.assertEqual(self.n("projects"), 1)
        shop_admin = self.q1("SELECT id FROM users WHERE username='shop_admin'")
        self.assertIsNotNone(shop_admin)
        self.assertIsNotNone(self.q1("SELECT id FROM users WHERE username='master'"))

    def test_squawk_reset_keeps_flight_log_entries(self):
        cfi = self.q1("SELECT id FROM cfis WHERE user_id=?", (self.users["cfi"]["id"],))["id"]
        fid = self.exec("INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, squawk) VALUES (?,?,?,?,1)",
                        (cfi, self.student, self.asset, "2026-01-02"))
        self.exec("INSERT INTO plane_squawks (asset_id, notes, reported_by, reported_at) VALUES (?,?,?,?)",
                  (self.asset, "Left brake", "cfi", db.now_iso()))
        self.login("master").post("/admin/reset/squawks")
        self.assertEqual(self.n("plane_squawks"), 0)
        self.assertEqual(self.q1("SELECT squawk FROM flights WHERE id=?", (fid,))["squawk"], 0)
