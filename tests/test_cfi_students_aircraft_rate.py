"""Idea 'Non-School Plane Rate ($/hr) re label to Students Aircraft and make
it so the amount can not be a negative number': the CFI form's rate for
instruction given in a student's own plane (cfis.external_rate, billed via
_get_or_create_own_plane_asset - see flight.py/db.py) is labeled "Students
Aircraft Rate ($/hr)" to match the "Students Aircraft" wording used for the
same feature on Schedule a Flight, and can't be saved as a negative number.
"""
from harness import OpsHubTestCase


class CfiStudentsAircraftRateTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]

    def _edit(self, external_rate):
        return self.login("master").post(f"/flight/cfis/{self.cfi_id}/edit", data={
            "name": "Some CFI",
            "rate_per_hour": "50",
            "external_rate": external_rate,
        }, follow_redirects=True)

    def test_cfi_form_shows_the_students_aircraft_rate_label(self):
        html = self.login("master").get(f"/flight/cfis/{self.cfi_id}/edit").get_data(as_text=True)
        self.assertIn("Students Aircraft Rate ($/hr)", html)
        self.assertNotIn("Non-School Plane Rate", html)

    def test_negative_students_aircraft_rate_is_rejected(self):
        html = self._edit("-15").get_data(as_text=True)
        self.assertIn("negative number", html)
        row = self.q1("SELECT external_rate FROM cfis WHERE id = ?", (self.cfi_id,))
        self.assertIsNone(row["external_rate"])

    def test_positive_students_aircraft_rate_is_saved(self):
        self._edit("75")
        row = self.q1("SELECT external_rate FROM cfis WHERE id = ?", (self.cfi_id,))
        self.assertEqual(row["external_rate"], 75.0)

    def test_zero_students_aircraft_rate_is_allowed(self):
        self._edit("0")
        row = self.q1("SELECT external_rate FROM cfis WHERE id = ?", (self.cfi_id,))
        self.assertEqual(row["external_rate"], 0.0)
