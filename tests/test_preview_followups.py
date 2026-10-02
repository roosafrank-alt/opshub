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
