"""Schedule a flight: Start Time with an End Time picker beside it."""
from harness import OpsHubTestCase


class EndTimePicker(OpsHubTestCase):
    def test_form_has_start_and_end_time(self):
        html = self.login("cfi").get("/flight/schedule/new").get_data(as_text=True)
        self.assertIn("Start Time", html)
        self.assertIn('id="end-hour-display"', html)
        self.assertIn('id="end-ampm-down"', html)
        self.assertIn('name="duration_hours"', html)
