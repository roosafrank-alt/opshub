"""Small cross-part follow-ups from the UI review merge (SHOP-36, JOBS-15, SCHOOL-04)."""
from harness import OpsHubTestCase


class FollowUpTest(OpsHubTestCase):
    def test_shop_36_admin_manage_menu_has_recently_deleted(self):
        html = self.login("master").get("/projects").get_data(as_text=True)
        self.assertIn("Recently Deleted", html)
        self.assertIn('href="/trash"', html)

    def test_jobs_15_inspector_header_has_aircraft_squawks_my_tasks(self):
        html = self.login("inspector").get("/projects").get_data(as_text=True)
        for needle in ('href="/assets"', 'href="/squawks"', 'href="/my-tasks"'):
            self.assertIn(needle, html)
        self.assertNotIn('href="/trash"', html)

    def test_school_04_admin_account_save_returns_to_next_page(self):
        uid = self.q1("SELECT id FROM users WHERE username = 'tech'")["id"]
        c = self.login("master")
        r = c.post(f"/admin/users/{uid}/edit?next=/flight/students", data={"name": "Tech Person", "shop_role": "tech", "active": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/flight/students"), r.headers["Location"])
        r = c.post(f"/admin/users/{uid}/edit?next=//evil.example", data={"name": "Tech Person", "shop_role": "tech", "active": "1"})
        self.assertNotIn("evil.example", r.headers["Location"])


class DashboardShortcutsTest(OpsHubTestCase):
    """FLY-30: each kind of user gets their own most-frequent shortcuts."""
    def test_instructor_tiles(self):
        html = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        for needle in ("/flight/schedule/availability", "/flight/waitlist", "/flight/students"):
            self.assertIn(f'href="{needle}"', html)
        self.assertNotIn("Log a Past Session", html)

    def test_student_tiles_have_no_planes(self):
        html = self.login("flight_student").get("/flight/dashboard").get_data(as_text=True)
        for needle in ("/flight/schedule/availability", "/flight/waitlist", "/flight/schedule/new"):
            self.assertIn(f'href="{needle}"', html)
        self.assertNotIn('class="fw-bold dash-shortcut-label">Planes<', html)

    def test_flight_history_has_the_log_a_past_session_button_for_instructors(self):
        self.assertIn("Log a Past Session", self.login("cfi").get("/flight/log").get_data(as_text=True))


class AutoscanWaitsForScanningAsTest(OpsHubTestCase):
    def test_shop_39_autoscan_waits_for_the_scanning_as_name(self):
        """The dashboard's Scan In opens /scan?autoscan=<code>. A fixed 250 ms wait ran before the
        'Scanning as' name was filled in on a slow connection (red 'Select who's scanning')."""
        html = self.login("master").get("/scan").get_data(as_text=True)
        self.assertIn("const operatorsReady = fetch('/api/operators')", html)
        self.assertIn("operatorsReady.then(function () { processScan(auto, false); })", html)
        self.assertNotIn("setTimeout(function () { processScan(auto, false); }, 250)", html)
