"""QA fix ux-academy-log-first: the Flight Academy tab row is now one line
(Board, Logbook, Ground School, More - Resources joins the row when there's
room, else it lives under More with How points work), Logbook/Progress/Map
are sub-tabs inside Logbook instead of three separate top tabs, and Log Your
Flying sits right under the Flight Academy banner instead of below the
leaderboards. See _academy_tabs.html / _academy_logbook_subtabs.html."""
from harness import OpsHubTestCase


class AcademyTabsLayoutTest(OpsHubTestCase):
    def test_board_page_has_one_line_tab_row_with_more(self):
        c = self.login("master")
        body = c.get("/academy").get_data(as_text=True)
        self.assertIn('class="academy-tabs-row"', body)
        self.assertIn(">Board<", body)
        self.assertIn(">Ground School<", body)
        self.assertIn(">More<", body)
        self.assertIn("academy-tab-resources", body)
        # Progress/Map are no longer separate top-level tabs on the Board page.
        self.assertNotIn('href="/academy/progress"', body)
        self.assertNotIn('href="/academy/map"', body)

    def test_log_your_flying_is_right_under_the_banner(self):
        c = self.login("master")
        body = c.get("/academy").get_data(as_text=True)
        hero_pos = body.index("Flight Academy")
        log_pos = body.index("Log Your Flying")
        filters_pos = body.index("All Time")
        points_board_pos = body.index("bi-trophy\"></i> Points")
        self.assertLess(hero_pos, log_pos)
        self.assertLess(log_pos, filters_pos)
        self.assertLess(log_pos, points_board_pos)

    def test_how_points_work_is_a_modal_not_a_bottom_list(self):
        c = self.login("master")
        body = c.get("/academy").get_data(as_text=True)
        self.assertIn('id="howPointsModal"', body)
        self.assertIn("Flight hours: 10 each", body)
        self.assertNotIn('href="#how"', body)
        self.assertNotIn('id="how"', body)

    def test_logbook_page_shows_logbook_progress_map_subtabs(self):
        c = self.login("master")
        body = c.get("/academy/logbook").get_data(as_text=True)
        self.assertIn('class="nav nav-pills academy-subtabs', body)
        self.assertIn('href="/academy/progress"', body)
        self.assertIn('href="/academy/map"', body)
        self.assertIn('id="howPointsModal"', body)

    def test_progress_and_map_pages_show_the_same_subtabs(self):
        c = self.login("master")
        for path in ("/academy/progress", "/academy/map"):
            body = c.get(path).get_data(as_text=True)
            self.assertIn('class="nav nav-pills academy-subtabs', body)

    def test_ground_school_page_has_no_logbook_subtabs(self):
        c = self.login("master")
        body = c.get("/flight/groundschool").get_data(as_text=True)
        self.assertIn('class="academy-tabs-row"', body)
        self.assertNotIn('class="nav nav-pills academy-subtabs', body)
        self.assertIn('id="howPointsModal"', body)

    def test_resources_page_has_the_tabs_and_modal_too(self):
        c = self.login("master")
        body = c.get("/flight/groundschool/resources").get_data(as_text=True)
        self.assertIn('class="academy-tabs-row"', body)
        self.assertIn('id="howPointsModal"', body)
