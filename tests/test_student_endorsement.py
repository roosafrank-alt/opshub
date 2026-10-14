"""Idea 'Write endorsement': the student profile's Solo sign-off tile has a
button that opens a pop-up with six endorsements filled in to copy or print;
solo ones save today as the solo sign-off date (expiry +90 days)."""
from datetime import date, timedelta
from harness import OpsHubTestCase


class StudentEndorsementTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]

    def test_profile_has_button_and_popup(self):
        html = self.login("master").get(f"/flight/students/{self.sid}/edit").get_data(as_text=True)
        self.assertIn("Write endorsement", html)
        self.assertIn('id="endorse-modal"', html)
        self.assertIn("61.87(n)", html)

    def test_save_sets_date_and_90_day_expiry(self):
        r = self.login("master").post(f"/flight/students/{self.sid}/solo-signoff/save")
        self.assertTrue(r.get_json()["ok"])
        row = self.q1("SELECT solo_signoff_date, solo_signoff_expires FROM students WHERE id = ?", (self.sid,))
        self.assertEqual(row["solo_signoff_date"], date.today().isoformat())
        self.assertEqual(row["solo_signoff_expires"], (date.today() + timedelta(days=90)).isoformat())

    def test_student_cannot_save(self):
        r = self.login("flight_student").post(f"/flight/students/{self.sid}/solo-signoff/save")
        self.assertNotEqual(r.status_code, 200)
        self.assertIsNone(self.q1("SELECT solo_signoff_date FROM students WHERE id = ?", (self.sid,))["solo_signoff_date"])
