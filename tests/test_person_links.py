"""One person, several roles (idea "Admin"): accounts are made in Admin, a
shop worker badge links to the account like a CFI profile does, and Payroll
shows the person's pay across roles together, each role at its own rate."""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class PersonLinksTest(OpsHubTestCase):
    def test_new_account_can_make_a_linked_badge_and_set_cfi_rate(self):
        c = self.login("master")
        r = c.post("/admin/users/new", data=dict(name="Katelynn Kearney", username="kate", password="pw12345",
                                                  flight_role="cfi", shop_role="tech", badge="new",
                                                  badge_rate="22", cfi_pay_rate="35"))
        self.assertEqual(r.status_code, 302)
        u = self.q1("SELECT * FROM users WHERE username='kate'")
        lab = self.q1("SELECT * FROM laborers WHERE user_id = ?", (u["id"],))
        self.assertEqual((lab["name"], lab["rate"]), ("Katelynn Kearney", 22))
        self.assertTrue(lab["code"].startswith("LABOR-"))
        self.assertEqual(self.q1("SELECT pay_rate_per_hour FROM cfis WHERE user_id = ?", (u["id"],))[0], 35)

    def test_bad_rate_saves_nothing(self):
        c = self.login("master")
        c.post("/admin/users/new", data=dict(name="X", username="x1", password="pw12345", badge="new", badge_rate="-3"))
        self.assertIsNone(self.q1("SELECT id FROM users WHERE username='x1'"))

    def test_existing_badge_links_to_account_and_payroll_groups_them(self):
        conn = db.get_db()
        kate_badge = seed_row(conn, "laborers", name="Kate", code="LABOR-KATE", rate=20, active=1)
        conn.commit(); conn.close()
        cfi_user = self.users["cfi"]
        c = self.login("master")
        r = c.post(f"/admin/users/{cfi_user['id']}/edit",
                   data=dict(name=cfi_user["name"], flight_role="cfi", active="on", badge=str(kate_badge),
                             cfi_pay_rate="40"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.q1("SELECT user_id FROM laborers WHERE id = ?", (kate_badge,))[0], cfi_user["id"])
        # Some shop time this week so the badge shows on Payroll.
        today = date.today().isoformat()
        self.exec("INSERT INTO labor_sessions (laborer_id, project_id, started_at, ended_at, hours, cost, rate, created_at) "
                  "VALUES (?, NULL, ?, ?, 2, 40, 20, ?)", (kate_badge, today + " 08:00:00", today + " 10:00:00", db.now_iso()))
        html = c.get("/admin/payroll").get_data(as_text=True)
        self.assertIn("2 roles", html)
        self.assertIn(cfi_user["name"], html)
        csv_text = c.get("/payroll/export").get_data(as_text=True)
        self.assertIn(f"{cfi_user['name']},Kate,Shop", csv_text)

    def test_unlink(self):
        conn = db.get_db()
        b = seed_row(conn, "laborers", name="Kate", code="LABOR-KATE", rate=20, active=1,
                     user_id=self.users["tech"]["id"])
        conn.commit(); conn.close()
        t = self.users["tech"]
        self.login("master").post(f"/admin/users/{t['id']}/edit",
                                  data=dict(name=t["name"], shop_role="tech", active="on", unlink_badge=str(b)))
        self.assertIsNone(self.q1("SELECT user_id FROM laborers WHERE id = ?", (b,))[0])

    def test_add_buttons_send_master_admins_to_admin(self):
        c = self.login("master")
        self.assertIn("/admin/users/new?shop_role=tech", c.get("/laborers").get_data(as_text=True))
        self.assertIn("/admin/users/new?flight_role=cfi", c.get("/flight/cfis").get_data(as_text=True))
        html = c.get("/admin/users/new?flight_role=cfi").get_data(as_text=True)
        self.assertIn('value="cfi" id="flight-role-cfi" checked', html)
