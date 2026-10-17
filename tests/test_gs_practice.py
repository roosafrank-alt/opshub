"""GS-PRACTICE / GS-AREA-REVIEW: hand-written games keep their own keys (so the automatic games'
saved progress stays put), a lesson's own final_quiz saves under its own keys, and the review quiz
at the end of each Area is graded and saved like a Final check."""
import json
import os
import shutil
import tempfile

from harness import OpsHubTestCase
import db
import ground_lessons

GS = "/flight/groundschool"

REVIEW = {
    "area": "PA.I", "title": "Area I review", "intro": "Mixed questions.",
    "questions": [
        {"task": "PA.I.E", "q": "Q%d?" % i, "options": ["a", "b", "c", "d"], "answer": i % 4,
         "why": "Because [[1:2|PHAK 1-2]]."} for i in range(1, 8)
    ],
}


class ActivityKeysTest(OpsHubTestCase):
    def test_hand_written_keys_do_not_shift_auto_game_keys(self):
        terms = [{"term": "T%d" % i, "def": "D%d" % i} for i in range(5)]
        before = ground_lessons.activities({"key_terms": terms})
        self.assertEqual([a["key"] for a in before], ["a0", "a1"])
        after = ground_lessons.activities({"key_terms": terms, "activities": [
            {"type": "tf", "key": "h1", "title": "x", "cards": []},
            {"type": "match", "key": "h2", "title": "y", "pairs": []}]})
        self.assertEqual([a["key"] for a in after], ["h1", "h2", "a0", "a1"])
        self.assertEqual([a["title"] for a in after if a["key"] == "a1"], [before[1]["title"]])

    def test_lessons_without_own_keys_keep_old_numbering(self):
        L = ground_lessons.load("PA.I.A")
        self.assertEqual([a["key"] for a in ground_lessons.activities(L)], ["a%d" % i for i in range(6)])

    def test_shipped_new_content_is_well_formed(self):
        for f in sorted(os.listdir(ground_lessons.LESSON_DIR)):
            if not f.endswith(".json"):
                continue
            L = ground_lessons.load(f[:-5])
            n = len(L["chunks"])
            own = [a for a in L.get("activities") or [] if a.get("key")]
            for a in own:
                self.assertRegex(a["key"], r"^h\d+$", f)
                self.assertTrue(1 <= a["after"] < n, f)
            for q in L.get("final_quiz") or []:
                self.assertEqual(len(q["options"]), 4, f)
                self.assertTrue(0 <= q["answer"] < 4, f)
                self.assertIn("[[", q["why"], f)
                if q.get("figure"):
                    self.assertIn(q["figure"], L["figures"], f)
        adir = ground_lessons.AREA_DIR
        for f in sorted(os.listdir(adir)) if os.path.isdir(adir) else []:
            R = ground_lessons.load_area(f[:-5])
            self.assertEqual(R["area"] + ".json", f)
            for q in R["questions"]:
                self.assertEqual(len(q["options"]), 4, f)
                self.assertTrue(0 <= q["answer"] < 4, f)
                self.assertTrue(ground_lessons.load(q["task"]), f)
                if q.get("figure"):
                    self.assertTrue(ground_lessons.area_figure(q["figure"]), f)

    def test_final_prefix(self):
        self.assertEqual(ground_lessons.final_prefix({"quiz": [1]}), "fq")
        self.assertEqual(ground_lessons.final_prefix({"quiz": [1], "final_quiz": [1]}), "fz")


class PracticeFlowTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        conn = db.get_db()
        self.rid = conn.execute("INSERT INTO acs_ratings (name, slug, uploaded_at, status) VALUES (?,?,?,?)",
                                ("Private Pilot", "ppl-pr", db.now_iso(), "parsed")).lastrowid
        self.area = conn.execute("INSERT INTO acs_areas (rating_id, code, title, order_index) VALUES (?,?,?,1)",
                                 (self.rid, "I", "Preflight Preparation")).lastrowid
        self.task = conn.execute("INSERT INTO acs_tasks (area_id, code, title, acs_references, order_index) "
                                 "VALUES (?,?,?,?,1)", (self.area, "E", "National Airspace System", "")).lastrowid
        conn.execute("INSERT INTO acs_task_elements (task_id, kind, code, text, order_index) VALUES (?,?,?,?,1)",
                     (self.task, "knowledge", "PA.I.E.K1", "Airspace classes."))
        conn.execute("INSERT INTO academy_course_grants (student_id, rating_id, granted_by, granted_at) VALUES (?,?,?,?)",
                     (self.sid, self.rid, "test", db.now_iso()))
        conn.commit()
        conn.close()
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, "PA.I.json"), "w") as f:
            json.dump(REVIEW, f)
        self._old_dir = ground_lessons.AREA_DIR
        ground_lessons.AREA_DIR = self.tmp
        ground_lessons._AREA_CACHE.clear()

    def tearDown(self):
        ground_lessons.AREA_DIR = self._old_dir
        ground_lessons._AREA_CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _area_row(self, key):
        return self.q1("SELECT * FROM gs_area_results WHERE student_id = ? AND area_id = ? AND item_key = ?",
                       (self.sid, self.area, key))

    def test_hand_written_game_key_is_accepted(self):
        c = self.login("flight_student")
        self.assertTrue(c.post(f"{GS}/task/{self.task}/game-done", json={"key": "h2", "tries": 2}).get_json()["recorded"])
        self.assertTrue(c.post(f"{GS}/task/{self.task}/game-done", json={"key": "fh2", "tries": 1}).get_json()["recorded"])
        self.assertEqual(c.post(f"{GS}/task/{self.task}/game-done", json={"key": "hx", "tries": 1}).status_code, 400)

    def test_old_final_answers_do_not_count_for_new_final_quiz(self):
        L = ground_lessons.load("PA.I.E")
        if not L.get("final_quiz"):
            self.skipTest("PA.I.E has no final_quiz yet")
        conn = db.get_db()      # an answer saved before the lesson had its own final_quiz
        conn.execute("INSERT INTO gs_lesson_results (student_id, task_id, item_key, kind, first_correct, attempts, "
                     "completed_at, first_at, last_at) VALUES (?, ?, 'fq1', 'final', 1, 1, ?, ?, ?)",
                     (self.sid, self.task, db.now_iso(), db.now_iso(), db.now_iso()))
        conn.commit()
        conn.close()
        import groundschool
        conn = db.get_db()
        st = groundschool.lesson_stats(conn, self.sid, self.task, L)
        conn.close()
        self.assertEqual(st["final_answered"], 0)
        self.assertEqual(st["final_total"], len(L["final_quiz"]))
        q = L["final_quiz"][0]
        c = self.login("flight_student")
        c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 1, "j": q["answer"], "final": True})
        self.assertTrue(self.q1("SELECT 1 FROM gs_lesson_results WHERE student_id = ? AND item_key = 'fz1'", (self.sid,)))

    def test_area_review_graded_saved_and_shown(self):
        c = self.login("flight_student")
        page = c.get(f"{GS}/{self.rid}").get_data(as_text=True)
        self.assertIn(f"/area/{self.area}/review", page)
        self.assertIn("Area I review", page)
        body = c.get(f"{GS}/area/{self.area}/review").get_data(as_text=True)
        self.assertIn("Q1?", body)
        self.assertIn('data-base="/flight/groundschool/area/%d/review"' % self.area, body)
        q = REVIEW["questions"][0]
        wrong, right = (q["answer"] + 1) % 4, q["answer"]
        r = c.post(f"{GS}/area/{self.area}/review/quiz-answer", json={"n": 1, "j": wrong}).get_json()
        self.assertFalse(r["correct"])
        self.assertNotIn("why", r)
        r = c.post(f"{GS}/area/{self.area}/review/quiz-answer", json={"n": 1, "j": right}).get_json()
        self.assertTrue(r["correct"])
        self.assertIn("PHAK 1-2", r["why"])
        row = self._area_row("r1")
        self.assertEqual((row["first_correct"], row["attempts"]), (0, 2))
        self.assertTrue(row["completed_at"])
        c.post(f"{GS}/area/{self.area}/review/quiz-answer", json={"n": 1, "j": wrong})   # review after right: not counted
        self.assertEqual(self._area_row("r1")["attempts"], 2)
        q2 = REVIEW["questions"][1]
        c.post(f"{GS}/area/{self.area}/review/quiz-answer", json={"n": 2, "j": q2["answer"]})
        self.assertEqual(self._area_row("r2")["first_correct"], 1)
        c.post(f"{GS}/area/{self.area}/review/lesson-time", json={"seconds": 9999})
        c.post(f"{GS}/area/{self.area}/review/lesson-time", json={"seconds": 20})
        t = self.q1("SELECT * FROM gs_area_time WHERE student_id = ? AND area_id = ?", (self.sid, self.area))
        self.assertEqual(t["seconds"], 60)
        self.assertEqual(c.post(f"{GS}/area/{self.area}/review/quiz-answer", json={"n": 99, "j": 0}).status_code, 404)
        self.assertEqual(c.post(f"{GS}/area/{self.area}/review/quiz-answer", json={"n": "x", "j": 0}).status_code, 400)
        # saved state comes back on return
        body = c.get(f"{GS}/area/{self.area}/review").get_data(as_text=True)
        self.assertNotIn("Student activity on this review", body)
        # the instructor sees first-try grade, tries and time, on the review and the student summary
        m = self.login("master")
        staff = m.get(f"{GS}/area/{self.area}/review?student_id={self.sid}").get_data(as_text=True)
        self.assertIn("Student activity on this review", staff)
        self.assertIn("initial review grade", staff)
        self.assertIn("missed", staff)
        self.assertIn("1 min", staff)
        summary = m.get(f"{GS}/student/{self.sid}").get_data(as_text=True)
        self.assertIn("Area reviews", summary)
        self.assertIn("Preflight Preparation", summary)
        self.assertIn("2 / 7", summary)          # answered
        course = m.get(f"{GS}/{self.rid}?student_id={self.sid}").get_data(as_text=True)
        self.assertIn("(2/7 answered)", course)

    def test_staff_answers_are_not_saved_and_missing_review_404s(self):
        m = self.login("master")
        q = REVIEW["questions"][0]
        self.assertTrue(m.post(f"{GS}/area/{self.area}/review/quiz-answer", json={"n": 1, "j": q["answer"]}).get_json()["correct"])
        self.assertIsNone(self.q1("SELECT 1 FROM gs_area_results"))
        os.remove(os.path.join(self.tmp, "PA.I.json"))
        ground_lessons._AREA_CACHE.clear()
        self.assertEqual(m.get(f"{GS}/area/{self.area}/review").status_code, 404)
        self.assertNotIn("/review", m.get(f"{GS}/{self.rid}").get_data(as_text=True).split("Area I.")[-1][:2000])
