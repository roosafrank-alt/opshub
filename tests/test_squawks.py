"""Squawk flows, both kinds: "quick" (reported straight against a plane) and
"flight" (flagged when a flight is logged). Report, acknowledge, assign,
worker accept, mark repaired, inspector confirm or send back.

Tests marked @open_finding("qa-...") describe the CORRECT behavior for a
problem reported on the Idea Queue page but not fixed yet.
"""
from harness import OpsHubTestCase, seed_row, open_finding
import db


class SquawkBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N12345")
        conn = db.get_db()
        self.quick = seed_row(conn, "plane_squawks", asset_id=self.asset, notes="Left brake soft",
                              reported_by="Frank", reported_at=db.now_iso())
        stu = conn.execute("SELECT id FROM students WHERE user_id=?",
                           (self.users["flight_student"]["id"],)).fetchone()["id"]
        self.flight = seed_row(conn, "flights", asset_id=self.asset, student_id=stu,
                               flight_date=db.now_iso()[:10], squawk=1, notes="Radio static")
        conn.commit()
        conn.close()

    def sq(self, kind="quick", sid=None):
        if kind == "quick":
            r = self.q1("SELECT * FROM plane_squawks WHERE id=?", (sid or self.quick,))
            return dict(r) if r else None
        r = self.q1("SELECT * FROM flights WHERE id=?", (sid or self.flight,))
        if not r:
            return None
        r = dict(r)
        return {k[len("squawk_"):] if k.startswith("squawk_") else k: v for k, v in r.items()}

    def ids(self, kind):
        return self.quick if kind == "quick" else self.flight

    def post(self, kind, step, **data):
        return self.client.post(f"/squawks/{kind}/{self.ids(kind)}/{step}", data=data)


