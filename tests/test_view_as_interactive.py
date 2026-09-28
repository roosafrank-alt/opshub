"""Idea "View as": Frank asked for View As to let a master admin actually
interact as the role being previewed (schedule flights, clock in, etc.),
not just look at pages read-only.

Checked the code and this already works: start_view_as() (auth.py) swaps
the real session fields (is_master_admin, shop_role/flight_role, cfi_id/
student_id) for the previewed role's own, so every shop_role_required/
cfi_required check downstream treats the session as if it really were that
role - actions go through and hit the real database, and a role the
preview doesn't have access to is refused exactly like a real account in
that role would be. These tests lock that behavior in so it can't quietly
regress."""
from harness import OpsHubTestCase
import db


class ViewAsInteractiveTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")

    def test_viewing_as_tech_can_add_a_plane_todo(self):
        c = self.login("master")
        c.post("/view-as/shop/tech")
        r = c.post(f"/assets/{self.asset_id}/todo/new", data={"description": "Torque check"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM plane_todos WHERE asset_id = ?", (self.asset_id,))
        self.assertIsNotNone(row)
        self.assertEqual(row["description"], "Torque check")
        # Attributed to the real account doing the work, not a fictional one.
        self.assertEqual(row["created_by"], "Master")

    def test_viewing_as_apprentice_is_still_refused_admin_tech_only_pages(self):
        # Apprentice really can't reach Parts - the preview has to refuse it
        # too, or it isn't actually previewing that role.
        c = self.login("master")
        c.post("/view-as/shop/apprentice")
        r = c.get("/parts")
        self.assertEqual(r.status_code, 302)

    def test_viewing_as_cfi_can_schedule_a_real_flight(self):
        c = self.login("master")
        c.post("/view-as/flight/cfi")
        with c.session_transaction() as s:
            cfi_id = s.get("cfi_id")
        self.assertIsNotNone(cfi_id)
        student_id = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]
        r = c.post("/flight/schedule/new", data={
            "asset_id": self.asset_id, "student_id": student_id, "cfi_id": cfi_id,
            "scheduled_date": "2026-10-05", "scheduled_time": "10:00", "duration_hours": "1.5",
        })
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM scheduled_flights WHERE asset_id = ?", (self.asset_id,))
        self.assertIsNotNone(row)
        self.assertEqual(row["cfi_id"], cfi_id)
        self.assertEqual(row["created_by"], "Master")

    def test_exiting_view_as_restores_the_real_admin_session(self):
        c = self.login("master")
        c.post("/view-as/shop/tech")
        c.post("/view-as/exit")
        with c.session_transaction() as s:
            self.assertTrue(s["is_master_admin"])
            self.assertEqual(s["shop_role"], "admin")

    def test_view_as_chip_row_offers_inspector(self):
        # Idea "view as": the Maintenance tab's View as chips were missing
        # Inspector (SHOP_VIEW_AS_LEVELS only had admin/tech/apprentice).
        c = self.login("master")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn(">Inspector<", body)

    def test_viewing_as_inspector_can_sign_off_a_squawk_repair(self):
        squawk_id = self.exec(
            "INSERT INTO plane_squawks (asset_id, notes, reported_at, acknowledged_at, acknowledged_by, "
            "assigned_to, worker_acknowledged_at, repair_confirm_requested_at, repair_confirm_requested_by) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.asset_id, "Left brake soft", db.now_iso(), db.now_iso(), "Tech",
             self.users["tech"]["id"], db.now_iso(), db.now_iso(), "Tech"))
        c = self.login("master")
        c.post("/view-as/shop/inspector")
        with c.session_transaction() as s:
            self.assertEqual(s["shop_role"], "inspector")
        r = c.post(f"/squawks/quick/{squawk_id}/repair_confirm")
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM plane_squawks WHERE id = ?", (squawk_id,))
        self.assertIsNotNone(row["repaired_at"])

    def test_viewing_as_inspector_is_still_refused_parts(self):
        c = self.login("master")
        c.post("/view-as/shop/inspector")
        r = c.get("/parts")
        self.assertEqual(r.status_code, 302)
