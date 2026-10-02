"""Idea "Calander": Frank attached a competing scheduler's screenshot with
an hourly reference mark on every hour and asked for the Schedule's Day,
Week and Month views to match. Day/Week already labeled every hour; Month's
mini per-day timeline only had 4 sparse marks (9a/12p/3p/6p). It now has
one mark per hour too, over the school-day window (6 AM-9 PM unless School
Settings changed it), just in Month's existing
compact "9a" label style since a month cell has no room for "9 AM"."""
from harness import OpsHubTestCase


class CalendarHourlyMarksTest(OpsHubTestCase):
    def test_month_view_shows_an_hourly_mark_for_every_hour_in_the_window(self):
        c = self.login("cfi")
        body = c.get("/flight/schedule?view=month").get_data(as_text=True)
        # 6 AM through 9 PM inclusive = 16 marks per in-month day cell, one
        # set of marks per day, so the total is always a multiple of 16 -
        # it used to be a multiple of 4 (9a/12p/3p/6p only). FLY-37: the
        # window is the same one the Day/Week views and Availability use.
        label_count = body.count('class="calendar-time-label"')
        self.assertGreater(label_count, 0)
        self.assertEqual(label_count % 16, 0)

    def test_month_view_includes_labels_that_were_missing_before(self):
        c = self.login("cfi")
        body = c.get("/flight/schedule?view=month").get_data(as_text=True)
        for label in (">6a<", ">7a<", ">8a<", ">10a<", ">11a<", ">1p<", ">2p<", ">4p<", ">5p<", ">7p<", ">8p<", ">9p<"):
            self.assertIn(label, body)

    def test_day_and_week_views_are_unaffected(self):
        c = self.login("cfi")
        day_body = c.get("/flight/schedule?view=day").get_data(as_text=True)
        week_body = c.get("/flight/schedule?view=week").get_data(as_text=True)
        self.assertIn("day-timeline-hour-label", day_body)
        self.assertIn("day-timeline-hour-label", week_body)
