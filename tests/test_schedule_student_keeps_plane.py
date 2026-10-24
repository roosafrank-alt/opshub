"""Schedule a flight: picking a student's usual lesson must not swap out a plane
already picked; the usual plane is offered as a 'use it' link instead."""
from harness import OpsHubTestCase


class StudentKeepsPlane(OpsHubTestCase):
    def test_form_keeps_picked_plane_and_offers_usual(self):
        html = self.login("cfi").get("/flight/schedule/new").get_data(as_text=True)
        self.assertIn("the plane you picked (usually", html)
        self.assertIn('id="use-usual-plane"', html)
        self.assertIn("planeSel.value !== autoPlane", html)
