"""Laborers list: Edit matches the Students/CFIs style (labeled button),
and a Pay link jumps straight to that one laborer's pay, same idea as a
CFI's own Pay page (idea "account", revision 3)."""
from harness import OpsHubTestCase, seed_row
import db


class LaborerPayLinkTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        conn = db.get_db()
        self.joe = seed_row(conn, "laborers", name="Joe Tech", code="LABOR-JOE", rate=30, active=1)
        self.ann = seed_row(conn, "laborers", name="Ann Wrench", code="LABOR-ANN", rate=40, active=1)
        seed_row(conn, "labor_sessions", laborer_id=self.joe, project_id=None, section=None,
                 started_at="2026-09-20 08:00:00", ended_at="2026-09-20 10:00:00", rate=30, hours=2, cost=60)
        seed_row(conn, "labor_sessions", laborer_id=self.ann, project_id=None, section=None,
                 started_at="2026-09-20 08:00:00", ended_at="2026-09-20 09:00:00", rate=40, hours=1, cost=40)
        conn.commit()
        conn.close()

    def test_laborers_list_has_labeled_edit_and_a_pay_link(self):
        html = self.login("shop_admin").get("/laborers").get_data(as_text=True)
        self.assertIn('<i class="bi bi-pencil"></i> Edit</a>', html)
        self.assertIn(f"/shop/pay?laborer_id={self.joe}", html)
        self.assertIn(f"/shop/pay?laborer_id={self.ann}", html)

    def test_pay_link_filters_to_just_that_laborer(self):
        html = self.login("shop_admin").get(f"/shop/pay?laborer_id={self.joe}&period=custom&from=2026-09-01&to=2026-09-30").get_data(as_text=True)
        self.assertIn("Joe Tech", html)
        self.assertNotIn("Ann Wrench", html)
        self.assertIn("Joe Tech&#39;s Pay", html)

    def test_period_switch_keeps_the_laborer_filter(self):
        html = self.login("shop_admin").get(f"/shop/pay?laborer_id={self.ann}").get_data(as_text=True)
        self.assertIn(f"laborer_id={self.ann}", html)
