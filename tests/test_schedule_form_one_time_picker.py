"""QA fix ux-book-form-one-time-picker: the schedule form keeps one set of
time buttons (Quick pick); with a plane picked it shows that plane's open
times and crosses out taken ones, instead of listing times twice."""
from harness import OpsHubTestCase


class ScheduleFormOneTimePickerTest(OpsHubTestCase):
    def test_form_has_open_times_line_and_taken_style(self):
        c = self.login("flight_admin")
        html = c.get("/flight/schedule/new").get_data(as_text=True)
        self.assertIn('id="open-times-line"', html)
        self.assertIn("Open times for ", html)
        self.assertIn("qt-taken", html)
        self.assertIn("Also open: ", html)
