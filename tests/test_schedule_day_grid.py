"""The Schedule's Day view uses the Availability page's plane x time-slot
grid: open slots click through to a new booking, booked ones hold the flight."""
from datetime import date, timedelta
from harness import OpsHubTestCase


def future(days=3):
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


class ScheduleDayGridTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N300DG")
        self.exec("UPDATE assets SET is_flight_asset=1 WHERE id=?", (self.plane,))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id=?", (self.users["cfi"]["id"],))["id"]
        self.student = self.q1("SELECT id FROM students WHERE user_id=?", (self.users["flight_student"]["id"],))["id"]
        self.login("cfi")

    def day(self):
        return self.client.get(f"/flight/schedule?view=day&date={future()}").get_data(as_text=True)

    def test_open_slot_links_to_new_booking(self):
        html = self.day()
        self.assertIn("avail-grid-v", html)
        self.assertIn("N300DG", html)
        self.assertIn("avail-slot-open", html)
        self.assertRegex(html, r"/flight/schedule/new\?[^']*time=08(:|%3A)00")

    def test_booked_slot_holds_the_flight_and_continues_below(self):
        self.client.post("/flight/schedule/new", data=dict(asset_id=self.plane, student_id=self.student, cfi_id=self.cfi,
                                                         scheduled_date=future(), scheduled_time="08:00", duration_hours="3"))
        html = self.day()
        self.assertIn('data-plane="N300DG"', html)
        self.assertIn("avail-slot-cont", html)

    def test_day_view_lists_instructor_buttons_like_availability(self):
        html = self.day()
        self.assertIn('class="avail-cfi-side"', html)
        self.assertIn(f'data-cfi="{self.cfi}"', html)
        self.assertIn("avail-cfi-chips", html)

    def test_home_page_upcoming_uses_the_same_grid(self):
        self.client.post("/flight/schedule/new", data=dict(asset_id=self.plane, student_id=self.student, cfi_id=self.cfi,
                                                         scheduled_date=future(), scheduled_time="08:00", duration_hours="1.5"))
        html = self.client.get("/flight/", follow_redirects=True).get_data(as_text=True)
        self.assertIn("avail-grid-v", html)
        self.assertIn('data-plane="N300DG"', html)
