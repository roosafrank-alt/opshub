"""Idea "quick access to the availability page": on phones the Availability
button in the Schedule's day headers shrinks to a green plus in a circle."""
from harness import OpsHubTestCase


class AvailabilityPlusButtonTest(OpsHubTestCase):
    def test_button_has_phone_plus_icon(self):
        html = self.login("cfi").get("/flight/schedule?view=week").get_data(as_text=True)
        self.assertIn("avail-day-btn", html)
        self.assertIn("bi-plus-circle-fill avail-plus", html)


class MonthDayClickTest(OpsHubTestCase):
    """Idea "quick access to the availability page" (revision): a click anywhere
    on a Month-view day opens that day's Availability page, for every login."""
    def test_month_cells_point_at_availability(self):
        for role in ("cfi", "flight_admin"):
            html = self.login(role).get("/flight/schedule?view=month").get_data(as_text=True)
            self.assertIn('data-add-url="/flight/schedule/availability?date=', html, role)
            self.assertNotIn('data-add-url="/flight/schedule/new', html, role)


class WeekDayClickTest(OpsHubTestCase):
    """Idea "quick access to the availability page" (revision 3): Week and Day
    views open that day's Availability page from a click on the day."""
    def test_week_and_day_point_at_availability(self):
        for role in ("cfi", "flight_admin"):
            c = self.login(role)
            wk = c.get("/flight/schedule?view=week").get_data(as_text=True)
            self.assertIn('class="week-timeline-day-col day-click-avail" data-avail-url="/flight/schedule/availability?date=', wk, role)
            dy = c.get("/flight/schedule?view=day").get_data(as_text=True)
            self.assertIn('day-click-avail" data-avail-url="/flight/schedule/availability?date=', dy, role)
