"""Idea 'Non-School Plane Rate ($/hr) re label to Students Aircraft and make
it so the amount can not be a negative number': the rate for instruction
given in a student's own plane (cfis.external_rate, billed via
_get_or_create_own_plane_asset - see flight.py/db.py) is labeled "Students
Aircraft Rate" to match the "Students Aircraft" wording used for the same
feature on Schedule a Flight, and can't be saved as a negative number.

QA ux-students-aircraft-settings moved editing this rate off the CFI's own
profile (Manage > CFIs > Edit) to Manage > School Settings, so it has one
home next to the Students Aircraft calendar color and every other CFI's
rate; the CFI edit form only shows it read-only now.
"""
from harness import OpsHubTestCase


class CfiStudentsAircraftRateTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]

    def _set(self, external_rate):
        return self.login("master").post("/flight/settings/school",
                                          data={f"external_rate_{self.cfi_id}": external_rate})

    def test_cfi_form_shows_the_students_aircraft_rate_read_only(self):
        html = self.login("master").get(f"/flight/cfis/{self.cfi_id}/edit").get_data(as_text=True)
        self.assertIn("Students Aircraft Rate ($/hr)", html)
        self.assertIn("Not set - uses the Instructor Rate", html)
        self.assertIn("Change in Manage", html)
        self.assertIn("School Settings", html)
        self.assertNotIn("Non-School Plane Rate", html)
        self.assertNotIn('name="external_rate"', html)

    def test_cfi_edit_no_longer_accepts_a_students_aircraft_rate(self):
        # Posting external_rate to the CFI edit form (its old field name) is
        # a no-op now - it only saves from School Settings.
        self.login("master").post(f"/flight/cfis/{self.cfi_id}/edit", data={
            "name": "Some CFI",
            "rate_per_hour": "50",
            "external_rate": "75",
        }, follow_redirects=True)
        row = self.q1("SELECT external_rate FROM cfis WHERE id = ?", (self.cfi_id,))
        self.assertIsNone(row["external_rate"])

    def test_negative_students_aircraft_rate_is_rejected(self):
        r = self._set("-15")
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT external_rate FROM cfis WHERE id = ?", (self.cfi_id,))
        self.assertIsNone(row["external_rate"])

    def test_positive_students_aircraft_rate_is_saved(self):
        self._set("75")
        row = self.q1("SELECT external_rate FROM cfis WHERE id = ?", (self.cfi_id,))
        self.assertEqual(row["external_rate"], 75.0)

    def test_zero_students_aircraft_rate_is_allowed(self):
        self._set("0")
        row = self.q1("SELECT external_rate FROM cfis WHERE id = ?", (self.cfi_id,))
        self.assertEqual(row["external_rate"], 0.0)

    def test_saved_rate_shows_read_only_on_the_cfi_form(self):
        self._set("55")
        html = self.login("master").get(f"/flight/cfis/{self.cfi_id}/edit").get_data(as_text=True)
        self.assertIn("$55.00/hr", html)
