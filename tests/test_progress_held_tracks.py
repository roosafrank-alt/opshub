"""Progress: what the profile says you hold shows complete; Sport / Recreational only for those who want them."""
from harness import OpsHubTestCase


class HeldTracksTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE users SET academy_access = 1 WHERE username = 'flight_student'")

    def page(self):
        return self.login("flight_student").get("/academy/progress").get_data(as_text=True)

    def test_atp_holder_sees_held_tracks_complete_and_no_sport_or_rec(self):
        self.exec("UPDATE students SET pilot_certificate = 'atp', pilot_ratings = 'instrument,multi_engine', training_goal = 'tailwheel' WHERE id = ?", (self.sid,))
        html = self.page()
        for name in ("Private Pilot", "Instrument Rating", "Commercial Pilot", "Airline Transport Pilot"):
            self.assertIn(name, html)
        self.assertIn("Held - on your profile", html)
        self.assertNotIn("Sport Pilot", html)
        self.assertNotIn("Recreational Pilot", html)

    def test_sport_shown_when_it_is_the_goal(self):
        self.exec("UPDATE students SET pilot_certificate = 'student', pilot_ratings = '', training_goal = 'sport' WHERE id = ?", (self.sid,))
        self.assertIn("Sport Pilot", self.page())
        self.assertNotIn("Held - on your profile", self.page())
