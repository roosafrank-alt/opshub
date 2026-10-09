"""Admin > Accounts: create, edit, deactivate, role changes, owner lock.

Tests marked @open_finding("qa-...") describe the CORRECT behavior for a
problem reported on the Idea Queue page but not fixed yet.
"""
from harness import OpsHubTestCase, PASSWORD, open_finding


def form(**kw):
    d = dict(name="New Person", username="newperson", password="pw-12345", shop_role="tech")
    d.update(kw)
    return {k: v for k, v in d.items() if v is not None}


class AccessTest(OpsHubTestCase):
    def test_only_master_admin_sees_account_pages(self):
        for role in ("shop_admin", "tech", "inspector", "cfi", "flight_student", "no_roles"):
            self.login(role)
            for path in ("/admin/users", "/admin/users/new"):
                r = self.client.get(path)
                self.assertIn(r.status_code, (302, 403), (role, path))
            before = self.q1("SELECT COUNT(*) n FROM users")["n"]
            self.client.post("/admin/users/new", data=form(username="sneaky"))
            self.assertEqual(self.q1("SELECT COUNT(*) n FROM users")["n"], before, role)

    def test_master_sees_list(self):
        self.login("master")
        self.assertEqual(self.client.get("/admin/users").status_code, 200)


class CreateTest(OpsHubTestCase):
    def test_create_then_new_person_can_log_in(self):
        self.login("master")
        r = self.client.post("/admin/users/new", data=form())
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM users WHERE username='newperson'")
        self.assertEqual(row["shop_role"], "tech")
        self.assertEqual(row["active"], 1)
        c = self.app.test_client()
        r = c.post("/", data=dict(username="newperson", password="pw-12345"))
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertEqual(s.get("shop_role"), "tech")

    def test_blank_fields_rejected(self):
        self.login("master")
        before = self.q1("SELECT COUNT(*) n FROM users")["n"]
        for bad in (form(name="  "), form(username=" "), form(password="")):
            r = self.client.post("/admin/users/new", data=bad)
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM users")["n"], before)

    def test_duplicate_username_rejected(self):
        self.login("master")
        before = self.q1("SELECT COUNT(*) n FROM users")["n"]
        r = self.client.post("/admin/users/new", data=form(username="tech"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM users")["n"], before)

    def test_double_submit_makes_one_account(self):
        self.login("master")
        self.client.post("/admin/users/new", data=form())
        self.client.post("/admin/users/new", data=form())
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM users WHERE username='newperson'")["n"], 1)

    def test_flight_role_gets_profile(self):
        self.login("master")
        self.client.post("/admin/users/new", data=form(shop_role=None, flight_role="student", username="stu9"))
        uid = self.q1("SELECT id FROM users WHERE username='stu9'")["id"]
        self.assertIsNotNone(self.q1("SELECT id FROM students WHERE user_id=?", (uid,)))

    @open_finding("qa-user-password-stored-readable")
    def test_password_not_kept_in_readable_form(self):
        self.login("master")
        self.client.post("/admin/users/new", data=form())
        row = self.q1("SELECT * FROM users WHERE username='newperson'")
        self.assertFalse(row["password_plain"])


class EditTest(OpsHubTestCase):
    def uid(self, key):
        return self.users[key]["id"]

    def edit(self, key, **kw):
        base = dict(name=self.users[key]["name"], shop_role=self.users[key]["shop_role"],
                    flight_role=self.users[key]["flight_role"], active="1")
        base.update(kw)
        base = {k: v for k, v in base.items() if v is not None}
        return self.client.post(f"/admin/users/{self.uid(key)}/edit", data=base)

    def test_missing_account_redirects_not_500(self):
        self.login("master")
        r = self.client.post("/admin/users/99999/edit", data=dict(name="x"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/admin/users/99999/edit").status_code, 302)

    def test_blank_name_rejected(self):
        self.login("master")
        r = self.edit("tech", name=" ")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.q1("SELECT name FROM users WHERE id=?", (self.uid("tech"),))["name"], "Tech")

    def test_cannot_remove_own_admin_or_deactivate_self(self):
        # master is the oldest master admin = the owner, so use a second admin.
        self.exec("UPDATE users SET is_master_admin=1 WHERE id=?", (self.uid("shop_admin"),))
        self.login("shop_admin")
        self.edit("shop_admin", is_master_admin=None, active="1")
        self.assertEqual(self.q1("SELECT is_master_admin m FROM users WHERE id=?", (self.uid("shop_admin"),))["m"], 1)
        self.edit("shop_admin", is_master_admin="1", active=None)
        self.assertEqual(self.q1("SELECT active a FROM users WHERE id=?", (self.uid("shop_admin"),))["a"], 1)

    def test_other_admin_cannot_change_owner(self):
        self.exec("UPDATE users SET is_master_admin=1 WHERE id=?", (self.uid("shop_admin"),))
        self.login("master")  # sets owner = oldest master admin
        self.client.get("/admin/users")
        self.login("shop_admin")
        r = self.client.post(f"/admin/users/{self.uid('master')}/edit",
                             data=dict(name="Hacked", is_master_admin="1", active="1", password="owned"))
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT name, active FROM users WHERE id=?", (self.uid("master"),))
        self.assertEqual(row["name"], "Master")
        self.assertEqual(row["active"], 1)

    def test_password_reset_works_and_blank_keeps_old(self):
        self.login("master")
        self.edit("tech", password="brand-new-1")
        c = self.app.test_client()
        self.assertEqual(c.post("/", data=dict(username="tech", password="brand-new-1")).status_code, 302)
        self.edit("tech")  # blank password
        c = self.app.test_client()
        self.assertEqual(c.post("/", data=dict(username="tech", password="brand-new-1")).status_code, 302)

    def test_deactivated_person_cannot_log_in(self):
        self.login("master")
        self.edit("tech", active=None)
        c = self.app.test_client()
        r = c.post("/", data=dict(username="tech", password=PASSWORD))
        self.assertEqual(r.status_code, 200)

    def test_deactivated_person_already_signed_in_is_cut_off(self):
        tech = self.login("tech")
        self.assertEqual(tech.get("/parts").status_code, 200)
        self.exec("UPDATE users SET active=0 WHERE id=?", (self.uid("tech"),))
        r = tech.get("/parts")
        self.assertEqual(r.status_code, 302)

    def test_role_change_applies_on_next_request(self):
        tech = self.login("tech")
        self.assertEqual(tech.get("/parts").status_code, 200)
        self.exec("UPDATE users SET shop_role='apprentice' WHERE id=?", (self.uid("tech"),))
        r = tech.get("/parts")
        self.assertEqual(r.status_code, 302)

    def test_admin_rights_removed_apply_on_next_request(self):
        self.exec("UPDATE users SET is_master_admin=1 WHERE id=?", (self.uid("shop_admin"),))
        c = self.login("shop_admin")
        self.assertEqual(c.get("/admin/users").status_code, 200)
        self.exec("UPDATE users SET is_master_admin=0 WHERE id=?", (self.uid("shop_admin"),))
        self.assertIn(c.get("/admin/users").status_code, (302, 403))
