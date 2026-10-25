"""Idea "quick access to the availability page": on phones the Availability
button in the Schedule's day headers shrinks to a green plus in a circle."""
from harness import OpsHubTestCase


class AvailabilityPlusButtonTest(OpsHubTestCase):
    def test_button_has_phone_plus_icon(self):
        html = self.login("cfi").get("/flight/schedule?view=week").get_data(as_text=True)
        self.assertIn("avail-day-btn", html)
        self.assertIn("bi-plus-circle-fill avail-plus", html)
