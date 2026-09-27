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

    def test_clocked_in_list_carries_the_session_id_for_the_lockout_button(self):
        d = self.login("tech").get("/api/labor/clocked-in").get_json()
        by_name = {w["name"]: w for w in d["workers"]}
        session_id = self.q1("SELECT id FROM labor_sessions WHERE laborer_id = ?", (self.joe,))["id"]
        self.assertEqual(by_name["Joe Tech"]["id"], session_id)

    def test_page_includes_a_stop_button_wired_to_the_lockout_route(self):
        html = self.login("tech").get("/projects").get_data(as_text=True)
        self.assertIn("labor-timer-stop-btn", html)
        self.assertIn("/api/labor/stop/", html)

    def test_lockout_button_route_ends_the_timer(self):
        session_id = self.q1("SELECT id FROM labor_sessions WHERE laborer_id = ?", (self.joe,))["id"]
        c = self.login("tech")
        r = c.post(f"/api/labor/stop/{session_id}")
        self.assertTrue(r.get_json()["ok"])
        row = self.q1("SELECT ended_at FROM labor_sessions WHERE id = ?", (session_id,))
        self.assertIsNotNone(row["ended_at"])
        d = c.get("/api/labor/clocked-in").get_json()
        self.assertNotIn("Joe Tech", {w["name"] for w in d["workers"]})
