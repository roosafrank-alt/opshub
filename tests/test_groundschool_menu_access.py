"""QA fix ux-flight-groundschool-link: Ground School and its Resources were
only reachable by typing the address - the only links to them were the
Flight Academy tabs, which most CFIs/students don't have. There's now a
Ground School button in the Fly with Kate! menu, gated by a new per-account
'Ground School Access' checkbox (Flight Academy accounts and master admins
always have it), and the Ground School pages themselves now check that same
flag instead of opening for any logged-in flight account. See
groundschool.py's _require_groundschool_access and base_flight.html."""
from harness import OpsHubTestCase


class GroundschoolMenuAccessTest(OpsHubTestCase):
    def test_student_without_access_is_bounced_from_ground_school(self):
        c = self.login("flight_student")
        r = c.get("/flight/groundschool", follow_redirects=True)
        self.assertIn("You don&#39;t have access to Ground School yet", r.get_data(as_text=True))

    def test_student_without_access_is_bounced_from_resources_too(self):
        c = self.login("flight_student")
        r = c.get("/flight/groundschool/resources", follow_redirects=True)
        self.assertIn("You don&#39;t have access to Ground School yet", r.get_data(as_text=True))

    def test_menu_hides_ground_school_button_without_access(self):
        c = self.login("flight_student")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertNotIn("Ground School", body)

    def test_granting_access_unlocks_both_the_page_and_the_menu_button(self):
        self.exec("UPDATE users SET groundschool_access = 1 WHERE username = 'flight_student'")
        c = self.login("flight_student")
        self.assertIn("Ground School", c.get("/flight/dashboard").get_data(as_text=True))
        r = c.get("/flight/groundschool")
        self.assertEqual(r.status_code, 200)

    def test_flight_academy_access_implies_ground_school_access(self):
        self.exec("UPDATE users SET academy_access = 1 WHERE username = 'cfi'")
        c = self.login("cfi")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn("Ground School", body)
        self.assertIn("Academy", body)
        self.assertEqual(c.get("/flight/groundschool").status_code, 200)

    def test_master_admin_always_has_ground_school(self):
        c = self.login("master")
        self.assertIn("Ground School", c.get("/flight/dashboard").get_data(as_text=True))
        self.assertEqual(c.get("/flight/groundschool").status_code, 200)
