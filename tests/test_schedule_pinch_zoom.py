"""Idea "Calendar": Frank asked that pinching in on the Schedule calendar
(or Ctrl+scroll on a computer) zoom in and make each lesson tile bigger and
easier to read. Every Fly with Kate page sets maximum-scale=1 in its
viewport meta on purpose (it's what stops a tapped input field from
jumping the whole page - see the QA fix that made phone inputs 16px), which
also blocks a finger-pinch from enlarging anything. The Schedule page now
overrides that one meta tag (see the `viewport` block in base_flight.html)
so a pinch/zoom can make its tiles bigger; every other flight page keeps
the original guard."""
import re

from harness import OpsHubTestCase


def _viewport_meta(html):
    m = re.search(r'<meta name="viewport"[^>]*>', html)
    return m.group(0) if m else None


class SchedulePinchZoomTest(OpsHubTestCase):
    def test_schedule_page_allows_zooming_in(self):
        c = self.login("cfi")
        for view in ("day", "week", "month"):
            meta = _viewport_meta(c.get(f"/flight/schedule?view={view}").get_data(as_text=True))
            self.assertIsNotNone(meta)
            self.assertNotIn("maximum-scale=1", meta)

    def test_other_flight_pages_still_block_zoom(self):
        c = self.login("cfi")
        for path in ("/flight/dashboard", "/flight/schedule/availability"):
            meta = _viewport_meta(c.get(path).get_data(as_text=True))
            self.assertIsNotNone(meta)
            self.assertIn("maximum-scale=1", meta)
