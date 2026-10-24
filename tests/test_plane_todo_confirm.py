"""idea "tech spot" revision 2: a Completed section on My Tasks, and a plane
to-do's checkbox now requests Inspector/admin confirmation instead of
completing it outright (same request/confirm two-step a squawk's repair or
a project sub area already requires)."""
from harness import OpsHubTestCase


class PlaneTodoConfirmTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset(tag="N999TD")
        self.tech = self.users["tech"]
        self.todo_id = self.exec(
            "INSERT INTO plane_todos (asset_id, description, created_by, created_at, assigned_to) "
            "VALUES (?, 'Swap tire', 'test', datetime('now'), ?)", (self.asset, self.tech["id"]))

    def _todo(self):
        return self.q1("SELECT * FROM plane_todos WHERE id = ?", (self.todo_id,))

    def test_checking_it_off_only_requests_confirmation(self):
        c = self.login("tech")
        c.post(f"/assets/{self.asset}/todo/{self.todo_id}/toggle")
        t = self._todo()
        self.assertEqual(t["done"], 0)
        self.assertIsNotNone(t["confirm_requested_at"])
        self.assertEqual(t["confirm_requested_by"], "Tech")

    def test_unchecking_a_pending_one_cancels_the_request(self):
        c = self.login("tech")
        c.post(f"/assets/{self.asset}/todo/{self.todo_id}/toggle")
        c.post(f"/assets/{self.asset}/todo/{self.todo_id}/toggle")
        t = self._todo()
        self.assertEqual(t["done"], 0)
        self.assertIsNone(t["confirm_requested_at"])

    def test_inspector_confirms_it_done(self):
        self.login("tech").post(f"/assets/{self.asset}/todo/{self.todo_id}/toggle")
        self.login("inspector").post(f"/assets/{self.asset}/todo/{self.todo_id}/confirm")
        t = self._todo()
        self.assertEqual(t["done"], 1)
        self.assertEqual(t["confirmed_by"], "Inspector")
        self.assertIsNotNone(t["completed_at"])

    def test_admin_can_also_confirm(self):
        self.login("tech").post(f"/assets/{self.asset}/todo/{self.todo_id}/toggle")
        self.login("shop_admin").post(f"/assets/{self.asset}/todo/{self.todo_id}/confirm")
        self.assertEqual(self._todo()["done"], 1)

    def test_tech_cannot_confirm_their_own_task(self):
        c = self.login("tech")
        c.post(f"/assets/{self.asset}/todo/{self.todo_id}/toggle")
        r = c.post(f"/assets/{self.asset}/todo/{self.todo_id}/confirm")
        self.assertNotEqual(r.status_code, 200)
        self.assertEqual(self._todo()["done"], 0)
        self.assertIsNone(self._todo()["confirmed_by"])

    def test_send_back_clears_the_request_without_completing_it(self):
        self.login("tech").post(f"/assets/{self.asset}/todo/{self.todo_id}/toggle")
        self.login("inspector").post(f"/assets/{self.asset}/todo/{self.todo_id}/confirm", data={"action": "send_back"})
        t = self._todo()
        self.assertEqual(t["done"], 0)
        self.assertIsNone(t["confirm_requested_at"])
        self.assertIsNotNone(t["sent_back_at"])

    def test_reopening_a_confirmed_task_clears_everything(self):
        self.login("tech").post(f"/assets/{self.asset}/todo/{self.todo_id}/toggle")
        self.login("inspector").post(f"/assets/{self.asset}/todo/{self.todo_id}/confirm")
        self.login("shop_admin").post(f"/assets/{self.asset}/todo/{self.todo_id}/toggle")
        t = self._todo()
        self.assertEqual(t["done"], 0)
        self.assertIsNone(t["confirmed_by"])
        self.assertIsNone(t["completed_at"])
        self.assertIsNone(t["confirm_requested_at"])

    def test_my_tasks_page_shows_open_pending_and_completed_sections(self):
        c = self.login("tech")
        html = c.get("/my-tasks").get_data(as_text=True)
        self.assertIn("Swap tire", html)
        self.assertIn("Completed", html)
        self.assertIn("Nothing completed yet.", html)

        c.post(f"/assets/{self.asset}/todo/{self.todo_id}/toggle")
        html = c.get("/my-tasks").get_data(as_text=True)
        self.assertIn("Awaiting Confirmation", html)
        self.assertIn("waiting on an Inspector or admin to confirm", html)

        self.login("inspector").post(f"/assets/{self.asset}/todo/{self.todo_id}/confirm")
        html = c.get("/my-tasks").get_data(as_text=True)
        self.assertIn("Swap tire", html)
        self.assertNotIn("Nothing completed yet.", html)

    def test_asset_page_shows_confirm_and_send_back_for_admin_inspector(self):
        self.login("tech").post(f"/assets/{self.asset}/todo/{self.todo_id}/toggle")
        html = self.login("shop_admin").get(f"/assets/{self.asset}").get_data(as_text=True)
        self.assertIn("Awaiting confirmation", html)
        self.assertIn(f'/assets/{self.asset}/todo/{self.todo_id}/confirm', html)

    def test_asset_page_hides_confirm_buttons_from_plain_tech(self):
        self.login("tech").post(f"/assets/{self.asset}/todo/{self.todo_id}/toggle")
        html = self.login("tech").get(f"/assets/{self.asset}").get_data(as_text=True)
        self.assertIn("Awaiting confirmation", html)
        self.assertNotIn(f'/assets/{self.asset}/todo/{self.todo_id}/confirm', html)
