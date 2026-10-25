"""Idea "Schedule a Flight page": after a student or instructor is picked, the
box drops its focus so the mouse wheel can't change the name by accident."""
from harness import OpsHubTestCase


class PickerBlurTest(OpsHubTestCase):
    def test_pickers_blur_after_choosing(self):
        html = self.login("cfi").get("/flight/schedule/new").get_data(as_text=True)
        self.assertIn("cfi.blur()", html)
        self.assertIn("input.blur();  // leave the box un-highlighted", html)
