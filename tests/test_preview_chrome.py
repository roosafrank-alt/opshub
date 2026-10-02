"""Preview build, chrome part: the shared header, the one login page, the
Switch role chips as the only look-as-someone feature, My Account rules,
the owner portal and the admin page fixes. One small test per change."""
from unittest import mock

from harness import OpsHubTestCase, PASSWORD, seed_everything
import db
import notify


class SharedHeaderTest(OpsHubTestCase):
    HEADERS = (("master", "/shop"), ("master", "/flight/dashboard"), ("master", "/admin"),
               ("master", "/academy"), ("master", "/account"), ("master", "/customers"))

    def test_every_header_has_back_programs_and_the_shared_account_menu(self):
        """SEAM-1 / SEAM-2 / SEAM-7 / SEAM-17: same pieces in the same place."""
        for role, url in self.HEADERS:
            body = self.login(role).get(url).get_data(as_text=True)
            self.assertIn('id="opshub-back-btn"', body, url)
            self.assertIn('id="programs-menu-toggle"', body, url)
            self.assertIn("All programs", body, url)
            self.assertIn('id="user-menu-toggle-mobile"', body, url)
            self.assertIn('id="user-menu-toggle-desktop"', body, url)
            self.assertIn("Log out of OpsHub?", body, url)

    def test_programs_button_lists_only_what_the_account_can_open(self):
        """SEAM-12."""
        body = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        # A lone CFI has one program - no Programs button at all.
        self.assertNotIn('id="programs-menu-toggle"', body)
        body = self.login("shop_admin").get("/shop").get_data(as_text=True)
        self.assertIn('id="programs-menu-toggle"', body)
        self.assertIn('href="/flight/"', body)
        self.assertIn('href="/customers"', body)
        self.assertNotIn('href="/admin"', body)
        self.assertNotIn('href="/academy"', body)

    def test_owner_portal_has_back_account_menu_and_the_shared_stylesheet(self):
        """SEAM-6 / SEAM-7 / SEAM-11 / SEAM-18."""
        body = self.login("customer").get("/portal/").get_data(as_text=True)
        self.assertIn('id="opshub-back-btn"', body)
        self.assertIn("/portal/account", body)
        self.assertIn("css/style.css", body)
        self.assertIn("owner-nav", body)
        self.assertIn("manifest-windsaloft.json", body)  # SEAM-13

    def test_academy_header_is_branded_flight_academy(self):
        """HUB-21 / SEAM-10 / SEAM-13."""
        body = self.login("master").get("/academy").get_data(as_text=True)
        self.assertIn("bi-mortarboard-fill", body)
        self.assertIn('<span class="opshub-hdr-name">Flight Academy</span>', body)
        self.assertNotIn("bi-house-door", body)
        self.assertIn("manifest-flywithkate.json", body)
        self.assertIn('class="academy-tabs-row"', body)

    def test_customers_pages_render_inside_winds_aloft(self):
        """SEAM-9: the Winds Aloft header, not a plum My Aircraft Admin bar."""
        body = self.login("master").get("/customers").get_data(as_text=True)
        self.assertIn("bg-dark", body)
        self.assertIn('<span class="opshub-hdr-name">Winds Aloft</span>', body)
        self.assertNotIn("My Aircraft Admin", body)
        picker = self.login("master").get("/").get_data(as_text=True)
        self.assertIn(">Customers<", picker)

    def test_admin_and_account_pages_declare_a_real_identity(self):
        """SEAM-13: never a neutral icon."""
        self.assertIn("manifest-windsaloft.json", self.login("master").get("/admin").get_data(as_text=True))
        self.assertIn("manifest-flywithkate.json", self.login("cfi").get("/account?from=flight").get_data(as_text=True))
        self.assertIn("manifest-windsaloft.json", self.login("tech").get("/account?from=shop").get_data(as_text=True))

    def test_picker_has_the_shared_account_menu(self):
        """HUB-45."""
        body = self.login("master").get("/").get_data(as_text=True)
        self.assertIn('id="user-menu-toggle-picker"', body)
        self.assertIn("Signed in as", body)


