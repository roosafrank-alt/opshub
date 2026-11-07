"""QA fix ux-gs-builtin-courses-box: an installed built-in course is one short
line (name, Installed tag, About link, visible grey Update button); a course
not added yet keeps its description and blue Add course button."""
from harness import OpsHubTestCase

GS = "/flight/groundschool"


class BuiltinCompactTest(OpsHubTestCase):
    def test_installed_course_is_one_line_with_visible_update(self):
        c = self.login("master")
        before = c.get(GS).get_data(as_text=True)
        self.assertIn("Add course", before)
        self.assertNotIn("btn-outline-light fw-bold", before)
        self.assertEqual(c.post(f"{GS}/builtin/FII/install").status_code, 302)
        body = c.get(GS).get_data(as_text=True)
        self.assertIn("gs-builtin-installed", body)
        self.assertIn(">Installed<", body)
        self.assertIn("btn-secondary fw-bold", body)
        self.assertIn("Update", body)
        self.assertIn('data-bs-toggle="collapse"', body)
        self.assertNotIn("btn-outline-light fw-bold", body)
