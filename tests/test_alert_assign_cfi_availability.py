"""Idea "lesson without an instructor assigned": the Resolve > Assign CFI
button on a flagged booking opens the Availability page for that day, where
each instructor shows as free or busy."""
from test_fly_preview import FlyBase


class AlertAssignCfiAvailabilityTest(FlyBase):
    def test_assign_cfi_link_goes_to_availability_for_that_day(self):
        sid = self.book(cfi=None, needs_review=1, review_reason="No instructor assigned yet")
        html = self.login("cfi").get("/flight/alerts").get_data(as_text=True)
        self.assertIn("Assign CFI", html)
        self.assertIn("/flight/schedule/availability?date=%s" % self.day.isoformat(), html)
        self.assertNotIn("/flight/schedule/%d/edit" % sid, html.split("Assign CFI")[0][-400:] + html.split("Assign CFI")[1][:50])

    def test_availability_page_for_that_day_loads(self):
        r = self.login("cfi").get("/flight/schedule/availability?date=%s" % self.day.isoformat())
        self.assertEqual(r.status_code, 200)
