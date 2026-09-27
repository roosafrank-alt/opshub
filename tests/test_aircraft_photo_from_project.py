"""Idea "Photos for ID": an aircraft/asset with no photo of its own shows
its newest project's cover photo instead, on both the Aircraft list and the
Aircraft detail page, with a link back to that project. Display-only - the
aircraft picks up its own photo the moment one is added, and the project's
photos are never touched. See app.py's _asset_project_cover.
"""
from harness import OpsHubTestCase


class AircraftPhotoFromProjectTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N54321")
        self.project = self.make_project(name="Annual - N54321", asset_id=self.asset)

    def test_asset_with_no_photo_shows_project_cover_on_list_and_detail(self):
        self.exec("INSERT INTO photos (filename, project_id, is_cover) VALUES ('stack.jpg', ?, 1)",
                  (self.project,))
        c = self.login("shop_admin")

        list_html = c.get("/assets").get_data(as_text=True)
        self.assertIn("stack.jpg", list_html)

        detail_html = c.get(f"/assets/{self.asset}").get_data(as_text=True)
        self.assertIn("stack.jpg", detail_html)
        self.assertIn("Annual - N54321", detail_html)

    def test_asset_own_photo_takes_priority_over_project_cover(self):
        self.exec("INSERT INTO photos (filename, project_id, is_cover) VALUES ('stack.jpg', ?, 1)",
                  (self.project,))
        self.exec("INSERT INTO photos (filename, asset_id, is_cover) VALUES ('plane.jpg', ?, 1)",
                  (self.asset,))
        c = self.login("shop_admin")

        detail_html = c.get(f"/assets/{self.asset}").get_data(as_text=True)
        self.assertIn("plane.jpg", detail_html)
        self.assertNotIn("stack.jpg", detail_html)

    def test_asset_with_neither_photo_shows_no_project_cover(self):
        c = self.login("shop_admin")
        list_html = c.get("/assets").get_data(as_text=True)
        self.assertNotIn("stack.jpg", list_html)
