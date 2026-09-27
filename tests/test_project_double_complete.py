"""QA fix qa-project-double-complete: marking a 100-hour or oil change job
Completed when it's already completed (a double tap, or pressing Complete
again later) used to record the inspection in the plane's maintenance
history a second time. Pressing Complete on an already-completed job is
now a no-op; reopening it and completing it again still records normally.
"""
from harness import OpsHubTestCase


class ProjectDoubleCompleteTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N2231Q")
        self.exec("UPDATE assets SET tach_hours = 850 WHERE id = ?", (self.plane,))
        self.project = self.make_project(name="100hr Inspection - N2231Q", asset_id=self.plane)

    def logs(self):
        return self.q("SELECT * FROM maintenance_log WHERE project_id = ?", (self.project,))

    def test_pressing_complete_twice_records_the_inspection_once(self):
        c = self.login("shop_admin")
        c.post(f"/projects/{self.project}/status", data=dict(status="completed"))
        self.assertEqual(len(self.logs()), 1)
        c.post(f"/projects/{self.project}/status", data=dict(status="completed"))
        self.assertEqual(len(self.logs()), 1)

    def test_pressing_complete_a_third_time_still_records_once(self):
        c = self.login("shop_admin")
        for _ in range(3):
            c.post(f"/projects/{self.project}/status", data=dict(status="completed"))
        self.assertEqual(len(self.logs()), 1)

    def test_completed_at_and_by_are_unchanged_by_a_repeat_complete(self):
        c = self.login("shop_admin")
        c.post(f"/projects/{self.project}/status", data=dict(status="completed"))
        first = self.q1("SELECT completed_at, completed_by FROM projects WHERE id = ?", (self.project,))
        c.post(f"/projects/{self.project}/status", data=dict(status="completed"))
        second = self.q1("SELECT completed_at, completed_by FROM projects WHERE id = ?", (self.project,))
        self.assertEqual(dict(first), dict(second))

    def test_reopening_and_completing_again_records_a_second_time(self):
        c = self.login("shop_admin")
        c.post(f"/projects/{self.project}/status", data=dict(status="completed"))
        self.assertEqual(len(self.logs()), 1)
        c.post(f"/projects/{self.project}/status", data=dict(status="active"))
        c.post(f"/projects/{self.project}/status", data=dict(status="completed"))
        self.assertEqual(len(self.logs()), 2)
