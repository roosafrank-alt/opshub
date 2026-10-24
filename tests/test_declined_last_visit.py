"""Declined last visit (QA feat-declined-last-visit): items an owner declined
on an earlier job show on a new job for the same plane, with Offer again and
Add to this job."""
import io

from harness import OpsHubTestCase


class DeclinedLastVisitTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N123")
        self.old = self.make_project(name="Annual - N123", asset_id=self.asset, status="completed")
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, self.asset))
        c = self.login("tech")
        c.post(f"/projects/{self.old}/found-items", data=dict(
            description="Right main brake disc worn", est_parts="300", est_labor_hours="1", est_labor_rate="100",
            photos=(io.BytesIO(b"\xff\xd8\xff fake"), "disc.jpg")), content_type="multipart/form-data")
        self.item = self.q("SELECT id FROM found_items")[-1]["id"]
        self.login("customer").post(f"/portal/found-item/{self.item}/decide",
                                    data=dict(decision="decline", note="Do it at the next annual"))
        self.new = self.make_project(name="100 hour - N123", asset_id=self.asset)

    def page(self, pid=None, role="tech"):
        return self.login(role).get(f"/projects/{pid or self.new}").get_data(as_text=True)

    def test_box_shows_on_new_job_only(self):
        html = self.page()
        self.assertIn("Declined last visit", html)
        self.assertIn("Right main brake disc worn", html)
        self.assertIn("$400.00", html)
        self.assertIn("1 photo", html)
        self.assertIn("Do it at the next annual", html)
        self.assertNotIn("Declined last visit", self.page(self.old))

    def test_no_box_when_nothing_declined(self):
        other = self.make_project(name="Other", asset_id=self.make_asset("N999"))
        self.assertNotIn("Declined last visit", self.page(other))

    def test_offer_again_copies_item_and_leaves_box(self):
        r = self.login("tech").post(f"/projects/{self.new}/declined/{self.item}/offer")
        self.assertEqual(r.status_code, 302)
        rows = self.q("SELECT * FROM found_items WHERE project_id = ?", (self.new,))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["status"], rows[0]["est_total"]), ("waiting", 400))
        self.assertEqual(len(self.q("SELECT id FROM photos WHERE found_item_id = ?", (rows[0]["id"],))), 1)
        self.assertNotIn("Declined last visit", self.page())
        # a second decline puts it back for the next visit
        self.login("customer").post(f"/portal/found-item/{rows[0]['id']}/decide", data=dict(decision="decline", note="Still no"))
        third = self.make_project(name="Third", asset_id=self.asset)
        self.assertIn("Declined last visit", self.page(third))
        self.assertNotIn("Declined last visit", self.page())

    def test_add_to_this_job_makes_discrepancy(self):
        self.login("tech").post(f"/projects/{self.new}/declined/{self.item}/add")
        sec = self.q("SELECT name FROM project_sections WHERE project_id = ?", (self.new,))
        self.assertEqual([s["name"] for s in sec], ["Right main brake disc worn"])
        self.assertNotIn("Declined last visit", self.page())
        # can't be used twice
        self.login("tech").post(f"/projects/{self.new}/declined/{self.item}/add")
        self.assertEqual(len(self.q("SELECT name FROM project_sections WHERE project_id = ?", (self.new,))), 1)