class ReportTest(SquawkBase):
    def test_tech_reports_from_squawks_page(self):
        self.login("tech")
        r = self.client.post("/squawks/new", data=dict(asset_id=self.asset, notes="  Oil leak  "))
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM plane_squawks ORDER BY id DESC LIMIT 1")
        self.assertEqual(row["notes"], "Oil leak")
        self.assertEqual(row["reported_by"], "Tech")

    def test_blank_notes_or_no_plane_rejected(self):
        self.login("tech")
        before = self.q1("SELECT COUNT(*) n FROM plane_squawks")["n"]
        self.client.post("/squawks/new", data=dict(asset_id=self.asset, notes="   "))
        self.client.post("/squawks/new", data=dict(notes="Oil leak"))
        self.client.post("/squawks/new", data=dict(asset_id="abc", notes="Oil leak"))
        self.client.post(f"/assets/{self.asset}/squawk", data=dict(notes=""))
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM plane_squawks")["n"], before)

    def test_missing_plane_is_404(self):
        self.login("tech")
        self.assertEqual(self.client.post("/squawks/new", data=dict(asset_id=999, notes="x")).status_code, 404)
        self.assertEqual(self.client.post("/assets/999/squawk", data=dict(notes="x")).status_code, 404)

    def test_only_admin_and_tech_can_report(self):
        for role in ("inspector", "shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            before = self.q1("SELECT COUNT(*) n FROM plane_squawks")["n"]
            self.client.post("/squawks/new", data=dict(asset_id=self.asset, notes="x"))
            self.client.post(f"/assets/{self.asset}/squawk", data=dict(notes="x"))
            self.assertEqual(self.q1("SELECT COUNT(*) n FROM plane_squawks")["n"], before, role)

    def test_cannot_report_against_a_trashed_plane(self):
        self.exec("UPDATE assets SET deleted_at=? WHERE id=?", (db.now_iso(), self.asset))
        self.login("tech")
        before = self.q1("SELECT COUNT(*) n FROM plane_squawks")["n"]
        self.client.post("/squawks/new", data=dict(asset_id=self.asset, notes="Oil leak"))
        self.client.post(f"/assets/{self.asset}/squawk", data=dict(notes="Oil leak"))
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM plane_squawks")["n"], before)

    def test_squawks_page_renders_for_each_shop_role(self):
        for role in ("master", "shop_admin", "tech", "inspector"):
            self.login(role)
            for step in ("", "?step=new", "?step=bogus", "?report=1"):
                r = self.client.get("/squawks" + step)
                self.assertEqual(r.status_code, 200, (role, step))

    def test_squawks_page_closed_to_others(self):
        for role in ("shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            self.assertNotEqual(self.client.get("/squawks").status_code, 200, role)


class WorkflowTest(SquawkBase):
    def test_full_happy_path_both_kinds(self):
        tech_id = self.users["tech"]["id"]
        for kind in ("quick", "flight"):
            self.login("shop_admin")
            self.post(kind, "acknowledge", assigned_to=str(tech_id))
            s = self.sq(kind)
            self.assertTrue(s["acknowledged_at"], kind)
            self.assertEqual(s["assigned_to"], tech_id, kind)
            self.login("tech")
            self.post(kind, "worker_ack")
            self.assertTrue(self.sq(kind)["worker_acknowledged_at"], kind)
            self.post(kind, "repair")
            self.assertTrue(self.sq(kind)["repair_confirm_requested_at"], kind)
            self.assertIsNone(self.sq(kind)["repaired_at"], kind)
            self.login("inspector")
            self.post(kind, "repair_confirm")
            s = self.sq(kind)
            self.assertTrue(s["repaired_at"], kind)
            self.assertEqual(s["repaired_by"], "Inspector", kind)
            self.assertIsNone(s["repair_confirm_requested_at"], kind)

    def test_send_back_keeps_it_open_with_note(self):
        self.login("tech")
        self.post("quick", "repair")
        self.login("inspector")
        self.post("quick", "repair_confirm", action="send_back", note="Still soft")
        s = self.sq()
        self.assertIsNone(s["repaired_at"])
        self.assertIsNone(s["repair_confirm_requested_at"])
        self.assertEqual(s["sent_back_note"], "Still soft")
        self.login("tech")
        self.post("quick", "repair")
        self.assertIsNone(self.sq()["sent_back_note"], "a new repair request clears the old send-back note")

    def test_repair_acknowledges_if_not_yet(self):
        self.login("tech")
        self.post("quick", "repair")
        self.assertTrue(self.sq()["acknowledged_at"])

    def test_reassign_clears_worker_accept(self):
        a, t = self.users["shop_admin"]["id"], self.users["tech"]["id"]
        self.login("shop_admin")
        self.post("quick", "assign", assigned_to=str(t))
        self.login("tech")
        self.post("quick", "worker_ack")
        self.login("shop_admin")
        self.post("quick", "assign", assigned_to=str(a))
        s = self.sq()
        self.assertEqual(s["assigned_to"], a)
        self.assertIsNone(s["worker_acknowledged_at"])
        self.post("quick", "assign", assigned_to="")
        self.assertIsNone(self.sq()["assigned_to"])

    def test_worker_ack_only_by_assignee_or_master(self):
        self.login("shop_admin")
        self.post("quick", "assign", assigned_to=str(self.users["tech"]["id"]))
        self.post("quick", "worker_ack")
        self.assertIsNone(self.sq()["worker_acknowledged_at"], "admin who isn't the assignee")
        self.login("master")
        self.post("quick", "worker_ack")
        self.assertTrue(self.sq()["worker_acknowledged_at"])

    def test_worker_ack_on_unassigned_squawk_rejected(self):
        self.login("tech")
        self.post("quick", "worker_ack")
        self.assertIsNone(self.sq()["worker_acknowledged_at"])

    def test_role_gates_on_every_step(self):
        steps = {"acknowledge": ("inspector", "shop_student", "cfi", "flight_student", "no_roles"),
                 "assign": ("inspector", "shop_student", "cfi", "flight_student", "no_roles"),
                 "repair": ("inspector", "shop_student", "cfi", "flight_student", "no_roles"),
                 "repair_confirm": ("tech", "shop_student", "cfi", "flight_student", "no_roles")}
        for step, roles in steps.items():
            for role in roles:
                self.login(role)
                before = self.sq()
                self.post("quick", step, assigned_to=str(self.users["tech"]["id"]))
                self.assertEqual(self.sq(), before, (step, role))

    def test_missing_or_bad_kind(self):
        self.login("shop_admin")
        for step in ("acknowledge", "assign", "worker_ack", "repair", "repair_confirm"):
            r = self.client.post(f"/squawks/bogus/{self.quick}/{step}")
            self.assertEqual(r.status_code, 404, step)
            r = self.client.post(f"/squawks/quick/9999/{step}")
            self.assertEqual(r.status_code, 302, step)
            r = self.client.post(f"/squawks/flight/9999/{step}")
            self.assertEqual(r.status_code, 302, step)

    def test_flight_without_squawk_flag_is_not_a_squawk(self):
        self.exec("UPDATE flights SET squawk=0 WHERE id=?", (self.flight,))
        self.login("shop_admin")
        self.post("flight", "acknowledge")
        self.assertIsNone(self.sq("flight")["acknowledged_at"])

    def test_can_only_assign_to_an_active_shop_worker(self):
        self.login("shop_admin")
        bad = [str(self.users["flight_student"]["id"]), str(self.users["cfi"]["id"]), "99999"]
        self.exec("UPDATE users SET active=0 WHERE id=?", (self.users["inspector"]["id"],))
        bad.append(str(self.users["inspector"]["id"]))
        for who in bad:
            self.post("quick", "assign", assigned_to=who)
            self.assertIsNone(self.sq()["assigned_to"], who)
            self.post("flight", "acknowledge", assigned_to=who)
            self.assertIsNone(self.sq("flight")["assigned_to"], who)

    def test_inspector_cannot_confirm_a_repair_nobody_marked_done(self):
        self.login("inspector")
        self.post("quick", "repair_confirm")
        self.post("flight", "repair_confirm")
        self.assertIsNone(self.sq()["repaired_at"])
        self.assertIsNone(self.sq("flight")["repaired_at"])

    def test_inspector_can_sign_off_own_repair_in_one_step(self):
        self.login("inspector")
        self.post("quick", "repair_confirm", action="self_repair")
        self.assertTrue(self.sq()["repaired_at"])
        self.assertEqual(self.sq()["repaired_by"], self.users["inspector"]["name"])

    def test_second_confirm_does_not_rewrite_who_signed_off(self):
        self.login("tech")
        self.post("quick", "repair")
        self.login("inspector")
        self.post("quick", "repair_confirm")
        first = self.sq()
        self.login("master")
        self.post("quick", "repair_confirm")
        self.assertEqual(self.sq()["repaired_by"], first["repaired_by"])
        self.assertEqual(self.sq()["repaired_at"], first["repaired_at"])

    @open_finding("qa-squawk-repair-after-done")
    def test_mark_repaired_on_a_finished_squawk_is_ignored(self):
        self.login("tech")
        self.post("quick", "repair")
        self.login("inspector")
        self.post("quick", "repair_confirm")
        self.login("tech")
        self.post("quick", "repair")
        s = self.sq()
        self.assertTrue(s["repaired_at"])
        self.assertIsNone(s["repair_confirm_requested_at"],
                          "a repaired squawk shouldn't go back to waiting for an inspector")

    def test_repair_toggle_undoes_the_request(self):
        """Documented behavior: pressing Mark Repaired again while waiting
        for the inspector takes the request back."""
        self.login("tech")
        self.post("quick", "repair")
        self.post("quick", "repair")
        self.assertIsNone(self.sq()["repair_confirm_requested_at"])


class FixOnJobTest(SquawkBase):
    def test_fix_on_job_claims_and_adds_sub_area(self):
        pid = self.make_project(asset_id=self.asset)
        self.login("tech")
        r = self.client.post(f"/projects/{pid}/squawks/quick/{self.quick}/fix_on_job")
        self.assertEqual(r.status_code, 302)
        s = self.sq()
        self.assertEqual(s["assigned_to"], self.users["tech"]["id"])
        self.assertTrue(s["worker_acknowledged_at"])
        self.assertIsNotNone(self.q1("SELECT id FROM project_sections WHERE project_id=?", (pid,)))

    def test_fix_on_job_for_another_planes_squawk_is_refused(self):
        other = self.make_asset("N99999")
        pid = self.make_project(asset_id=other)
        self.login("tech")
        self.client.post(f"/projects/{pid}/squawks/quick/{self.quick}/fix_on_job")
        self.assertIsNone(self.sq()["assigned_to"])
        self.assertEqual(self.client.post(f"/projects/9999/squawks/quick/{self.quick}/fix_on_job").status_code, 404)
