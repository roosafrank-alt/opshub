"""Floating "who's clocked in" timer (idea "Floating timer maint")."""
from harness import OpsHubTestCase, seed_row
import db


class LaborTimerWidgetTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.pid = self.make_project(name="Annual - N1")
        conn = db.get_db()
        self.joe = seed_row(conn, "laborers", name="Joe Tech", code="LABOR-JOE", rate=30, active=1)
        self.ann = seed_row(conn, "laborers", name="Ann Wrench", code="LABOR-ANN", rate=30, active=1)
        seed_row(conn, "labor_sessions", laborer_id=self.joe, project_id=self.pid, section="Plugs",
                 started_at=db.now_iso(), ended_at=None, rate=30)
        seed_row(conn, "labor_sessions", laborer_id=self.ann, project_id=None, section=None,
                 started_at=db.now_iso(), ended_at=None, rate=30)
        conn.commit()
        conn.close()

    def test_shop_roles_see_everyone_clocked_in_with_project_and_sub_area(self):
        for role in ("tech", "shop_admin", "master", "inspector", "shop_student"):
            with self.subTest(role=role):
                d = self.login(role).get("/api/labor/clocked-in").get_json()
                by_name = {w["name"]: w for w in d["workers"]}
                self.assertEqual(set(by_name), {"Joe Tech", "Ann Wrench"})
                self.assertEqual(by_name["Joe Tech"]["section"], "Plugs")
                self.assertTrue(by_name["Ann Wrench"]["general"])

    def test_flight_only_accounts_get_nobody(self):
        for role in ("cfi", "flight_student", "no_roles"):
            with self.subTest(role=role):
                self.assertEqual(self.login(role).get("/api/labor/clocked-in").get_json()["workers"], [])

    def test_widget_is_on_shop_and_admin_pages_only_for_shop_staff(self):
        self.assertIn('id="labor-timer-widget"', self.login("tech").get("/projects").get_data(as_text=True))
        self.assertIn('id="labor-timer-widget"', self.login("master").get("/admin").get_data(as_text=True))
        self.assertNotIn('id="labor-timer-widget"', self.login("cfi").get("/flight/").get_data(as_text=True))
