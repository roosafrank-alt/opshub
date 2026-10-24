"""QA finding qa-costs-visible-to-techs, follow-up edit: "techs who are not
admin can not see any cost of parts, hourly rate or costs other then their
pay per hour and pay due." The original fix stripped cost/sell_price from
the scanner lookup, the Parts Used page and the Orders CSV export. This
covers the remaining spots a tech can actually reach: a part's own detail
page (Unit Cost/Sell Price/Total Value), and a project's page (parts-used
Cost column/sub-area total, and the Labor Cost column/total - which would
otherwise reveal another worker's hourly rate via rate x hours)."""
from harness import OpsHubTestCase


class TechCostsFurtherHiddenTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.part = self.make_part(name="Oil Filter", unit_cost=12.50)
        self.pid = self.make_project(name="N555 Annual")
        self.exec("INSERT INTO transactions (part_id, project_id, type, qty, performed_by, section, created_at) "
                  "VALUES (?,?, 'out', 2, 'Tech', 'General', datetime('now'))", (self.part, self.pid))

    def test_tech_does_not_see_part_cost_fields_on_part_detail(self):
        html = self.login("tech").get(f"/parts/{self.part}").get_data(as_text=True)
        self.assertNotIn("Unit Cost", html)
        self.assertNotIn("Sell Price", html)
        self.assertNotIn("Total Value", html)

    def test_admin_still_sees_part_cost_fields_on_part_detail(self):
        html = self.login("shop_admin").get(f"/parts/{self.part}").get_data(as_text=True)
        self.assertIn("Unit Cost", html)
        self.assertIn("Sell Price", html)

    def test_tech_does_not_see_parts_used_cost_on_project_page(self):
        html = self.login("tech").get(f"/projects/{self.pid}").get_data(as_text=True)
        self.assertNotIn("$25.00", html)  # 2 x $12.50

    def test_admin_still_sees_parts_used_cost_on_project_page(self):
        html = self.login("shop_admin").get(f"/projects/{self.pid}").get_data(as_text=True)
        self.assertIn("$25.00", html)

    def test_tech_does_not_see_labor_cost_or_rate_on_project_page(self):
        laborer = self.exec("INSERT INTO laborers (name, code, rate, active, created_at, updated_at) "
                            "VALUES ('Other Worker', 'LAB-OTHER', 40.00, 1, datetime('now'), datetime('now'))")
        self.exec("INSERT INTO labor_sessions (laborer_id, project_id, section, started_at, ended_at, hours, cost) "
                  "VALUES (?, ?, 'General', datetime('now','-1 hour'), datetime('now'), 1.0, 40.00)",
                  (laborer, self.pid))
        html = self.login("tech").get(f"/projects/{self.pid}").get_data(as_text=True)
        self.assertNotIn("$40.00", html)

    def test_admin_still_sees_labor_cost_on_project_page(self):
        laborer = self.exec("INSERT INTO laborers (name, code, rate, active, created_at, updated_at) "
                            "VALUES ('Other Worker', 'LAB-OTHER', 40.00, 1, datetime('now'), datetime('now'))")
        self.exec("INSERT INTO labor_sessions (laborer_id, project_id, section, started_at, ended_at, hours, cost) "
                  "VALUES (?, ?, 'General', datetime('now','-1 hour'), datetime('now'), 1.0, 40.00)",
                  (laborer, self.pid))
        html = self.login("shop_admin").get(f"/projects/{self.pid}").get_data(as_text=True)
        self.assertIn("$40.00", html)
