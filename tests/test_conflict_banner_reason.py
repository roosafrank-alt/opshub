"""The red banner on the Schedule says exactly what's in conflict, and a
plain ?highlight= link (from Alerts / the Dashboard) isn't called a conflict."""
from datetime import date, timedelta
from urllib.parse import urlencode
from harness import OpsHubTestCase


def future(days=3):
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


class ConflictBannerReasonTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("cfi")

    def get_day(self, **params):
        q = urlencode([("view", "day"), ("date", future())] + [(k, v) for k, vs in params.items() for v in (vs if isinstance(vs, list) else [vs])])
        return self.client.get("/flight/schedule?" + q).get_data(as_text=True)

    def test_reason_shown_in_red_banner(self):
        html = self.get_day(highlight="5", why="Bernard Voss Pott is already booked at 3:30 PM (N22689, JD).")
        self.assertIn("Conflict:", html)
        self.assertIn("Bernard Voss Pott is already booked at 3:30 PM", html)

    def test_reason_with_no_block_to_outline_still_shows(self):
        html = self.get_day(highlight="0", why="Kate has time off on 10/20/2026.")
        self.assertIn("Kate has time off", html)

    def test_plain_highlight_is_not_called_a_conflict(self):
        html = self.get_day(highlight="5")
        self.assertNotIn("Conflict:", html)
        self.assertNotIn("in conflict", html)
        self.assertIn("Showing the flight you opened", html)

    def test_reason_is_escaped(self):
        html = self.get_day(highlight="5", why="<script>x</script>")
        self.assertNotIn("<script>x</script>", html)
