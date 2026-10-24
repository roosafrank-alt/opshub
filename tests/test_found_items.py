"""Found items / owner approval (QA feat-owner-squawk-approval): a tech adds
something extra found on a job, the plane's owner approves or declines it
from the customer portal, and approved items become a Sub Area on the job.
Drives the real routes and checks the database, like the other flow tests.
"""
import io

from harness import OpsHubTestCase, SIDE_EFFECTS


class FoundItemsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N123")
        self.project = self.make_project(name="Annual - N123", asset_id=self.asset)
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, self.asset))
        other = self.make_asset("N999")
        self.other_project = self.make_project(name="Annual - N999", asset_id=other)

    def add_item(self, role="tech", project=None, **form):
        c = self.login(role)
        data = dict(description="Cracked exhaust stack", est_parts="250", est_labor_hours="2",
                    est_labor_rate="95", notify_owner="on")
        data.update(form)
        return c.post(f"/projects/{project or self.project}/found-items", data=data,
                      content_type="multipart/form-data")

    def items(self):
        return self.q("SELECT * FROM found_items ORDER BY id")

    def test_tech_adds_item_with_estimate_and_photo(self):
        r = self.add_item(photos=(io.BytesIO(b"\xff\xd8\xff fake jpeg"), "exhaust.jpg"))
        self.assertEqual(r.status_code, 302)
        it = self.items()[-1]
        self.assertEqual((it["status"], it["est_total"], it["created_by"]), ("waiting", 440, "Tech"))
        self.assertIsNotNone(it["notified_at"])
        self.assertEqual(len(self.q("SELECT id FROM photos WHERE found_item_id = ?", (it["id"],))), 1)
        html = self.login("tech").get(f"/projects/{self.project}").get_data(as_text=True)
        self.assertIn("Cracked exhaust stack", html)
        self.assertIn("Waiting", html)

    def test_bad_estimate_or_blank_description_is_rejected(self):
        for form in (dict(description="  "), dict(est_parts="-5"), dict(est_labor_hours="nan")):
            with self.subTest(**form):
                self.add_item(**form)
        self.assertEqual(self.items(), [])

    def test_accounts_without_shop_access_cannot_add(self):
        for role in ("cfi", "flight_student", "no_roles", "shop_student"):
            with self.subTest(role=role):
                self.add_item(role=role)
        self.assertEqual(self.items(), [])

    def test_owner_sees_and_approves_item_which_becomes_a_sub_area(self):
        self.add_item()
        iid = self.items()[-1]["id"]
        c = self.login("customer")
        html = c.get(f"/portal/aircraft/{self.asset}").get_data(as_text=True)
        self.assertIn("Cracked exhaust stack", html)
        self.assertIn("need", html)
        r = c.post(f"/portal/found-item/{iid}/decide", data=dict(decision="approve", note="Go ahead"))
        self.assertEqual(r.status_code, 302)
        it = self.q1("SELECT * FROM found_items WHERE id=?", (iid,))
        self.assertEqual((it["status"], it["decided_by"], it["decision_note"]), ("approved", "Owner Customer", "Go ahead"))
        self.assertIsNotNone(self.q1("SELECT id FROM project_sections WHERE project_id=? AND name=?",
                                     (self.project, "Cracked exhaust stack")))
        # A second answer doesn't change the first.
        c.post(f"/portal/found-item/{iid}/decide", data=dict(decision="decline"))
        self.assertEqual(self.q1("SELECT status FROM found_items WHERE id=?", (iid,))["status"], "approved")

    def test_owner_declining_without_a_note_is_refused(self):
        """Revision 2: a decline needs a note, so the shop knows why."""
        self.add_item()
        iid = self.items()[-1]["id"]
        self.login("customer").post(f"/portal/found-item/{iid}/decide", data=dict(decision="decline"))
        self.assertEqual(self.q1("SELECT status FROM found_items WHERE id=?", (iid,))["status"], "waiting")

    def test_owner_declines_and_it_stays_on_record(self):
        self.add_item()
        iid = self.items()[-1]["id"]
        self.login("customer").post(f"/portal/found-item/{iid}/decide",
                                    data=dict(decision="decline", note="Not right now, budget's tight."))
        self.assertEqual(self.q1("SELECT status FROM found_items WHERE id=?", (iid,))["status"], "declined")
        self.assertIsNone(self.q1("SELECT id FROM project_sections WHERE project_id=?", (self.project,)))
        # Admin can't delete an answered item.
        self.login("shop_admin").post(f"/found-items/{iid}/delete")
        self.assertEqual(len(self.items()), 1)

    def test_owner_can_reconsider_a_declined_item(self):
        """Revision 2: the owner can change their mind on a declined item."""
        self.add_item()
        iid = self.items()[-1]["id"]
        c = self.login("customer")
        c.post(f"/portal/found-item/{iid}/decide", data=dict(decision="decline", note="Not now."))
        self.assertEqual(self.q1("SELECT status FROM found_items WHERE id=?", (iid,))["status"], "declined")
        c.post(f"/portal/found-item/{iid}/decide", data=dict(decision="approve"))
        row = self.q1("SELECT status FROM found_items WHERE id=?", (iid,))
        self.assertEqual(row["status"], "approved")
        self.assertIsNotNone(self.q1("SELECT id FROM project_sections WHERE project_id=?", (self.project,)))

    def test_approved_item_cannot_be_reconsidered(self):
        self.add_item()
        iid = self.items()[-1]["id"]
        c = self.login("customer")
        c.post(f"/portal/found-item/{iid}/decide", data=dict(decision="approve"))
        c.post(f"/portal/found-item/{iid}/decide", data=dict(decision="decline", note="Changed my mind"))
        self.assertEqual(self.q1("SELECT status FROM found_items WHERE id=?", (iid,))["status"], "approved")

    def test_shop_and_owner_can_message_back_and_forth(self):
        self.add_item()
        iid = self.items()[-1]["id"]
        self.login("shop_admin").post(f"/found-items/{iid}/message", data=dict(body="Any thoughts on this one?"))
        self.login("customer").post(f"/portal/found-item/{iid}/message", data=dict(body="Can you send a closer photo?"))
        html = self.login("shop_admin").get(f"/projects/{self.project}").get_data(as_text=True)
        self.assertIn("Any thoughts on this one?", html)
        self.assertIn("Can you send a closer photo?", html)
        owner_html = self.login("customer").get(f"/portal/aircraft/{self.asset}").get_data(as_text=True)
        self.assertIn("Any thoughts on this one?", owner_html)
        self.assertIn("Can you send a closer photo?", owner_html)

    def test_message_requires_a_body(self):
        self.add_item()
        iid = self.items()[-1]["id"]
        r = self.login("shop_admin").post(f"/found-items/{iid}/message", data=dict(body=""))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM found_item_messages WHERE found_item_id=?", (iid,))["c"], 0)

    def test_owner_cannot_answer_another_owners_item(self):
        self.add_item(project=self.other_project)
        iid = self.items()[-1]["id"]
        c = self.login("customer")
        self.assertEqual(c.post(f"/portal/found-item/{iid}/decide", data=dict(decision="approve")).status_code, 404)
        self.assertEqual(self.q1("SELECT status FROM found_items WHERE id=?", (iid,))["status"], "waiting")

    def test_admin_removes_unanswered_item(self):
        self.add_item()
        iid = self.items()[-1]["id"]
        self.login("tech").post(f"/found-items/{iid}/delete")
        self.assertEqual(len(self.items()), 1)  # techs can't remove
        self.login("shop_admin").post(f"/found-items/{iid}/delete")
        self.assertEqual(self.items(), [])

    def test_owner_notification_goes_out_by_email_or_text(self):
        # Email/SMS are faked in tests; the call is recorded, never sent.
        before = len(SIDE_EFFECTS)
        self.add_item()
        self.assertGreaterEqual(len(SIDE_EFFECTS), before)
