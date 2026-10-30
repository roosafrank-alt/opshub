"""The Academy tour must stay 'seen' after signing out and in again (it used
to be forgotten at login, unlike the shop and Flight School tours)."""
from harness import OpsHubTestCase, PASSWORD, flask_app


class AcademyTourStaysSeenTest(OpsHubTestCase):
    def _real_login(self, role):
        client = flask_app.test_client()
        client.post("/", data={"username": role, "password": PASSWORD})
        return client

    def test_seen_flag_survives_a_new_sign_in(self):
        uid = self.users["master"]["id"]
        self.exec("UPDATE users SET tour_seen_academy = 1 WHERE id = ?", (uid,))
        client = self._real_login("master")
        with client.session_transaction() as s:
            self.assertTrue(s.get("tour_seen_academy"))
        self.assertNotIn("OpsHubTour.start", client.get("/academy").get_data(as_text=True))

    def test_unseen_tour_still_shows(self):
        uid = self.users["master"]["id"]
        self.exec("UPDATE users SET tour_seen_academy = 0 WHERE id = ?", (uid,))
        client = self._real_login("master")
        self.assertIn("OpsHubTour.start", client.get("/academy").get_data(as_text=True))
