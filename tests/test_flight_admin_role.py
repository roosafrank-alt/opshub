"""Shop Admin / Flight Admin: each admin acts only in their own program.

Shop Admin (stored shop_role 'admin') = Winds Aloft + Customers / My Aircraft.
Flight Admin (stored flight_role/flight_roles 'admin') = Fly with Kate! +
Flight Academy (Academy, Ground School, course grants). Master admin = both,
plus the Admin program and payroll/staff-account pages. Everything is checked
on the server by direct URL, not just by what a menu shows."""
from harness import OpsHubTestCase, PASSWORD
import db


def redirected(r):
    return r.status_code in (301, 302, 303)


class FlightAdminRoleTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        conn = db.get_db()
        self.rating = conn.execute("INSERT INTO acs_ratings (name, slug, uploaded_at, status) VALUES (?,?,?,?)",
                                   ("Private Pilot", "ppl", db.now_iso(), "parsed")).lastrowid
        conn.commit()
        conn.close()

    def ok(self, who, url):
        r = self.login(who).get(url)
        self.assertEqual(r.status_code, 200, f"{who} should open {url}")

    def refused(self, who, url):
        r = self.login(who).get(url)
        self.assertTrue(redirected(r), f"{who} must NOT open {url} (got {r.status_code})")

    # ----- labels ----------------------------------------------------------
    def test_account_form_labels_and_wording(self):
        body = self.login("master").get("/admin/users/new").get_data(as_text=True)
        self.assertIn(">Shop Admin <", body)
        self.assertIn('for="flight-role-admin">Flight Admin</label>', body)
        self.assertIn("Shop Admin:</strong> runs Winds Aloft and manages Customers and My Aircraft.", body)
        self.assertIn("Flight Admin:</strong> runs Fly with Kate! and Flight Academy.", body)
        self.assertIn("Master admin:</strong> can work in every program", body)
        # the bare "Admin" role label is gone from the pickers
        self.assertNotIn('for="shop-role-admin">Admin ', body)
        self.assertNotIn('for="flight-role-admin">Admin', body)

    def test_accounts_list_shows_new_names(self):
        self.exec("UPDATE users SET flight_role='admin', flight_roles='admin' WHERE username='cfi'")
        body = self.login("master").get("/admin/users").get_data(as_text=True)
        self.assertIn("Shop Admin</span>", body)
        self.assertIn("Flight Admin</span>", body)

    def test_new_account_form_starts_with_no_admin_role_ticked(self):
        c = self.login("master")
        for url in ("/admin/users/new", "/admin/users/new?shop_role=admin&flight_role=admin&flight_roles=admin"):
            body = c.get(url).get_data(as_text=True)
            for box in ('id="shop-role-admin"', 'id="flight-role-admin"'):
                i = body.index(box)
                self.assertNotIn("checked", body[i:body.index(">", i)])

    # ----- Flight Admin: allowed -------------------------------------------
    def test_flight_admin_opens_flight_pages(self):
        for url in ("/flight/dashboard", "/flight/planes", "/flight/students", "/flight/settings/school",
                    "/flight/schedule/new", "/flight/alerts", "/flight/planes/simulator/new"):
            self.ok("flight_admin", url)

    def test_flight_admin_has_hidden_non_bookable_profile(self):
        cfi = self.q1("SELECT * FROM cfis WHERE user_id = ?", (self.users["flight_admin"]["id"],))
        self.assertIsNotNone(cfi)
        self.assertEqual(cfi["is_station"], 1)
        self.assertEqual(cfi["rate_per_hour"], 0)
        c = self.login("flight_admin")
        c.get("/flight/dashboard")
        with c.session_transaction() as s:
            self.assertEqual(s["cfi_id"], cfi["id"])

    def test_flight_admin_real_login_works_without_any_profile_ahead(self):
        self.exec("DELETE FROM cfis WHERE user_id = ?", (self.users["flight_admin"]["id"],))
        c = self.app.test_client()
        r = c.post("/", data={"username": "flight_admin", "password": PASSWORD})
        self.assertTrue(redirected(r))
        self.assertEqual(c.get("/flight/dashboard").status_code, 200)
        self.assertIsNotNone(self.q1("SELECT 1 FROM cfis WHERE user_id = ? AND is_station = 1",
                                     (self.users["flight_admin"]["id"],)))

    def test_flight_admin_has_academy_and_ground_school_automatically(self):
        row = self.q1("SELECT academy_access, groundschool_access FROM users WHERE username='flight_admin'")
        self.assertEqual((row["academy_access"], row["groundschool_access"]), (0, 0))
        self.ok("flight_admin", "/academy")
        self.ok("flight_admin", "/flight/groundschool")
        self.ok("flight_admin", "/flight/groundschool/resources")

    def test_flight_admin_can_grant_courses(self):
        c = self.login("flight_admin")
        body = c.get(f"/flight/students/{self.sid}/edit").get_data(as_text=True)
        self.assertIn("Flight Academy courses", body)
        c.post(f"/flight/students/{self.sid}/course-grants", data={"rating_id": [self.rating]})
        self.assertEqual(self.q("SELECT COUNT(*) c FROM academy_course_grants")[0]["c"], 1)

    def test_flight_admin_can_post_school_settings(self):
        r = self.login("flight_admin").post("/flight/settings/school", data={})
        self.assertFalse(redirected(r) and "/flight/dashboard" in r.headers["Location"])

    # ----- Flight Admin: refused --------------------------------------------
    def test_flight_admin_cannot_open_winds_aloft_customers_or_admin(self):
        for url in ("/shop", "/customers", "/parts", "/admin", "/admin/users", "/admin/users/new", "/admin/system",
                    "/admin/notifications", "/payroll"):
            self.refused("flight_admin", url)

    def test_flight_admin_cannot_use_master_only_flight_pages(self):
        cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        for url in ("/flight/cfis", "/flight/cfis/new", f"/flight/cfis/{cfi_id}/edit", f"/flight/cfis/{cfi_id}/pay"):
            self.refused("flight_admin", url)
        r = self.login("flight_admin").post(f"/flight/cfis/{cfi_id}/pay/mark-paid",
                                            data={"start": "2026-01-01", "end": "2026-01-31"})
        self.assertTrue(redirected(r))

    def test_flight_admin_has_no_billing_unless_can_bill(self):
        self.refused("flight_admin", "/flight/billing")
        self.exec("UPDATE users SET can_bill=1 WHERE username='flight_admin'")
        self.ok("flight_admin", "/flight/billing")

    def test_flight_admin_cannot_change_accounts_or_roles(self):
        uid = self.users["cfi"]["id"]
        c = self.login("flight_admin")
        c.post(f"/admin/users/{uid}/edit", data={"name": "Cfi", "flight_roles": ["admin"], "shop_roles": ["admin"]})
        row = self.q1("SELECT flight_role, shop_role FROM users WHERE id=?", (uid,))
        self.assertEqual((row["flight_role"], row["shop_role"]), ("cfi", None))

    # ----- Shop Admin ------------------------------------------------------
    def test_shop_admin_keeps_winds_aloft_and_customers(self):
        self.ok("shop_admin", "/shop")
        self.ok("shop_admin", "/customers")
        self.ok("shop_admin", "/parts")

    def test_shop_admin_has_no_flight_or_academy_power(self):
        for url in ("/flight/dashboard", "/flight/planes", "/flight/students", "/flight/settings/school",
                    "/flight/cfis", "/academy", "/flight/groundschool", "/flight/groundschool/resources",
                    "/flight/billing", "/admin", "/admin/users"):
            self.refused("shop_admin", url)
        c = self.login("shop_admin")
        c.post(f"/flight/students/{self.sid}/course-grants", data={"rating_id": [self.rating]})
        c.post("/flight/groundschool/upload", data={})
        self.assertEqual(self.q("SELECT COUNT(*) c FROM academy_course_grants")[0]["c"], 0)
        self.assertIsNone(self.q1("SELECT 1 FROM cfis WHERE user_id = ?", (self.users["shop_admin"]["id"],)))

    def test_shop_admin_cannot_make_anyone_an_admin(self):
        uid = self.users["tech"]["id"]
        c = self.login("shop_admin")
        c.post(f"/admin/users/{uid}/edit", data={"name": "Tech", "shop_roles": ["admin"], "flight_roles": ["admin"]})
        c.post("/admin/users/new", data={"name": "N", "username": "newbie", "password": "pw12345", "flight_roles": ["admin"]})
        row = self.q1("SELECT shop_role, flight_role FROM users WHERE id=?", (uid,))
        self.assertEqual((row["shop_role"], row["flight_role"]), ("tech", None))
        self.assertIsNone(self.q1("SELECT 1 FROM users WHERE username='newbie'"))

    def test_cfi_cannot_make_anyone_an_admin(self):
        uid = self.users["flight_student"]["id"]
        c = self.login("cfi")
        c.post(f"/admin/users/{uid}/edit", data={"name": "S", "flight_roles": ["admin"]})
        c.post("/admin/users/new", data={"name": "N", "username": "newbie", "password": "pw12345", "shop_roles": ["admin"]})
        self.assertEqual(self.q1("SELECT flight_role FROM users WHERE id=?", (uid,))["flight_role"], "student")
        self.assertIsNone(self.q1("SELECT 1 FROM users WHERE username='newbie'"))

    def test_master_admin_can_set_both_admin_roles(self):
        c = self.login("master")
        c.post("/admin/users/new", data={"name": "Both", "username": "both", "password": "pw12345",
                                         "shop_roles": ["admin"], "flight_roles": ["admin"]})
        row = self.q1("SELECT shop_role, flight_role, flight_roles FROM users WHERE username='both'")
        self.assertEqual((row["shop_role"], row["flight_role"], row["flight_roles"]), ("admin", "admin", "admin"))
        self.assertTrue(self.q1("SELECT 1 FROM cfis WHERE user_id = (SELECT id FROM users WHERE username='both')"))

    # ----- CFI, student, master unchanged -----------------------------------
    def test_cfi_and_student_unchanged(self):
        for url in ("/flight/dashboard", "/flight/planes", "/flight/students"):
            self.ok("cfi", url)
        for url in ("/flight/settings/school", "/flight/cfis", "/academy", "/flight/groundschool", "/shop"):
            self.refused("cfi", url)
        self.ok("flight_student", "/flight/dashboard")
        for url in ("/flight/settings/school", "/flight/students", "/shop"):
            self.refused("flight_student", url)
        c = self.login("cfi")
        c.post(f"/flight/students/{self.sid}/course-grants", data={"rating_id": [self.rating]})
        self.assertEqual(self.q("SELECT COUNT(*) c FROM academy_course_grants")[0]["c"], 0)

    def test_master_admin_works_everywhere(self):
        for url in ("/shop", "/customers", "/flight/dashboard", "/flight/settings/school", "/flight/cfis",
                    "/academy", "/flight/groundschool", "/admin", "/admin/users", "/flight/billing"):
            self.ok("master", url)

    # ----- program tiles / picker ------------------------------------------
    def test_flight_admin_sees_fly_and_academy_tiles_only(self):
        c = self.login("flight_admin")
        body = c.get("/").get_data(as_text=True)
        self.assertIn("Choose a program", body)
        self.assertIn('href="/flight/"', body)
        self.assertIn('href="/academy"', body)
        for gone in ('href="/shop"', 'href="/customers"', 'href="/admin"', 'href="/portal/"'):
            self.assertNotIn(gone, body)
        import auth
        with self.app.test_request_context():
            from flask import session
            session.update(is_master_admin=False, shop_role=None, flight_role="admin", academy_access=True)
            self.assertEqual([p["key"] for p in auth.session_programs()], ["flight", "academy"])
            self.assertEqual(auth.account_program_count(), 2)
            self.assertIsNone(auth.single_program_endpoint())

    def test_shop_admin_programs_are_winds_aloft_and_customers(self):
        import auth
        with self.app.test_request_context():
            from flask import session
            session.update(is_master_admin=False, shop_role="admin", flight_role=None)
            self.assertEqual([p["key"] for p in auth.session_programs()], ["shop", "customers"])

    # ----- Switch role chips -----------------------------------------------
    def test_chips_for_flight_admin_who_is_also_a_cfi(self):
        c = self.login("flight_admin_cfi")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Flight Admin", body)
        self.assertIn(">CFI<", body)
        self.assertEqual(c.get("/flight/settings/school").status_code, 200)
        c.post("/view-as/flight/cfi")
        self.assertTrue(redirected(c.get("/flight/settings/school")))
        self.assertTrue(redirected(c.get("/academy")))
        c.post("/view-as/exit")
        self.assertEqual(c.get("/flight/settings/school").status_code, 200)

    def test_master_chips_name_shop_and_flight_admin(self):
        c = self.login("master")
        self.assertIn("Flight Admin", c.get("/flight/dashboard").get_data(as_text=True))
        self.assertIn("Shop Admin", c.get("/shop").get_data(as_text=True))

    def test_master_previewing_cfi_loses_flight_admin_power(self):
        c = self.login("master")
        c.post("/view-as/flight/cfi")
        self.assertTrue(redirected(c.get("/flight/settings/school")))

    # ----- Academy only by selection ---------------------------------------
    GS_ROUTES = ("/flight/groundschool", "/flight/groundschool/resources", "/flight/groundschool/1",
                 "/flight/groundschool/task/1", "/flight/groundschool/element/1")

    def assert_no_academy(self, c, username):
        row = self.q1("SELECT id, academy_access, groundschool_access FROM users WHERE username=?", (username,))
        self.assertEqual((row["academy_access"], row["groundschool_access"]), (0, 0))
        sid = self.q1("SELECT id FROM students WHERE user_id=?", (row["id"],))["id"]
        self.assertEqual(self.q("SELECT COUNT(*) c FROM academy_course_grants WHERE student_id=?", (sid,))[0]["c"], 0)
        self.assertNotIn('href="/academy"', c.get("/").get_data(as_text=True))
        self.assertTrue(redirected(c.get("/academy")))
        for url in self.GS_ROUTES:
            self.assertTrue(redirected(c.get(url)), url)
        self.assertTrue(redirected(c.post("/flight/groundschool/upload", data={})))

    def test_student_made_by_a_cfi_has_no_academy(self):
        c = self.login("cfi")
        r = c.post("/flight/students/new", data={"name": "New Kid", "username": "newkid", "password": "pw12345",
                                                 "email": "k@example.com", "phone": "555-1111"})
        self.assertTrue(redirected(r))
        k = self.app.test_client()
        k.post("/", data={"username": "newkid", "password": "pw12345"})
        self.assert_no_academy(k, "newkid")

    def test_student_made_by_a_flight_admin_has_no_academy(self):
        c = self.login("flight_admin")
        c.post("/flight/students/new", data={"name": "New Kid", "username": "newkid", "password": "pw12345",
                                              "email": "k@example.com", "phone": "555-1111"})
        k = self.app.test_client()
        k.post("/", data={"username": "newkid", "password": "pw12345"})
        self.assert_no_academy(k, "newkid")

    def test_student_made_in_accounts_has_no_academy(self):
        c = self.login("master")
        c.post("/admin/users/new", data={"name": "Acct Kid", "username": "acctkid", "password": "pw12345",
                                         "flight_roles": ["student"]})
        k = self.app.test_client()
        k.post("/", data={"username": "acctkid", "password": "pw12345"})
        self.assert_no_academy(k, "acctkid")

    def test_seeded_flight_student_has_no_academy(self):
        self.assert_no_academy(self.login("flight_student"), "flight_student")

    def test_ticking_academy_access_opens_it(self):
        self.exec("UPDATE users SET academy_access=1 WHERE username='flight_student'")
        self.ok("flight_student", "/academy")

    def test_new_accounts_have_neither_admin_role(self):
        c = self.login("master")
        c.post("/admin/users/new", data={"name": "Plain", "username": "plain", "password": "pw12345"})
        row = self.q1("SELECT shop_role, shop_roles, flight_role, flight_roles FROM users WHERE username='plain'")
        self.assertEqual(tuple(row), (None, None, None, None))
        # nothing in the migration promoted an existing account
        self.assertEqual(self.q("SELECT COUNT(*) c FROM users WHERE flight_role='admin' OR flight_roles LIKE '%admin%'")[0]["c"], 2)
