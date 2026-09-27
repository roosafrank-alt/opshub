"""QA finding ux-shop-dash-by-role: an Apprentice or Inspector landed on the
same shop home as an admin - squawk Acknowledge boxes they can't use,
Pending Orders, Low Stock, Recent Activity - 14 of 25 links just bounced
them back. The dashboard is now reshaped around what they actually do:
Scan, (Inspector only) Sub Areas Awaiting Confirmation, Active Projects,
Upcoming Schedule, Currently Clocked In. Admins, Techs and master admins
keep the full page (see is_limited_role in dashboard.html)."""
from harness import OpsHubTestCase, seed_row
import db


class ShopDashboardByRoleTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        self.project_id = self.make_project(name="Annual - N999TT", asset_id=self.asset_id)

    def test_apprentice_sees_active_projects_list(self):
        c = self.login("shop_student")  # ROLES maps shop_student -> shop_role='apprentice'
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Active Projects", body)
        self.assertIn("Annual - N999TT", body)
        self.assertIn(f"/projects/{self.project_id}", body)

    def test_apprentice_does_not_see_admin_boxes(self):
        c = self.login("shop_student")
        body = c.get("/shop").get_data(as_text=True)
        self.assertNotIn("Pending Orders", body)
        self.assertNotIn("Low Stock", body)
        self.assertNotIn("Recent Activity", body)

    def test_apprentice_keeps_upcoming_schedule_and_clocked_in(self):
        c = self.login("shop_student")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Upcoming Schedule", body)
        self.assertIn("Currently Clocked In", body)

    def test_inspector_sees_active_projects_list(self):
        c = self.login("inspector")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Active Projects", body)
        self.assertIn("Annual - N999TT", body)

    def test_inspector_does_not_see_admin_boxes(self):
        c = self.login("inspector")
        body = c.get("/shop").get_data(as_text=True)
        self.assertNotIn("Pending Orders", body)
        self.assertNotIn("Low Stock", body)
        self.assertNotIn("Recent Activity", body)

    def test_inspector_sees_sub_areas_awaiting_confirmation_near_top(self):
        section_id = self.exec(
            "INSERT INTO project_sections (project_id, name, created_at, confirm_requested_at, confirm_requested_by) "
            "VALUES (?,?,?,?,?)",
            (self.project_id, "Brakes", db.now_iso(), db.now_iso(), "Tech"))
        c = self.login("inspector")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Sub Area", body)
        self.assertIn("Awaiting Confirmation", body)
        # Right under Scan, before Active Projects.
        self.assertLess(body.index("Awaiting Confirmation"), body.index("Active Projects"))

    def test_admin_keeps_the_full_dashboard(self):
        c = self.login("shop_admin")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Pending Orders", body)
        self.assertIn("Low Stock", body)
        self.assertIn("Recent Activity", body)
        self.assertNotIn("Active Projects</h6>", body)

    def test_tech_keeps_the_full_dashboard(self):
        c = self.login("tech")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Pending Orders", body)
        self.assertIn("Low Stock", body)

    def test_master_admin_keeps_full_dashboard_even_with_apprentice_shop_role(self):
        # Master admin always sees the full page, whatever shop_role says.
        self.exec("UPDATE users SET shop_role = 'apprentice' WHERE username = 'master'")
        c = self.login("master")
        body = c.get("/shop").get_data(as_text=True)
        self.assertIn("Pending Orders", body)
        self.assertIn("Low Stock", body)
