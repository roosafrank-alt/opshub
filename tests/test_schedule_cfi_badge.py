"""On the Schedule, an instructor in the pilot seat shows a plain "CFI"
badge instead of their certificate tier; a regular student keeps theirs."""
from datetime import date, timedelta
from harness import OpsHubTestCase


def future(days=3):
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


class ScheduleCfiBadgeTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N300CB")
        self.exec("UPDATE assets SET is_flight_asset=1 WHERE id=?", (self.plane,))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id=?", (self.users["cfi"]["id"],))["id"]
        self.student = self.q1("SELECT id FROM students WHERE user_id=?", (self.users["flight_student"]["id"],))["id"]
        self.login("cfi")

    def book_and_view(self):
        self.client.post("/flight/schedule/new", data=dict(asset_id=self.plane, student_id=self.student, cfi_id=self.cfi,
                                                         scheduled_date=future(), scheduled_time="10:00", duration_hours="1.5"))
        return self.client.get(f"/flight/schedule?view=day&date={future()}").get_data(as_text=True)

    def test_regular_student_keeps_certificate_badge(self):
        self.exec("UPDATE students SET pilot_certificate='commercial', pilot_ratings='instrument' WHERE id=?", (self.student,))
        html = self.book_and_view()
        self.assertNotIn('title="Instructor"', html)

    def test_instructor_rating_shows_cfi(self):
        self.exec("UPDATE students SET pilot_certificate='commercial', pilot_ratings='instrument,cfi' WHERE id=?", (self.student,))
        html = self.book_and_view()
        self.assertIn('title="Instructor"', html)

    def test_linked_cfi_profile_shows_cfi(self):
        self.exec("UPDATE students SET pilot_certificate='atp', pilot_ratings='' WHERE id=?", (self.student,))
        self.exec("INSERT INTO cfis (name, username, password_hash, user_id, active) VALUES ('Second CFI', 'cfi2test', 'x', ?, 1)", (self.users["flight_student"]["id"],))
        html = self.book_and_view()
        self.assertIn('title="Instructor"', html)
