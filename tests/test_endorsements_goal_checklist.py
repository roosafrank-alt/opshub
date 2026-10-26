"""The Endorsements checklist opens on the student's own goal (e.g. Tailwheel), also for someone who
already holds an ATP - never on a certificate they already have."""
from harness import OpsHubTestCase


class EndorsementsGoalChecklistTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE users SET academy_access = 1 WHERE username = 'flight_student'")

    def page(self, **q):
        return self.login("flight_student").get("/academy/endorsements/", query_string=q).get_data(as_text=True)

    def test_atp_holder_working_toward_tailwheel_sees_the_tailwheel_checklist(self):
        self.exec("UPDATE students SET pilot_certificate = 'atp', training_goal = 'tailwheel' WHERE id = ?", (self.sid,))
        html = self.page()
        self.assertIn("Needed for Tailwheel Endorsement", html)
        self.assertNotIn("Needed for Airline Transport Pilot", html)

    def test_other_goals_have_a_checklist_too(self):
        self.exec("UPDATE students SET pilot_certificate = 'private', training_goal = 'complex' WHERE id = ?", (self.sid,))
        self.assertIn("Needed for Complex Endorsement", self.page())
