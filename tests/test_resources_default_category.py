"""Resources opens on the rating a student is working on (still one tap from everything),
and the student account can say which rating that is."""
from harness import OpsHubTestCase
import db

GS = "/flight/groundschool"


class ResourcesDefaultCategoryTest(OpsHubTestCase):
    def add(self, title, cats):
        self.exec("INSERT INTO acs_resources (title, filename, doc_type, uploaded_at, categories) VALUES (?,?,?,?,?)",
                  (title, "x.pdf", "Advisory Circular", db.now_iso(), cats))

    def setUp(self):
        super().setUp()
        self.add("Private Book", "Private")
        self.add("Instrument Book", "Instrument")
        self.add("Sport Book", "Sport")

    def grant(self, name):
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("INSERT INTO acs_ratings (name, slug, uploaded_at, status) VALUES (?,?,?,'parsed')",
                  (name, name.lower().replace(" ", "-"), db.now_iso()))
        rid = self.q1("SELECT id FROM acs_ratings ORDER BY id DESC")["id"]
        self.exec("INSERT INTO academy_course_grants (student_id, rating_id, granted_by, granted_at) VALUES (?,?,?,?)",
                  (sid, rid, "test", db.now_iso()))
        return sid

    def test_migration_adds_training_goal(self):
        self.assertIn("training_goal", [r["name"] for r in self.q("PRAGMA table_info(students)")])
        db.init_db()

    def test_student_opens_on_goal_and_can_see_all(self):
        sid = self.grant("Private Pilot")
        self.exec("UPDATE students SET training_goal = 'sport' WHERE id = ?", (sid,))
        body = self.login("flight_student").get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn("<option selected>Sport</option>", body)
        self.assertIn("All categories", body)
        for t in ("Private Book", "Instrument Book", "Sport Book"):
            self.assertIn(t, body)  # whole library still in the page

    def test_no_goal_follows_certificate(self):
        sid = self.grant("Ground Basics")
        self.exec("UPDATE students SET training_goal = NULL, pilot_certificate = 'private' WHERE id = ?", (sid,))
        body = self.login("flight_student").get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn("<option selected>Instrument</option>", body)

    def test_granted_course_name_sets_default(self):
        sid = self.grant("Private Pilot")
        self.exec("UPDATE students SET training_goal = NULL, pilot_certificate = NULL WHERE id = ?", (sid,))
        body = self.login("flight_student").get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn("<option selected>Private</option>", body)

    def test_admin_has_no_default(self):
        body = self.login("master").get(f"{GS}/resources").get_data(as_text=True)
        self.assertNotIn("<option selected>", body)

    def test_student_form_saves_training_goal(self):
        sid = self.q1("SELECT id FROM students ORDER BY id")["id"]
        self.login("master").post(f"/flight/students/{sid}/edit", data={"name": "X Student", "email": "a@b.co", "phone": "5", "training_goal": "instrument", "active": "on"})
        self.assertEqual(self.q1("SELECT training_goal FROM students WHERE id = ?", (sid,))["training_goal"], "instrument")
        self.login("master").post(f"/flight/students/{sid}/edit", data={"name": "X Student", "email": "a@b.co", "phone": "5", "training_goal": "bogus", "active": "on"})
        self.assertIsNone(self.q1("SELECT training_goal FROM students WHERE id = ?", (sid,))["training_goal"])
