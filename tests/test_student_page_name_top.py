"""QA fix: a student's page opens with the student's name, a short summary line
and an Edit details button; the form below is headed Details."""
from harness import OpsHubTestCase


class StudentPageNameTopTest(OpsHubTestCase):
    def test_name_first_then_details_form(self):
        s = self.q1("SELECT id, name FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))
        html = self.login("master").get(f"/flight/students/{s['id']}/edit").get_data(as_text=True)
        top = html.index('<h1 class="h4 mb-0">')
        self.assertIn(s["name"], html[top:top + 200])
        self.assertLess(top, html.index("Lessons &amp; Landings"))
        self.assertIn('href="#student-details"', html)
        self.assertIn('id="student-details"', html)
        self.assertIn("balance $", html)
        self.assertNotIn(">Edit Student<", html)
