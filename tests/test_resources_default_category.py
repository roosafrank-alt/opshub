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


class AdminAccountTrainingGoalTest(OpsHubTestCase):
    def test_admin_account_page_edits_every_student_detail(self):
        uid = self.users["flight_student"]["id"]
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (uid,))["id"]
        c = self.login("master")
        body = c.get(f"/admin/users/{uid}/edit").get_data(as_text=True)
        for t in ('name="training_goal"', "Sport Pilot", 'name="pilot_certificate"', 'name="rating_instrument"',
                  'name="medical_class"', 'name="first_solo_date"', 'name="rate_override"', 'name="pay_preference"',
                  'name="solo_currency_days"', 'name="notify_booking_confirm"'):
            self.assertIn(t, body)
        user = self.q1("SELECT * FROM users WHERE id = ?", (uid,))
        data = {"name": user["name"], "active": "on", "flight_roles": user["flight_roles"] or "student",
                "student_fields": "1", "training_goal": "sport", "pilot_certificate": "sport",
                "rating_instrument": "on", "medical_class": "third", "medical_expires": "2030-01-01",
                "first_solo_date": "2026-05-01", "rate_override": "55", "plane_rate_override": "130",
                "pay_preference": "Card", "solo_currency_days": "45", "notify_booking_confirm": "on"}
        c.post(f"/admin/users/{uid}/edit", data=data)
        r = self.q1("SELECT * FROM students WHERE id = ?", (sid,))
        self.assertEqual((r["training_goal"], r["pilot_certificate"], r["pilot_ratings"], r["medical_class"],
                          r["first_solo_date"], r["rate_override"], r["plane_rate_override"], r["pay_preference"],
                          r["solo_currency_days"], r["notify_booking_confirm"]),
                         ("sport", "sport", "instrument", "third", "2026-05-01", 55.0, 130.0, "Card", 45, 1))
        c.post(f"/admin/users/{uid}/edit", data={**data, "training_goal": "", "rating_instrument": ""})
        r = self.q1("SELECT * FROM students WHERE id = ?", (sid,))
        self.assertIsNone(r["training_goal"])
        self.assertIsNone(r["pilot_ratings"])

    def test_saving_without_the_section_leaves_student_alone(self):
        uid = self.users["flight_student"]["id"]
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (uid,))["id"]
        self.exec("UPDATE students SET training_goal = 'private' WHERE id = ?", (sid,))
        user = self.q1("SELECT * FROM users WHERE id = ?", (uid,))
        self.login("master").post(f"/admin/users/{uid}/edit", data={"name": user["name"], "active": "on",
                                  "flight_roles": user["flight_roles"] or "student"})
        self.assertEqual(self.q1("SELECT training_goal FROM students WHERE id = ?", (sid,))["training_goal"], "private")
