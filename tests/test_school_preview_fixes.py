"""Preview build, "school" part: one small check per bug fix in Fly with
Kate!'s people / money / planes / learning pages (SCHOOL-02, 07, 12, 15,
17, 19, 23, 25, 26, 27, 31, 36, 37, 38, 40, 41, 42, 44). SCHOOL-01 has its
own file (test_flight_payments.py)."""
from datetime import date

from harness import OpsHubTestCase, seed_row
import db


class CfiAndStudentFormsTest(OpsHubTestCase):
    def test_new_cfi_saves_pay_rate_and_logs_it(self):  # SCHOOL-02
        c = self.login("master")
        c.post("/flight/cfis/new", data={"name": "Pat Pilot", "username": "patp", "password": "Welcome#1",
                                         "rate_per_hour": "75", "pay_rate_per_hour": "40"})
        row = self.q1("SELECT id, pay_rate_per_hour FROM cfis WHERE username = 'patp'")
        self.assertEqual(row["pay_rate_per_hour"], 40.0)
        log = self.q1("SELECT new_value FROM field_change_log WHERE entity_type = 'cfi' AND entity_id = ? AND field_name = 'pay_rate_per_hour'",
                      (row["id"],))
        self.assertEqual(log["new_value"], "40.0")
        # SCHOOL-43: the starting password isn't kept readable on file.
        self.assertIsNone(self.q1("SELECT password_plain FROM users WHERE username = 'patp'")["password_plain"])

    def test_new_student_keeps_typed_values_on_a_taken_username(self):  # SCHOOL-15
        c = self.login("master")
        r = c.post("/flight/students/new", data={"name": "Sam Student", "username": "cfi", "password": "x",
                                                 "email": "sam@example.com", "phone": "555-1212",
                                                 "medical_class": "third", "rate_override": "90"})
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("already taken", body)
        self.assertIn('value="Sam Student"', body)
        self.assertIn('value="sam@example.com"', body)
        self.assertIn('name="username" class="form-control is-invalid"', body)
        self.assertIn('value="third" selected', body)

    def test_cfi_cannot_change_rates_but_admin_can(self):  # SCHOOL-12
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE students SET plane_rate_override = 120 WHERE id = ?", (sid,))
        html = self.login("cfi").get(f"/flight/students/{sid}/edit").get_data(as_text=True)
        self.assertNotIn('name="plane_rate_override"', html)
        self.assertIn("$120.00/hr", html)
        self.login("cfi").post(f"/flight/students/{sid}/edit", data={"name": "Flight Student", "email": "a@b.c", "phone": "1",
                                                                     "plane_rate_override": "5", "active": "on"})
        self.assertEqual(self.q1("SELECT plane_rate_override FROM students WHERE id = ?", (sid,))["plane_rate_override"], 120.0)
        self.login("master").post(f"/flight/students/{sid}/edit", data={"name": "Flight Student", "email": "a@b.c", "phone": "1",
                                                                        "plane_rate_override": "5", "active": "on"})
        self.assertEqual(self.q1("SELECT plane_rate_override FROM students WHERE id = ?", (sid,))["plane_rate_override"], 5.0)

    def test_edit_student_error_keeps_the_account_card(self):  # SCHOOL-47
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        r = self.login("master").post(f"/flight/students/{sid}/edit", data={"name": "", "email": "a@b.c", "phone": "1"})
        body = r.get_data(as_text=True)
        self.assertIn("Name is required", body)
        self.assertIn('id="account"', body)
        self.assertIn("Lessons &amp; Landings", body)
        self.assertIn('value="a@b.c"', body)

    def test_students_list_defaults_to_active_and_counts_only_shown(self):  # SCHOOL-42
        self.exec("UPDATE students SET active = 0 WHERE user_id = ?", (self.users["flight_student"]["id"],))
        active = self.q1("SELECT COUNT(*) c FROM students WHERE active = 1")["c"]
        total = self.q1("SELECT COUNT(*) c FROM students")["c"]
        self.assertLess(active, total)
        c = self.login("master")
        self.assertIn(f'id="students-count" data-change="SCHOOL-42">{active}<', c.get("/flight/students").get_data(as_text=True))
        self.assertIn(f'id="students-count" data-change="SCHOOL-42">{total}<', c.get("/flight/students?show=all").get_data(as_text=True))

    def test_master_admin_without_cfi_profile_still_reaches_students(self):  # SCHOOL-44
        self.exec("DELETE FROM cfis WHERE user_id = ?", (self.users["master"]["id"],))
        c = self.login("master")
        r = c.get("/flight/students")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Log in as a CFI", r.get_data(as_text=True))
        self.assertEqual(c.get("/flight/planes").status_code, 200)


class PlanesTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N55GR")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))

    def test_ground_from_planes_row_without_a_reason_and_cfi_sees_badge(self):  # SCHOOL-25 / SCHOOL-26
        c = self.login("master")
        c.post(f"/flight/planes/{self.plane}/ground", data={"reason": ""})
        self.assertEqual(self.q1("SELECT grounded_reason FROM assets WHERE id = ?", (self.plane,))["grounded_reason"], "grounded by an admin")
        html = self.login("cfi").get("/flight/planes").get_data(as_text=True)
        self.assertIn("Down for maintenance", html)
        self.assertNotIn("100-hr", html)  # hours-left column is admin-only
        html = c.get("/flight/planes").get_data(as_text=True)
        self.assertIn("Return to service", html)
        c.post(f"/flight/planes/{self.plane}/return-to-service")
        self.assertIsNone(self.q1("SELECT grounded_at FROM assets WHERE id = ?", (self.plane,))["grounded_at"])

    def test_own_plane_form_saves_and_returns_to_planes(self):  # SCHOOL-27
        c = self.login("master")
        self.assertIn(f"/flight/planes/{self.plane}/edit", c.get("/flight/planes").get_data(as_text=True))
        r = c.post(f"/flight/planes/{self.plane}/edit", data={"tag": "N55GR", "name": "", "make": "Cessna", "model": "172S",
                                                             "is_flight_asset": "on"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/flight/planes"))
        row = self.q1("SELECT make, model, is_flight_asset FROM assets WHERE id = ?", (self.plane,))
        self.assertEqual((row["make"], row["model"], row["is_flight_asset"]), ("Cessna", "172S", 1))


class ReportsAndSquawksTest(OpsHubTestCase):
    def test_plane_issue_links_its_squawk_and_resolves_when_repaired(self):  # SCHOOL-31
        aid = self.make_asset("N1FK")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (aid,))
        c = self.login("cfi")
        c.post("/flight/reports", data={"category": "plane_issue", "asset_id": aid, "notes": "Flat tire"})
        rep = self.q1("SELECT id, squawk_id FROM flight_reports")
        self.assertIsNotNone(rep["squawk_id"])
        html = c.get("/flight/alerts").get_data(as_text=True)
        self.assertIn(f"Squawk #{rep['squawk_id']}", html)
        self.assertIn("Also close the squawk in Maintenance", html)
        self.exec("UPDATE plane_squawks SET repaired_at = ? WHERE id = ?", (db.now_iso(), rep["squawk_id"]))
        c.get("/flight/alerts")
        self.assertEqual(self.q1("SELECT resolved_by FROM flight_reports WHERE id = ?", (rep["id"],))["resolved_by"], "Squawk repaired")

    def test_resolve_can_close_the_squawk_too(self):  # SCHOOL-31
        aid = self.make_asset("N1FK")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (aid,))
        c = self.login("cfi")
        c.post("/flight/reports", data={"category": "plane_issue", "asset_id": aid, "notes": "Flat tire"})
        rep = self.q1("SELECT id, squawk_id FROM flight_reports")
        c.post(f"/flight/reports/{rep['id']}/resolve", data={"close_squawk": "1"})
        self.assertIsNotNone(self.q1("SELECT repaired_at FROM plane_squawks WHERE id = ?", (rep["squawk_id"],))["repaired_at"])


class StatsAndPayTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.exec("UPDATE students SET plane_rate_override = 100.5 WHERE id = ?", (self.student_id,))
        conn = db.get_db()
        self.fid = seed_row(conn, "flights", asset_id=self.plane, student_id=self.student_id, cfi_id=self.cfi_id,
                            flight_date=date.today().isoformat(), hobbs_start=100.0, hobbs_end=101.0,
                            started_at=None, ended_at=db.now_iso(), solo=0, paid=0)
        conn.commit()
        conn.close()

    def test_stats_shows_cents_us_dates_and_hours_paid(self):  # SCHOOL-17 / SCHOOL-19
        html = self.login("master").get("/flight/stats?range=month").get_data(as_text=True)
        self.assertIn("Hours Paid", html)
        self.assertIn("on flights marked paid", html)
        self.assertIn("$100.50", html)
        self.assertNotIn("$100<", html)
        self.assertNotIn("p[2] + '-' + p[1] + '-' + p[0]", html)
        self.assertIn('class="btn-check" name="plane_id"', html)  # SCHOOL-18 chips
        self.assertNotIn("ctrl/cmd-click", html)

    def test_cfi_pay_period_can_be_marked_paid(self):  # SCHOOL-23
        self.exec("UPDATE cfis SET pay_rate_per_hour = NULL WHERE id = ?", (self.cfi_id,))
        c = self.login("master")
        html = c.get(f"/flight/cfis/{self.cfi_id}/pay").get_data(as_text=True)
        self.assertIn("No pay rate set - ask an admin", html)
        self.exec("UPDATE cfis SET pay_rate_per_hour = 30 WHERE id = ?", (self.cfi_id,))
        html = c.get(f"/flight/cfis/{self.cfi_id}/pay?range=month").get_data(as_text=True)
        self.assertIn("Mark period paid", html)
        today = date.today().isoformat()
        r = c.post(f"/flight/cfis/{self.cfi_id}/pay/mark-paid", data={"start": today, "end": today, "note": "Check 77"},
                   follow_redirects=True)
        body = r.get_data(as_text=True)
        self.assertIn("Marked 1 flight", body)
        self.assertIsNotNone(self.q1("SELECT cfi_paid_at FROM flights WHERE id = ?", (self.fid,))["cfi_paid_at"])
        self.assertIn("Check 77", body)
        self.assertIn("Paid (1 flight", body)
        # A plain CFI can see their own pay but not mark it paid.
        r = self.login("cfi").post(f"/flight/cfis/{self.cfi_id}/pay/mark-paid", data={"start": today, "end": today})
        self.assertEqual(r.status_code, 302)


class GroundSchoolTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.exec("UPDATE users SET groundschool_access = 1 WHERE username IN ('flight_student', 'cfi')")
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        conn = db.get_db()
        self.rating_id = conn.execute("INSERT INTO acs_ratings (name, slug, uploaded_at, status) VALUES (?,?,?,?)",
                                      ("Private Pilot", "ppl", db.now_iso(), "parsed")).lastrowid
        area_id = conn.execute("INSERT INTO acs_areas (rating_id, code, title, order_index) VALUES (?,?,?,?)",
                               (self.rating_id, "I", "Preflight Preparation", 1)).lastrowid
        self.task_id = conn.execute("INSERT INTO acs_tasks (area_id, code, title, acs_references, order_index) VALUES (?,?,?,?,?)",
                                    (area_id, "A", "Pilot Qualifications", "14 CFR 61.83", 1)).lastrowid
        self.el1 = conn.execute("INSERT INTO acs_task_elements (task_id, kind, code, text, order_index) VALUES (?,?,?,?,?)",
                                (self.task_id, "knowledge", "PA.I.A.K1", "Certification requirements", 1)).lastrowid
        self.el2 = conn.execute("INSERT INTO acs_task_elements (task_id, kind, code, text, order_index) VALUES (?,?,?,?,?)",
                                (self.task_id, "knowledge", "PA.I.A.K2", "Privileges and limitations", 2)).lastrowid
        conn.commit()
        conn.close()

    def test_text_only_element_self_completes_on_open_and_cfi_can_verify_anyway(self):  # SCHOOL-07
        s = self.login("flight_student")
        s.get(f"/flight/groundschool/element/{self.el1}")
        row = self.q1("SELECT self_completed_at FROM acs_element_completion WHERE student_id = ? AND element_id = ?",
                      (self.student_id, self.el1))
        self.assertIsNotNone(row["self_completed_at"])
        c = self.login("cfi")
        html = c.get(f"/flight/groundschool/element/{self.el2}?student_id={self.student_id}").get_data(as_text=True)
        self.assertIn("Verify Complete", html)  # even though the student never opened el2
        c.post(f"/flight/groundschool/element/{self.el2}/verify", data={"student_id": self.student_id})
        html = c.get(f"/flight/groundschool/{self.rating_id}").get_data(as_text=True)
        self.assertIn("1 / 2 &middot; 50%", html)  # verification is what counts (picker remembered from the element page)

    def test_task_signoff_verifies_every_element(self):  # SCHOOL-41
        c = self.login("cfi")
        c.post(f"/flight/groundschool/task/{self.task_id}/signoff", data={"student_id": self.student_id, "notes": "Great job"},
               follow_redirects=True)
        n = self.q1("SELECT COUNT(*) c FROM acs_element_completion WHERE student_id = ? AND cfi_verified_at IS NOT NULL",
                    (self.student_id,))["c"]
        self.assertEqual(n, 2)
        html = c.get(f"/flight/groundschool/task/{self.task_id}").get_data(as_text=True)
        self.assertIn("2 / 2 elements complete", html)
        self.assertIn("Great job", html)

    def test_one_picker_remembered_across_pages(self):  # SCHOOL-08
        c = self.login("cfi")
        c.get(f"/flight/groundschool?student_id={self.student_id}")
        html = c.get(f"/flight/groundschool/task/{self.task_id}").get_data(as_text=True)
        self.assertEqual(html.count("Choose a student..."), 1)
        self.assertNotIn("Reading Review - Student", html)
        self.assertIn(f'<option value="{self.student_id}" selected>', html)
        self.assertIn(f"/flight/groundschool/element/{self.el1}?student_id={self.student_id}", html)
        s = self.login("flight_student")
        self.assertNotIn("Choose a student...", s.get(f"/flight/groundschool/task/{self.task_id}").get_data(as_text=True))

    def test_missing_rating_redirects_with_a_message(self):  # SCHOOL-40
        r = self.login("cfi").get("/flight/groundschool/task/99999", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("no longer here", r.get_data(as_text=True))

    def test_ground_school_only_account_sees_no_board_or_logbook_tabs(self):  # SCHOOL-06
        html = self.login("flight_student").get("/flight/groundschool").get_data(as_text=True)
        self.assertNotIn(">Board<", html)
        self.assertNotIn(">Logbook", html)
        self.assertNotIn("How points work", html)
        self.assertIn("Resources", html)


class AcademyTest(OpsHubTestCase):
    def test_logged_out_logbook_link_goes_to_sign_in_and_back(self):  # SCHOOL-36
        r = self.client.get("/academy/logbook", follow_redirects=True)
        body = r.get_data(as_text=True)
        self.assertIn("Please sign in", body)
        self.assertNotIn("You don&#39;t have access to Flight Academy", body)
        r = self.client.post("/", data={"username": "master", "password": "test-pass-123", "remember": "on"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/academy/logbook"))

    def test_logbook_goal_links_carry_the_viewed_student(self):  # SCHOOL-34 / SCHOOL-35
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE students SET pilot_certificate = 'student' WHERE id = ?", (sid,))
        html = self.login("master").get(f"/academy/logbook?student_id={sid}").get_data(as_text=True)
        self.assertIn(f"student_id={sid}", html.split("See full requirements")[0][-200:] + html.split("Your Next Goal")[0][-200:])
        self.assertIn("Totalizer", html)
        self.assertNotIn("Totalizar", html)
        html = self.login("master").get(f"/academy/totalizer?student_id={sid}").get_data(as_text=True)
        self.assertIn('data-change="SCHOOL-35"><i class="bi bi-calculator"></i> Totalizer', html)
        self.assertIn("nav-link active", html.split("Totalizer</a>")[0][-200:])

    def test_academy_landing_entry_writes_a_manual_landing(self):  # SCHOOL-37
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE users SET academy_access = 1 WHERE username = 'flight_student'")
        s = self.login("flight_student")
        s.post("/academy/entry", data={"kind": "night_landings", "value": "3", "entry_date": "2026-09-30"})
        ml = self.q1("SELECT * FROM manual_landings WHERE student_id = ?", (sid,))
        self.assertEqual((ml["day_landings"], ml["night_landings"], ml["landing_date"]), (0, 3, "2026-09-30"))
        self.assertIsNotNone(ml["academy_entry_id"])
        html = self.login("master").get(f"/flight/students/{sid}/edit").get_data(as_text=True)
        self.assertIn("(logged by student)", html)
        s.post(f"/academy/entry/{ml['academy_entry_id']}/delete")
        self.assertIsNone(self.q1("SELECT id FROM manual_landings WHERE student_id = ?", (sid,)))

    def test_board_picker_filters_entries_for_staff(self):  # SCHOOL-38
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        c = self.login("master")
        c.post("/academy/entry", data={"student_id": sid, "kind": "landings", "value": "2"})
        html = c.get(f"/academy?student_id={sid}").get_data(as_text=True)
        self.assertIn("Whole school", html)
        self.assertIn(f'<option value="{sid}" selected>', html)
        self.assertIn("last 25 entries", html)
