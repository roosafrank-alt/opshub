"""Admin > Accounts, second pass: deactivate keeps linked profiles in step,
repeat saves, junk input."""
from harness import OpsHubTestCase


class MoreAdminUserTest(OpsHubTestCase):
    def uid(self, key):
        return self.users[key]["id"]

    def edit(self, key, **kw):
        base = dict(name=self.users[key]["name"], shop_role=self.users[key]["shop_role"],
                    flight_role=self.users[key]["flight_role"], active="1")
        base.update(kw)
        base = {k: v for k, v in base.items() if v is not None}
        return self.client.post(f"/admin/users/{self.uid(key)}/edit", data=base)

    def test_deactivate_cfi_also_deactivates_cfi_profile_and_back(self):
        self.login("master")
        self.edit("cfi", active=None)
        self.assertEqual(self.q1("SELECT active a FROM users WHERE id=?", (self.uid("cfi"),))["a"], 0)
        row = self.q1("SELECT active a FROM cfis WHERE user_id=?", (self.uid("cfi"),))
        if row is not None:
            self.assertEqual(row["a"], 0)
        self.edit("cfi", active="1")
        row = self.q1("SELECT active a FROM cfis WHERE user_id=?", (self.uid("cfi"),))
        if row is not None:
            self.assertEqual(row["a"], 1)

    def test_deactivate_student_also_deactivates_student_profile(self):
        self.login("master")
        self.edit("flight_student", active=None)
        row = self.q1("SELECT active a FROM students WHERE user_id=?", (self.uid("flight_student"),))
        if row is not None:
            self.assertEqual(row["a"], 0)

    def test_saving_same_form_twice_changes_nothing(self):
        self.login("master")
        self.edit("tech", email="t@example.com", phone="555-0100")
        a = dict(self.q1("SELECT * FROM users WHERE id=?", (self.uid("tech"),)))
        self.edit("tech", email="t@example.com", phone="555-0100")
        b = dict(self.q1("SELECT * FROM users WHERE id=?", (self.uid("tech"),)))
        self.assertEqual(a, b)

    def test_blank_email_saved_as_empty_not_text(self):
        self.login("master")
        self.edit("tech", email="   ", phone="  ")
        row = self.q1("SELECT email, phone FROM users WHERE id=?", (self.uid("tech"),))
        self.assertFalse(row["email"])
        self.assertFalse(row["phone"])

    def test_junk_ids_are_not_500(self):
        self.login("master")
        for path in ("/admin/users/0/edit", "/admin/users/-1/edit", "/admin/users/abc/edit"):
            self.assertLess(self.client.get(path).status_code, 500, path)

    def test_unknown_role_value_does_not_crash_or_grant_admin(self):
        self.login("master")
        r = self.edit("tech", shop_role="superuser", flight_role="wizard")
        self.assertLess(r.status_code, 500)
        row = self.q1("SELECT is_master_admin m FROM users WHERE id=?", (self.uid("tech"),))
        self.assertEqual(row["m"], 0)
