"""Schedule a flight: Start Time with an End Time picker beside it."""
from harness import OpsHubTestCase


class EndTimePicker(OpsHubTestCase):
    def test_form_has_start_and_end_time(self):
        html = self.login("cfi").get("/flight/schedule/new").get_data(as_text=True)
        self.assertIn('<label class="form-label">Time</label>', html)
        self.assertIn('id="end-hour-display"', html)
        self.assertIn('id="end-ampm-down"', html)
        self.assertIn('name="duration_hours"', html)

    def test_start_and_end_labelled_on_one_row(self):
        html = self.login("cfi").get("/flight/schedule/new").get_data(as_text=True)
        self.assertIn('<div class="tp-label">Start</div>', html)
        self.assertIn('<div class="tp-label">End</div>', html)
        self.assertIn('flex-nowrap', html)
        self.assertLess(html.index('id="time-picker"'), html.index('id="end-picker"'))

    def test_time_section_order(self):
        html = self.login("cfi").get("/flight/schedule/new").get_data(as_text=True)
        order = [html.index(k) for k in ('id="flight-presets"', 'id="quick-time-buttons"',
                                         'id="time-picker"', 'id="len-text"', 'id="end-picker"')]
        self.assertEqual(order, sorted(order))
        self.assertNotIn('Duration (hrs)', html)