class OneLoginTest(OpsHubTestCase):
    def test_login_page_is_opshub_with_both_logos(self):
        """SEAM-4."""
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("Username or Email", body)
        self.assertIn("icons/windsaloft", body)
        self.assertIn("icons/flywithkate", body)
        self.assertNotIn("Fly with Kate! Aviation Services", body)

    def test_old_login_addresses_redirect_to_the_one_login(self):
        """SEAM-4 / HUB-41 / HUB-42: /portal/login is retired."""
        for url in ("/portal/login", "/flight/login", "/login"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertEqual(r.headers["Location"].split("?")[0], "/", url)

    def test_bounce_returns_to_the_page_you_wanted_with_one_sentence(self):
        """SEAM-4 / SEAM-16."""
        r = self.client.get("/parts")
        self.assertEqual(r.headers["Location"], "/?next=/parts")
        html = self.client.get("/parts", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Please log in to continue.", html)
        self.assertIn('name="next" value="/parts"', html)
        # The portal says the same sentence and comes back the same way.
        r = self.client.get("/portal/")
        self.assertEqual(r.headers["Location"], "/?next=/portal/")
        r = self.client.post("/", data={"username": "tech", "password": PASSWORD, "next": "/parts"})
        self.assertEqual(r.headers["Location"], "/parts")

    def test_next_never_leaves_the_site(self):
        r = self.client.post("/", data={"username": "tech", "password": PASSWORD, "next": "https://evil.example/"})
        self.assertNotIn("evil", r.headers["Location"])

    def test_one_logout_everywhere(self):
        """SEAM-5: the portal logout says Logged out. and lands on the login page."""
        c = self.login("customer")
        r = c.get("/portal/logout")
        self.assertEqual(r.headers["Location"], "/")
        self.assertIn("Logged out.", c.get("/").get_data(as_text=True))


class SwitchRoleOnlyTest(OpsHubTestCase):
    def test_view_as_someone_and_owner_preview_are_gone(self):
        """HUB-03 (Frank): the Switch role chips are the only look-as feature."""
        c = self.login("master")
        self.assertEqual(c.get("/view-as/people").status_code, 404)
        self.assertEqual(c.post(f"/view-as/person/{self.users['tech']['id']}").status_code, 404)
        self.assertEqual(c.post(f"/view-as/owner/{self.customer_id}").status_code, 404)
        html = c.get("/shop").get_data(as_text=True)
        self.assertNotIn("View as someone", html)
        self.assertIn("Switch role:", html)
        self.assertNotIn("View as", c.get("/customers").get_data(as_text=True))

    def test_banner_says_switched_role(self):
        c = self.login("master")
        c.post("/view-as/shop/tech")
        html = c.get("/shop").get_data(as_text=True)
        self.assertIn("Switched role: Tech", html)
        self.assertIn("Back to my normal view", html)


class MasterAdminFlightProfileTest(OpsHubTestCase):
    def test_master_admin_without_a_flight_role_gets_a_hidden_profile(self):
        """HUB-01."""
        uid = self.exec("INSERT INTO users (name, username, password_hash, is_master_admin, active, created_at) "
                        "VALUES ('Boss', 'boss', ?, 1, 1, ?)", (self.users["master"]["password_hash"], db.now_iso()))
        c = self.app.test_client()
        r = c.post("/", data={"username": "boss", "password": PASSWORD})
        self.assertEqual(r.status_code, 302)
        cfi = self.q1("SELECT * FROM cfis WHERE user_id = ?", (uid,))
        self.assertIsNotNone(cfi)
        self.assertEqual(cfi["is_station"], 1)
        with c.session_transaction() as s:
            self.assertEqual(s["cfi_id"], cfi["id"])
        self.assertEqual(c.get("/flight/dashboard").status_code, 200)


class PasswordRulesTest(OpsHubTestCase):
    def test_my_account_needs_eight_characters(self):
        """HUB-27."""
        c = self.login("tech")
        before = self.q1("SELECT password_hash FROM users WHERE id = ?", (self.users["tech"]["id"],))["password_hash"]
        r = c.post("/account", data={"name": "Tech", "password": "short", "password_confirm": "short"})
        self.assertIn("at least 8 characters", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT password_hash FROM users WHERE id = ?", (self.users["tech"]["id"],))["password_hash"], before)
        c.post("/account", data={"name": "Tech", "password": "longenough1", "password_confirm": "longenough1"})
        self.assertNotEqual(self.q1("SELECT password_hash FROM users WHERE id = ?", (self.users["tech"]["id"],))["password_hash"], before)
        html = c.get("/account").get_data(as_text=True)
        self.assertIn('type="password" name="password"', html)
        self.assertIn("Show", html)

    def test_with_show_on_there_is_no_confirm_box(self):
        c = self.login("tech")
        r = c.post("/account", data={"name": "Tech", "password": "longenough1", "password_shown": "1"})
        self.assertEqual(r.status_code, 302)

    def test_admin_set_starting_password_is_exempt(self):
        c = self.login("master")
        r = c.post("/admin/users/new", data={"name": "Pat", "username": "pat", "password": "pw1", "shop_roles": ["tech"]})
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(self.q1("SELECT id FROM users WHERE username = 'pat'"))

    def test_owner_my_account_page(self):
        """SEAM-7: owners change their own details and password."""
        c = self.login("customer")
        self.assertEqual(c.get("/portal/account").status_code, 200)
        r = c.post("/portal/account", data={"name": "Owner Customer", "email": "owner@example.com", "phone": "555",
                                            "password": "tiny", "password_confirm": "tiny"})
        self.assertIn("at least 8 characters", r.get_data(as_text=True))
        r = c.post("/portal/account", data={"name": "Owner C.", "email": "owner@example.com", "phone": "555",
                                            "password": "newpass123", "password_confirm": "newpass123"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM customers WHERE id = ?", (self.customer_id,))
        self.assertEqual((row["name"], row["phone"], row["password_plain"]), ("Owner C.", "555", "newpass123"))


class CustomerLoginDetailsTest(OpsHubTestCase):
    def test_default_starting_password(self):
        """JOBS-31 (Frank): Jdoe#1."""
        import app as app_module
        self.assertEqual(app_module.default_customer_password("Jane Doe"), "Jdoe#1")
        self.assertEqual(app_module.default_customer_password("mary-ann van Berg"), "Mberg#1")
        c = self.login("master")
        r = c.post("/customers/new", data={"name": "Pat Jones", "email": "pat@example.com"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM customers WHERE email = 'pat@example.com'")
        self.assertEqual(row["password_plain"], "Pjones#1")
        self.assertIn(f"/customers/{row['id']}", r.headers["Location"])

    def test_send_login_details_records_when_it_was_sent(self):
        c = self.login("master")
        html = c.get(f"/customers/{self.customer_id}").get_data(as_text=True)
        self.assertIn("Send login details", html)
        self.assertNotIn("Login sent on", html)
        self.exec("UPDATE customers SET password_plain = 'Ocustomer#1' WHERE id = ?", (self.customer_id,))
        sent = []
        with mock.patch.object(notify, "send_email", lambda settings, to, subject, body, brand=None: (sent.append((to, body)) or (True, None))):
            r = c.post(f"/customers/{self.customer_id}/send-login")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(sent[0][0], "owner@example.com")
        self.assertIn("Ocustomer#1", sent[0][1])
        self.assertIsNotNone(self.q1("SELECT login_sent_at FROM customers WHERE id = ?", (self.customer_id,))["login_sent_at"])
        self.assertIn("Login sent on", c.get(f"/customers/{self.customer_id}").get_data(as_text=True))

    def test_send_login_that_fails_records_nothing(self):
        c = self.login("master")
        self.exec("UPDATE customers SET password_plain = 'x' WHERE id = ?", (self.customer_id,))
        c.post(f"/customers/{self.customer_id}/send-login")  # email/text not set up in tests
        self.assertIsNone(self.q1("SELECT login_sent_at FROM customers WHERE id = ?", (self.customer_id,))["login_sent_at"])


class OwnerPortalTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N42AB")
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, self.asset))

    def test_hobbs_line_uses_app_dates_and_drops_the_owner_prefix(self):
        """HUB-13."""
        c = self.login("customer")
        c.post(f"/portal/aircraft/{self.asset}/hours", data=dict(hobbs_hours="200.0"))
        html = c.get(f"/portal/aircraft/{self.asset}").get_data(as_text=True)
        self.assertIn("by Owner Customer", html)
        self.assertNotIn("Owner - Owner Customer", html)
        self.assertIn('data-hobbs="200.0"', html)  # HUB-14's lower-reading check

    def test_awaiting_appointment_says_please_confirm(self):
        """HUB-16."""
        self.make_project(name="Annual", asset_id=self.asset)
        self.exec("UPDATE projects SET scheduled_date = '2030-01-07' WHERE asset_id = ?", (self.asset,))
        html = self.login("customer").get(f"/portal/aircraft/{self.asset}").get_data(as_text=True)
        self.assertIn("Please confirm", html)
        self.assertNotIn("Awaiting Your Response", html)
        self.assertIn("1 to confirm", html)

    def test_job_costs_have_details_with_thousands_separators(self):
        """HUB-12."""
        pid = self.make_project(name="Annual", asset_id=self.asset)
        part = self.make_part(name="Big Part", unit_cost=1000)
        self.exec("INSERT INTO transactions (part_id, project_id, type, qty, performed_by, section, created_at) "
                  "VALUES (?, ?, 'out', 2, 'Frank', 'Engine', ?)", (part, pid, db.now_iso()))
        html = self.login("customer").get(f"/portal/aircraft/{self.asset}").get_data(as_text=True)
        self.assertIn("<summary", html)
        self.assertIn("Big Part", html)
        self.assertIn("$2,600.00", html)

    def test_approval_is_logged_on_the_job(self):
        """HUB-43 (Frank)."""
        pid = self.make_project(name="Annual", asset_id=self.asset)
        iid = self.exec("INSERT INTO found_items (project_id, asset_id, description, est_parts, est_labor_hours, est_labor_rate, est_total, status, created_by, created_at) "
                        "VALUES (?, ?, 'Cracked stack', 1000, 2, 125, 1250, 'waiting', 'Tech', ?)", (pid, self.asset, db.now_iso()))
        c = self.login("customer")
        self.assertIn("data-approve-amount=\"1,250.00\"", c.get(f"/portal/aircraft/{self.asset}").get_data(as_text=True))
        c.post(f"/portal/found-item/{iid}/decide", data=dict(decision="approve"))
        msg = self.q1("SELECT * FROM found_item_messages WHERE found_item_id = ?", (iid,))
        self.assertIn("Approved this work (about $1,250.00)", msg["body"])
        self.assertIsNotNone(self.q1("SELECT decided_at FROM found_items WHERE id = ?", (iid,))["decided_at"])


class AdminPagesTest(OpsHubTestCase):
    def test_edit_errors_keep_what_was_typed(self):
        """HUB-24."""
        c = self.login("master")
        tech = self.users["tech"]
        html = c.post(f"/admin/users/{tech['id']}/edit",
                      data={"name": "", "shop_roles": ["inspector"], "email": "typed@example.com", "active": "on",
                            "password": "newpw12345"}).get_data(as_text=True)
        self.assertIn("Name is required", html)
        self.assertIn('value="typed@example.com"', html)
        self.assertIn('value="inspector" id="shop-role-inspector" checked', html)
        self.assertIn('value="newpw12345"', html)
        self.assertIn("Pay", html)

    def test_accounts_list_has_no_passwords_and_stacks_on_phones(self):
        """HUB-25."""
        self.exec("UPDATE users SET password_plain = 'sekret-pw' WHERE id = ?", (self.users["tech"]["id"],))
        c = self.login("master")
        html = c.get("/admin/users").get_data(as_text=True)
        self.assertNotIn("sekret-pw", html)
        self.assertIn("table-stack", html)
        edit = c.get(f"/admin/users/{self.users['tech']['id']}/edit").get_data(as_text=True)
        self.assertIn("sekret-pw", edit)
        self.assertIn('id="current-pw-toggle"', edit)

    def test_resets_say_how_much_and_need_reset_typed(self):
        """HUB-29 / HUB-31."""
        self.make_project()
        self.make_project()
        c = self.login("master")
        r = c.post("/admin/reset/projects", follow_redirects=True)
        self.assertIn("Type RESET", r.get_data(as_text=True))
        self.assertEqual(len(self.q("SELECT id FROM projects")), 2)
        r = c.post("/admin/reset/projects", data={"confirm_text": "RESET"}, follow_redirects=True)
        self.assertIn("Deleted 2 projects", r.get_data(as_text=True))
        self.assertIn("starts again at 001", r.get_data(as_text=True))
        self.assertEqual(self.q("SELECT id FROM projects"), [])
        html = c.get("/admin/reset").get_data(as_text=True)
        self.assertIn('data-confirm-ok="Delete students"', html)
        self.assertIn("Winds Aloft", html.split("Clear Squawks")[0])

    def test_login_attempts_show_local_time_and_named_unlock(self):
        """HUB-32."""
        c = self.login("master")
        self.exec("INSERT INTO login_lockouts (account, fails, lockouts, locked_until) VALUES ('frank', 0, 1, '2999-10-02T14:03:00Z')")
        html = c.get("/admin/login-attempts").get_data(as_text=True)
        self.assertNotIn("UTC", html)
        self.assertNotIn("2999-10-02T14:03:00Z", html)
        r = c.post("/admin/login-attempts", data={"account": "frank"}, follow_redirects=True)
        self.assertIn("Unlocked frank - they can log in again now.", r.get_data(as_text=True))

    def test_system_actions_land_on_the_waiting_page(self):
        """HUB-30."""
        import app as app_module
        saved = app_module._run_delayed_command
        app_module._run_delayed_command = lambda args, delay=2.0: None
        try:
            c = self.login("master")
            r = c.post("/admin/system/restart_app")
            self.assertIn("/admin/system/wait?what=restart", r.headers["Location"])
            self.assertIn("reloads itself", c.get("/admin/system/wait?what=restart").get_data(as_text=True))
            self.assertIn("green light", c.get("/admin/system/wait?what=shutdown").get_data(as_text=True))
        finally:
            app_module._run_delayed_command = saved
        html = c.get("/admin/system").get_data(as_text=True)
        self.assertIn('data-confirm-title="Reboot the Pi?"', html)
        self.assertIn('data-confirm-ok="Shut down"', html)

    def test_payroll_messages_name_the_person(self):
        """HUB-36."""
        from datetime import date
        lab = self.exec("INSERT INTO laborers (name, code, rate, active, created_at, updated_at) VALUES ('Sam', 'LABOR-SAM', 20, 1, ?, ?)",
                        (db.now_iso(), db.now_iso()))
        today = date.today().isoformat()
        self.exec("INSERT INTO labor_sessions (laborer_id, project_id, started_at, ended_at, hours, cost, rate, created_at) "
                  "VALUES (?, NULL, ?, ?, 2, 40, 20, ?)", (lab, today + " 08:00:00", today + " 10:00:00", db.now_iso()))
        c = self.login("master")
        r = c.post("/payroll/mark-paid", data={"week": today, "person_type": "laborer", "person_id": lab}, follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("Marked Sam paid $40.00 for the week of", html)
        self.assertIn(f"/shop/pay?laborer_id={lab}", html)
        r = c.post("/payroll/unmark-paid", data={"week": today, "person_type": "laborer", "person_id": lab}, follow_redirects=True)
        self.assertIn("Marked Sam as not paid for the week of", r.get_data(as_text=True))

    def test_program_names_are_winds_aloft_and_fly_with_kate(self):
        """SEAM-3."""
        c = self.login("master")
        for url in ("/admin", "/admin/users", "/admin/reset", "/account"):
            html = c.get(url).get_data(as_text=True)
            self.assertNotIn("Flight School", html.replace("Reset All Flight School", ""), url)
            self.assertNotIn("Shop Inventory", html, url)
        html = c.get(f"/admin/users/{self.users['cfi']['id']}/edit").get_data(as_text=True)
        self.assertIn("Winds Aloft Roles", html)
        self.assertIn("Fly with Kate! Roles", html)
        self.assertNotIn("Maintenance Roles", html)

    def test_walkthrough_buttons_and_tours(self):
        """SEAM-19 / HUB-39."""
        html = self.login("master").get("/account").get_data(as_text=True)
        self.assertIn("Winds Aloft walkthrough", html)
        self.assertIn("Fly with Kate! walkthrough", html)
        self.assertIn("Flight Academy walkthrough", html)
        self.assertNotIn("Shop Inventory Tour", html)
        self.assertIn("Choose how you want reminders", html)
        self.assertNotIn("planned for later", html)
        self.assertIn("tour-academy-brand", self.login("master").get("/academy?tour=1").get_data(as_text=True))
        self.assertIn("tour-owner-brand", self.login("customer").get("/portal/?tour=1").get_data(as_text=True))
        # The tour-seen endpoints remember it.
        c = self.login("master")
        self.assertEqual(c.post("/tour/seen", data={"side": "academy"}).status_code, 204)
        self.assertEqual(self.q1("SELECT tour_seen_academy FROM users WHERE id = ?", (self.users["master"]["id"],))[0], 1)
        c = self.login("customer")
        self.assertEqual(c.post("/portal/tour/seen").status_code, 204)
        self.assertEqual(self.q1("SELECT tour_seen FROM customers WHERE id = ?", (self.customer_id,))[0], 1)

    def test_phone_alert_notes_and_test_hint(self):
        """HUB-33 / HUB-34."""
        self.assertIn("Phone alerts are for Fly with Kate! flying sessions only",
                      self.login("tech").get("/account").get_data(as_text=True))
        html = self.login("master").get("/admin/notifications").get_data(as_text=True)
        self.assertIn("save first after changing anything", html)
        self.assertIn("Fly with Kate! flying sessions only", html)
