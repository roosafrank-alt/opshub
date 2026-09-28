"""QA fix qa-viewas-owner-password: the owner's password is hidden and the
Edit button locked in Admin > Accounts for every OTHER master admin - but if
that admin uses "View as someone" (auth.start_view_as_person) to pick the
owner, session['user_id'] is swapped to the owner's own id, and both checks
used to read that swapped value, so the owner's password showed in full and
the owner's Edit page opened (saving stayed blocked, but the password was
already visible). The fix (auth.real_user_id) makes the owner lock look at
the REAL signed-in account, ignoring both "View as someone" and the separate
role-preview chips (auth._view_as_real).
"""
from harness import OpsHubTestCase
import db


OWNER_PASSWORD = "owner-secret-pw"


class OwnerLockWhileViewingAsPersonTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        # self.users["master"] is the oldest master admin, so it becomes the
        # owner (auth.owner_user_id) the first time anything asks it.
        self.exec("UPDATE users SET password_plain = ? WHERE id = ?",
                  (OWNER_PASSWORD, self.users["master"]["id"]))
        self.owner = self.q1("SELECT * FROM users WHERE id = ?", (self.users["master"]["id"],))
        # A SECOND, different master admin - not the owner - to do the
        # "View as someone" picking.
        second_id = self.exec(
            "INSERT INTO users (name, username, password_hash, password_plain, is_master_admin, active, created_at) "
            "VALUES (?, ?, ?, ?, 1, 1, ?)",
            ("Second Admin", "second_admin", self.owner["password_hash"], "second-pw", db.now_iso()))
        self.second_admin = self.q1("SELECT * FROM users WHERE id = ?", (second_id,))

    def _login_second_admin(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = self.second_admin["id"]
            s["user_name"] = self.second_admin["name"]
            s["is_master_admin"] = True
        return c

    def _login_second_admin_and_view_as_owner(self):
        c = self._login_second_admin()
        r = c.post(f"/view-as/person/{self.owner['id']}")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            # Confirms the swap this bug relies on: user_id is now the
            # owner's, even though a different real person is at the keyboard.
            self.assertEqual(s["user_id"], self.owner["id"])
        return c

    # Baseline: without "View as" active, a second master admin already sees
    # the owner correctly locked - this must keep working.
    def test_owner_locked_for_a_plain_second_admin(self):
        c = self._login_second_admin()
        html = c.get("/admin/users").get_data(as_text=True)
        self.assertIn("hidden", html)
        self.assertNotIn(OWNER_PASSWORD, html)
        self.assertIn("Locked", html)
        r = c.get(f"/admin/users/{self.owner['id']}/edit", follow_redirects=True)
        self.assertNotIn(OWNER_PASSWORD, r.get_data(as_text=True))

    def test_owner_password_still_hidden_on_accounts_list_while_viewing_as_owner(self):
        c = self._login_second_admin_and_view_as_owner()
        html = c.get("/admin/users").get_data(as_text=True)
        self.assertIn("hidden", html)
        self.assertNotIn(OWNER_PASSWORD, html)

    def test_owner_edit_button_still_locked_on_accounts_list_while_viewing_as_owner(self):
        c = self._login_second_admin_and_view_as_owner()
        html = c.get("/admin/users").get_data(as_text=True)
        self.assertIn("Locked", html)
        self.assertNotIn(f"/admin/users/{self.owner['id']}/edit", html)

    def test_owner_edit_page_still_refused_while_viewing_as_owner(self):
        c = self._login_second_admin_and_view_as_owner()
        r = c.get(f"/admin/users/{self.owner['id']}/edit")
        self.assertEqual(r.status_code, 302)
        html = c.get(f"/admin/users/{self.owner['id']}/edit", follow_redirects=True).get_data(as_text=True)
        self.assertNotIn(OWNER_PASSWORD, html)

    def test_owner_can_still_see_and_edit_their_own_account(self):
        # Makes sure the fix didn't lock the real owner out of their own
        # account (only the "View as" swap should be ignored, not a real
        # owner login).
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = self.owner["id"]
            s["is_master_admin"] = True
        html = c.get("/admin/users").get_data(as_text=True)
        self.assertIn(OWNER_PASSWORD, html)
        r = c.get(f"/admin/users/{self.owner['id']}/edit")
        self.assertEqual(r.status_code, 200)
        self.assertIn(OWNER_PASSWORD, r.get_data(as_text=True))
