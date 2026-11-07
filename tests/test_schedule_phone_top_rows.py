"""QA fix ux-schedule-phone-top-rows: on a phone the Schedule no longer spends
two rows on "Add to my phone calendar" and "Expand". The calendar link sits in
the More menu and Expand is an icon button beside More; desktop keeps both."""
from harness import OpsHubTestCase


class SchedulePhoneTopRowsTest(OpsHubTestCase):
    def test_phone_layout(self):
        for who in ("cfi", "flight_student"):
            html = self.login(who).get("/flight/schedule?view=day").get_data(as_text=True)
            self.assertIn("d-none d-sm-inline-block", html.split('id="phone-cal-btn"')[0].rsplit("<a", 1)[1])
            self.assertEqual(html.count("Add to my phone calendar"), 2)  # desktop button + More item
            self.assertIn("sch-fit-icon", html)
            self.assertIn('id="sch-fit-btn" class="btn btn-sm btn-outline-secondary float-end ms-2 d-none d-sm-inline-block"', html)
