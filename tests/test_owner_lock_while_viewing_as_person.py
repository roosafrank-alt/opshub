"""QA fix qa-viewas-owner-password: the owner's password is hidden and the
Edit button locked in Admin > Accounts for every OTHER master admin. (The
"View as someone" half of the original finding is gone - HUB-03 made the
Switch role chips the only look-as-someone feature - so this keeps just the
owner lock itself. HUB-25 moved every password off the Accounts list: it
shows only on the Edit page, behind Show.)
"""
from harness import OpsHubTestCase
import db


OWNER_PASSWORD = "owner-secret-pw"


class OwnerLockTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        # self.users["master"] is the oldest master admin, so it becomes the
        # owner (auth.owner_user_id) the first time anything asks it.
        self.exec("UPDATE users SET password_plain = ? WHERE id = ?",
                  (OWNER_PASSWORD, self.users["master"]["id"]))
        self.owner = self.q1("SELECT * FROM users WHERE id = ?", (self.users["master"]["id"],))
        # A SECOND, different master admin - not the owner.
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

    def test_owner_locked_for_a_second_admin(self):
        c = self._login_second_admin()
        html = c.get("/admin/users").get_data(as_text=True)
        self.assertNotIn(OWNER_PASSWORD, html)
        self.assertIn("Locked", html)
        self.assertNotIn(f"/admin/users/{self.owner['id']}/edit", html)
        r = c.get(f"/admin/users/{self.owner['id']}/edit")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn(OWNER_PASSWORD, c.get(f"/admin/users/{self.owner['id']}/edit", follow_redirects=True).get_data(as_text=True))

    def test_owner_can_still_see_and_edit_their_own_account(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = self.owner["id"]
            s["is_master_admin"] = True
        html = c.get("/admin/users").get_data(as_text=True)
        self.assertIn(f"/admin/users/{self.owner['id']}/edit", html)
        r = c.get(f"/admin/users/{self.owner['id']}/edit")
        self.assertEqual(r.status_code, 200)
        self.assertIn(OWNER_PASSWORD, r.get_data(as_text=True))
