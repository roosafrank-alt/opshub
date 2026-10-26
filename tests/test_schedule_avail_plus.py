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
