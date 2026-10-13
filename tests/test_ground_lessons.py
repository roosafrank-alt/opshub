"""GS-LESSONS: a Ground School Task shows its lesson plan (ground_lessons/<code>.json)
with citations linked to the uploaded resource pages."""
import json
import os

from harness import OpsHubTestCase
import db
import ground_lessons

GS = "/flight/groundschool"


class GroundLessonsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        conn = db.get_db()
        rid = conn.execute("INSERT INTO acs_ratings (name, slug, uploaded_at, status) VALUES (?,?,?,?)",
                           ("Private Pilot", "ppl", db.now_iso(), "parsed")).lastrowid
        area = conn.execute("INSERT INTO acs_areas (rating_id, code, title, order_index) VALUES (?,?,?,1)",
                            (rid, "I", "Preflight Preparation")).lastrowid
        self.task = conn.execute("INSERT INTO acs_tasks (area_id, code, title, acs_references, order_index) "
                                 "VALUES (?,?,?,?,1)", (area, "A", "Pilot Qualifications", "14 CFR part 61")).lastrowid
        conn.execute("INSERT INTO acs_task_elements (task_id, kind, code, text, order_index) VALUES (?,?,?,?,1)",
                     (self.task, "knowledge", "PA.I.A.K1", "Certification requirements."))
        self.other = conn.execute("INSERT INTO acs_tasks (area_id, code, title, acs_references, order_index) "
                                  "VALUES (?,?,?,?,2)", (area, "Z", "No lesson yet", "")).lastrowid
        conn.execute("INSERT INTO acs_task_elements (task_id, kind, code, text, order_index) VALUES (?,?,?,?,1)",
                     (self.other, "knowledge", "PA.I.Z.K1", "Nothing."))
        conn.commit()
        conn.close()

    def test_task_code_and_shipped_lessons_are_valid(self):
        self.assertEqual(ground_lessons.task_code(["PA.I.A.K1", "PA.I.A.R1"]), "PA.I.A")
        self.assertEqual(ground_lessons.task_code(["K1"]), None)
        files = [f for f in os.listdir(ground_lessons.LESSON_DIR) if f.endswith(".json")]
        self.assertTrue(files)
        static = os.path.join(os.path.dirname(ground_lessons.LESSON_DIR), "static", "lessons")
        for f in files:
            with open(os.path.join(ground_lessons.LESSON_DIR, f), encoding="utf-8") as fh:
                L = json.load(fh)
            self.assertEqual(L["task_code"] + ".json", f)
            self.assertTrue(L["chunks"])
            for q in L.get("quiz", []):
                self.assertTrue(0 <= q["answer"] < len(q["options"]), f)
            for fg in L.get("figures", {}).values():
                self.assertTrue(os.path.exists(os.path.join(static, fg["file"])), fg["file"])

    def test_lesson_shows_on_task_page_with_linked_citations(self):
        body = self.login("master").get(f"{GS}/task/{self.task}").get_data(as_text=True)
        self.assertIn('id="gsl"', body)
        self.assertIn("Pre-reading", body)
        self.assertIn(f"{GS}/resources/35/excerpt?pages=", body)
        self.assertIn("Lesson Content", body)          # the editable box is still there

    def test_rich_escapes_html(self):
        with self.app.test_request_context():
            out = str(ground_lessons.rich("<b>x</b> [[1:2|PHAK 1-2]] **bold**"))
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", out)
        self.assertIn("/resources/1/excerpt?pages=2", out)
        self.assertIn("<b>bold</b>", out)

    def test_task_without_lesson_has_no_lesson_block(self):
        body = self.login("master").get(f"{GS}/task/{self.other}").get_data(as_text=True)
        self.assertNotIn('id="gsl"', body)
