"""QA finding qa-session-keeps-old-access (Bug, high, area: Accounts):
deactivating an account or taking a role away in Admin > Accounts used to
only apply the next time that person logged in. Sessions last up to 180
days (PERMANENT_SESSION_LIFETIME, app.py), so anyone already signed in kept
their OLD access - a departed worker could keep scanning parts for months,
a demoted shop admin kept seeing costs, and a removed master admin kept
opening Admin > Accounts.

Fix: every login-gated decorator (auth.py and flight.py) now re-checks the
signed-in account's `users` row on every request (auth.refresh_or_expire_
session, called from auth.login_required/master_admin_required/
shop_role_required and flight.py's own login_required/admin_required/
billing_required/cfi_required, plus a matching check for the separate
customer-portal login). A deactivated (or deleted) account is signed out
with a flash message; a role/permission change is picked up immediately,
with no need to log out and back in."""
from harness import OpsHubTestCase


class SessionAccessRefreshedTest(OpsHubTestCase):
    # ----- deactivation: signed out on the very next request -------------
    def test_deactivated_account_is_signed_out_on_next_request(self):
        c = self.login("tech")
        # Still signed in and able to reach the shop dashboard before
        # anything changes.
        self.assertEqual(c.get("/shop").status_code, 200)

        self.exec("UPDATE users SET active = 0 WHERE id = ?", (self.users["tech"]["id"],))

        r = c.get("/shop", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("no longer active", body)
        # The session was actually cleared, not just refused this once.
        with c.session_transaction() as s:
            self.assertNotIn("user_id", s)
        # ...and stays signed out on a later request too.
        self.assertEqual(c.get("/shop").status_code, 302)

    def test_deleted_account_is_also_signed_out(self):
        """Same protection when the row itself is gone, not just flagged
        inactive (e.g. a hard delete), covered by the same "not row" check."""
        c = self.login("tech")
        self.exec("DELETE FROM users WHERE id = ?", (self.users["tech"]["id"],))
        r = c.get("/shop", follow_redirects=True)
        self.assertIn("no longer active", r.get_data(as_text=True))
        with c.session_transaction() as s:
            self.assertNotIn("user_id", s)

    def test_flight_school_login_required_also_signs_out_a_deactivated_account(self):
        """flight.py keeps its own login_required (gated on cfi_id/student_id
        rather than user_id), which needs the same refresh."""
        c = self.login("cfi")
        self.assertEqual(c.get("/flight/dashboard").status_code, 200)
        self.exec("UPDATE users SET active = 0 WHERE id = ?", (self.users["cfi"]["id"],))
        r = c.get("/flight/dashboard", follow_redirects=True)
        self.assertIn("no longer active", r.get_data(as_text=True))
        with c.session_transaction() as s:
            self.assertNotIn("cfi_id", s)

    def test_deactivated_customer_portal_account_is_signed_out(self):
        """The separate customer-portal login (My Aircraft) gets the same
        treatment via its own customer_login_required."""
        c = self.login("customer")
        self.assertEqual(c.get("/portal/").status_code, 200)
        self.exec("UPDATE customers SET active = 0 WHERE id = ?", (self.customer_id,))
        r = c.get("/portal/", follow_redirects=True)
        self.assertIn("no longer active", r.get_data(as_text=True))
        with c.session_transaction() as s:
            self.assertNotIn("customer_id", s)

    # ----- demotion: a narrower role applies immediately ------------------
    def test_demoted_shop_admin_loses_admin_only_access_immediately(self):
        c = self.login("shop_admin")
        # Admin-only page, no logout/login round trip yet.
        self.assertEqual(c.get("/shop/tools/new").status_code, 200)

        self.exec("UPDATE users SET shop_role = 'tech' WHERE id = ?", (self.users["shop_admin"]["id"],))

        r = c.get("/shop/tools/new")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertEqual(s["shop_role"], "tech")
        # The dashboard itself (open to tech too) still works - only the
        # admin-only page closed.
        self.assertEqual(c.get("/shop").status_code, 200)

    def test_removed_master_admin_loses_accounts_page_immediately(self):
        """The bug report's own worst case: a removed master admin kept
        opening Admin > Accounts."""
        c = self.login("master")
        self.assertEqual(c.get("/admin/users").status_code, 200)

        self.exec("UPDATE users SET is_master_admin = 0, shop_role = 'tech', flight_role = NULL, "
                  "can_bill = 0 WHERE id = ?", (self.users["master"]["id"],))

        r = c.get("/admin/users")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertFalse(s["is_master_admin"])

    def test_flight_school_admin_required_demotion_takes_effect_immediately(self):
        """flight.py's own admin_required, not auth.py's."""
        c = self.login("master")
        self.assertEqual(c.get("/flight/cfis").status_code, 200)
        self.exec("UPDATE users SET is_master_admin = 0 WHERE id = ?", (self.users["master"]["id"],))
        self.assertEqual(c.get("/flight/cfis").status_code, 302)

    def test_billing_permission_removed_takes_effect_immediately(self):
        c = self.login("cfi_billing")
        self.assertEqual(c.get("/flight/billing").status_code, 200)
        self.exec("UPDATE users SET can_bill = 0 WHERE id = ?", (self.users["cfi_billing"]["id"],))
        r = c.get("/flight/billing")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertFalse(s["can_bill"])

    # ----- promotion: a wider role also applies immediately ---------------
    def test_promoted_tech_gains_admin_only_access_immediately(self):
        c = self.login("tech")
        self.assertEqual(c.get("/shop/tools/new").status_code, 302)

        self.exec("UPDATE users SET shop_role = 'admin' WHERE id = ?", (self.users["tech"]["id"],))

        r = c.get("/shop/tools/new")
        self.assertEqual(r.status_code, 200)
        with c.session_transaction() as s:
            self.assertEqual(s["shop_role"], "admin")

    def test_granted_academy_access_takes_effect_immediately(self):
        """Flight Academy checks academy_access itself inside the route
        body (not one of the shared role-check decorators), but it still
        relies on refresh_or_expire_session having re-synced the session
        field from the fresh row."""
        c = self.login("tech")
        r = c.get("/academy", follow_redirects=True)
        self.assertIn("Ask an admin", r.get_data(as_text=True))

        self.exec("UPDATE users SET academy_access = 1 WHERE id = ?", (self.users["tech"]["id"],))

        r2 = c.get("/academy")
        self.assertEqual(r2.status_code, 200)
        with c.session_transaction() as s:
            self.assertTrue(s["academy_access"])

    def test_granted_master_admin_takes_effect_immediately(self):
        c = self.login("tech")
        self.assertEqual(c.get("/admin/users").status_code, 302)
        self.exec("UPDATE users SET is_master_admin = 1 WHERE id = ?", (self.users["tech"]["id"],))
        self.assertEqual(c.get("/admin/users").status_code, 200)

    # ----- "View as" role preview must keep working ------------------------
    def test_role_preview_is_not_reset_by_the_session_refresh(self):
        """A master admin previewing as Tech must keep seeing the Tech view
        on every request - refresh_or_expire_session must not silently
        cancel the preview by pulling the real (Admin) role back onto the
        live session."""
        c = self.login("master")
        r = c.post("/view-as/shop/tech")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertEqual(s["shop_role"], "tech")
            self.assertFalse(s["is_master_admin"])
        # Admin-only page is closed while previewing as Tech...
        self.assertEqual(c.get("/shop/tools/new").status_code, 302)
        # ...on more than one request in a row - the preview isn't reset
        # back to the real Admin role by the per-request refresh.
        self.assertEqual(c.get("/shop/tools/new").status_code, 302)
        with c.session_transaction() as s:
            self.assertEqual(s["shop_role"], "tech")

    def test_exiting_a_role_preview_restores_the_up_to_date_real_role(self):
        """If the real account's role changed while a preview was active,
        exiting the preview should land on the fresh real role, not one
        frozen from whenever the preview started."""
        c = self.login("master")
        c.post("/view-as/shop/tech")
        with c.session_transaction() as s:
            self.assertEqual(s["shop_role"], "tech")
            self.assertFalse(s["is_master_admin"])
        # The real account is demoted from master admin while previewing.
        self.exec("UPDATE users SET is_master_admin = 0 WHERE id = ?", (self.users["master"]["id"],))
        c.get("/shop")  # any protected page runs the refresh
        c.post("/view-as/exit")
        with c.session_transaction() as s:
            self.assertFalse(s["is_master_admin"])
            self.assertEqual(s["shop_role"], "admin")
