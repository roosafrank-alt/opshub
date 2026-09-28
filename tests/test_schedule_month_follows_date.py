"""The Schedule's Month view opens on the month of the day being looked at
(?date=), not always today's month - stepping from Sep 30 to Oct 1 in the
Day view and pressing Month used to open September. An explicit year/month
still wins."""
from harness import OpsHubTestCase


class ScheduleMonthFollowsDateTest(OpsHubTestCase):
    def test_month_view_opens_on_the_dates_month(self):
        html = self.login("cfi").get("/flight/schedule?view=month&date=2031-03-15").get_data(as_text=True)
        self.assertIn("March 2031", html)

    def test_day_view_month_button_goes_to_that_days_month(self):
        html = self.login("cfi").get("/flight/schedule?view=day&date=2031-03-15").get_data(as_text=True)
        self.assertIn("view=month&amp;year=2031&amp;month=3", html)

    def test_explicit_year_and_month_still_win(self):
        html = self.login("cfi").get("/flight/schedule?view=month&year=2031&month=7&date=2031-03-15").get_data(as_text=True)
        self.assertIn("July 2031", html)
