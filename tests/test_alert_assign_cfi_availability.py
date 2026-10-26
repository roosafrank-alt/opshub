"""Idea "lesson without an instructor assigned": the Resolve > Assign CFI
button on a flagged booking opens the Schedule day view for that day with
the booking highlighted."""
from test_fly_preview import FlyBase


class AlertAssignCfiScheduleDayTest(FlyBase):
    def test_assign_cfi_link_goes_to_schedule_day_highlighted(self):
        sid = self.book(cfi=None, needs_review=1, review_reason="No instructor assigned yet")
        html = self.login("cfi").get("/flight/alerts").get_data(as_text=True)
        self.assertIn("Assign CFI", html)
        self.assertIn("view=day", html)
        self.assertIn("date=%s" % self.day.isoformat(), html)
        self.assertIn("highlight=%d" % sid, html)
