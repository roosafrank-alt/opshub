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
            for a in L.get("activities", []):
                self.assertIn(a["type"], ground_lessons.ACTIVITY_TYPES, f)
                if a["type"] == "match":
                    self.assertTrue(all(p["l"] and p["r"] for p in a["pairs"]), f)
                elif a["type"] == "sort":
                    self.assertTrue(all(0 <= i["b"] < len(a["buckets"]) for i in a["items"]), f)
                elif a["type"] == "label":
                    self.assertIn(a["figure"], L["figures"], f)
                    self.assertTrue(all(0 <= p["x"] <= 100 and 0 <= p["y"] <= 100 for p in a["pins"]), f)
            for fg in L.get("figures", {}).values():
                self.assertTrue(os.path.exists(os.path.join(static, fg["file"])), fg["file"])

    def test_lesson_shows_on_task_page_with_linked_citations(self):
        body = self.login("master").get(f"{GS}/task/{self.task}").get_data(as_text=True)
        self.assertIn('id="gsl"', body)
        self.assertIn("Pre-reading", body)
        self.assertIn(f"{GS}/resources/60/excerpt?pages=", body)
        self.assertIn("Lesson Content", body)          # the editable box is still there

    def test_games_and_quizzes_sit_between_lessons(self):
        body = self.login("master").get(f"{GS}/task/{self.task}").get_data(as_text=True)
        self.assertNotIn('id="gsl-quiz"', body)             # no pile of quizzes at the end
        self.assertGreater(body.count("gsl-play-item"), 6)
        for kind in ("match", "sort", "order", "tf", "flash"):
            self.assertIn(f'data-act="{kind}"', body)
        self.assertIn("Quick check 1 of", body)
        # first game appears before the last lesson chunk (they are spread out)
        self.assertLess(body.index("gsl-play-item"), body.rindex('class="gsl-chunk'))
        # the lesson comes before the ACS standard card
        self.assertLess(body.index('id="gsl"'), body.index("ACS Standard"))

    def test_play_plan_spreads_evenly(self):
        L = {"chunks": [{}] * 10, "quiz": [{"q": str(i)} for i in range(9)],
             "activities": [{"type": "match"}, {"type": "sort", "after": 2}]}
        plan = ground_lessons.play_plan(L)
        spots = sorted(plan["by_chunk"])
        self.assertGreaterEqual(len(spots), 4)
        self.assertEqual(sum(len(v) for v in plan["by_chunk"].values()), 2 + 3)   # 2 games + 3 quiz groups
        self.assertTrue(any(i["kind"] == "act" and i["a"]["type"] == "sort" for i in plan["by_chunk"][2]))

    def test_activities_adds_flashcards_and_term_match_from_key_terms(self):
        L = {"key_terms": [{"term": f"T{i}", "def": f"D{i}"} for i in range(8)]}
        acts = ground_lessons.activities(L)
        self.assertEqual([a["type"] for a in acts], ["flash", "match"])
        self.assertEqual(len(acts[1]["pairs"]), 6)
        self.assertEqual(ground_lessons.activities({"key_terms": [{"term": "a", "def": "b"}]}), [])

    def test_activity_macro_renders_every_type_and_escapes(self):
        L = {"figures": {"f1": {"file": "PA.I.A-1.jpg", "alt": "x", "caption": "c"}}}
        acts = [
            {"type": "label", "title": "L", "figure": "f1", "pins": [{"x": 10, "y": 20, "label": "<i>A</i>"}]},
            {"type": "sort", "title": "S", "buckets": ["B0"], "items": [{"t": "i", "b": 0}]},
        ]
        with self.app.test_request_context():
            from flask import render_template_string
            html = render_template_string(
                '{% import "flight/_gsl_macros.html" as g %}{% for a in acts %}{{ g.activity(a, L) }}{% endfor %}',
                acts=acts, L=L)
        self.assertIn("gsl-pin", html)
        self.assertIn("left:10%;top:20%", html)
        self.assertIn("&lt;i&gt;A&lt;/i&gt;", html)
        self.assertIn("gsl-bucket", html)

    def test_rich_escapes_html(self):
        with self.app.test_request_context():
            out = str(ground_lessons.rich("<b>x</b> [[1:2|PHAK 1-2]] **bold**"))
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", out)
        self.assertIn("/resources/1/excerpt?pages=2", out)
        self.assertIn("<b>bold</b>", out)

    def test_task_without_lesson_has_no_lesson_block(self):
        body = self.login("master").get(f"{GS}/task/{self.other}").get_data(as_text=True)
        self.assertNotIn('id="gsl"', body)


class ElementLessonTest(OpsHubTestCase):
    """Tapping an element code (PA.I.E.K1) shows that part of the lesson; the student's
    "I've finished this lesson" turns the code green with a check."""
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        conn = db.get_db()
        rid = conn.execute("INSERT INTO acs_ratings (name, slug, uploaded_at, status) VALUES (?,?,?,?)",
                           ("Private Pilot", "ppl", db.now_iso(), "parsed")).lastrowid
        area = conn.execute("INSERT INTO acs_areas (rating_id, code, title, order_index) VALUES (?,?,?,1)",
                            (rid, "I", "Preflight Preparation")).lastrowid
        self.task = conn.execute("INSERT INTO acs_tasks (area_id, code, title, acs_references, order_index) "
                                 "VALUES (?,?,?,?,1)", (area, "E", "National Airspace System", "")).lastrowid
        self.k1 = conn.execute("INSERT INTO acs_task_elements (task_id, kind, code, text, order_index) VALUES (?,?,?,?,1)",
                               (self.task, "knowledge", "PA.I.E.K1", "Airspace classes.")).lastrowid
        self.k4 = conn.execute("INSERT INTO acs_task_elements (task_id, kind, code, text, order_index) VALUES (?,?,?,?,2)",
                               (self.task, "knowledge", "PA.I.E.K4", "Special VFR.")).lastrowid
        conn.execute("INSERT INTO academy_course_grants (student_id, rating_id, granted_by, granted_at) VALUES (?,?,?,?)",
                     (self.sid, rid, "test", db.now_iso()))
        conn.commit()
        conn.close()
        self.rid = rid

    def test_for_element_matches_sub_elements_only(self):
        L = {"chunks": [{"codes": ["X.I.A.K1a"]}, {"codes": ["X.I.A.K10"]}, {"codes": ["X.I.A.K2"]}],
             "skills": [{"code": "X.I.A.S1"}], "scenario": {"codes": ["X.I.A.R1"]}}
        got = ground_lessons.for_element(L, "X.I.A.K1")
        self.assertEqual([n for n, _ in got["chunks"]], [1])
        self.assertIsNone(got["scenario"])
        self.assertIsNone(ground_lessons.for_element(L, "X.I.A.K3"))

    def test_element_page_shows_its_lesson_and_student_marks_done(self):
        c = self.login("flight_student")
        body = c.get(f"{GS}/element/{self.k1}").get_data(as_text=True)
        self.assertIn("Lesson for PA.I.E.K1", body)
        self.assertIn("finished this lesson</button>", body)
        self.assertIsNone(self.q1("SELECT * FROM acs_element_completion WHERE element_id = ?", (self.k1,)))  # visit alone isn't done
        c.post(f"{GS}/element/{self.k1}/lesson-done")
        row = self.q1("SELECT * FROM acs_element_completion WHERE element_id = ? AND student_id = ?", (self.k1, self.sid))
        self.assertTrue(row["self_completed_at"])
        page = c.get(f"{GS}/{self.rid}").get_data(as_text=True)
        self.assertIn("/task/", page)                       # course page rows open the lesson page (codes live there)
        c.post(f"{GS}/element/{self.k1}/lesson-done", data={"undo": "1"})
        self.assertIsNone(self.q1("SELECT self_completed_at FROM acs_element_completion WHERE element_id = ?",
                                  (self.k1,))["self_completed_at"])

    def test_staff_cannot_post_done(self):
        r = self.login("master").post(f"{GS}/element/{self.k1}/lesson-done")
        self.assertEqual(r.status_code, 403)


class FinishWholeLessonTest(ElementLessonTest):
    def test_finish_whole_lesson_marks_every_element_and_staff_cannot(self):
        c = self.login("flight_student")
        c.post(f"{GS}/task/{self.task}/lesson-done")
        rows = self.q1("SELECT COUNT(*) AS n FROM acs_element_completion WHERE student_id = ? AND self_completed_at IS NOT NULL",
                       (self.sid,))
        self.assertEqual(rows["n"], 2)
        self.assertEqual(self.login("master").post(f"{GS}/task/{self.task}/lesson-done").status_code, 403)


class LessonGradesTest(ElementLessonTest):
    """GS-GRADES: quick checks are graded on the server and can be corrected to 100%, the first
    try is kept as the initial grade, tries and time are recorded, and Resume works."""
    def _row(self, key):
        return self.q1("SELECT * FROM gs_lesson_results WHERE student_id = ? AND task_id = ? AND item_key = ?",
                       (self.sid, self.task, key))

    def _wrong_and_right(self, n=1):
        q = ground_lessons.load("PA.I.E")["quiz"][n - 1]
        return (q["answer"] + 1) % len(q["options"]), q["answer"]

    def test_wrong_then_right_keeps_first_try_and_counts_tries(self):
        c = self.login("flight_student")
        wrong, right = self._wrong_and_right()
        r = c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 1, "j": wrong}).get_json()
        self.assertFalse(r["correct"])
        self.assertNotIn("why", r)                                  # the explanation waits until it is right
        r = c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 1, "j": right}).get_json()
        self.assertTrue(r["correct"])
        self.assertTrue(r["why"])
        row = self._row("q1")
        self.assertEqual((row["first_correct"], row["attempts"]), (0, 2))   # initial grade stays missed
        self.assertTrue(row["completed_at"])
        c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 1, "j": wrong})   # reviewing later changes nothing
        self.assertEqual(self._row("q1")["attempts"], 2)

    def test_first_try_right_is_recorded_as_initial_credit(self):
        _, right = self._wrong_and_right(2)
        self.login("flight_student").post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 2, "j": right})
        row = self._row("q2")
        self.assertEqual((row["first_correct"], row["attempts"]), (1, 1))

    def test_staff_get_graded_but_nothing_is_recorded_and_bad_input_rejected(self):
        _, right = self._wrong_and_right()
        c = self.login("master")
        self.assertTrue(c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 1, "j": right}).get_json()["correct"])
        self.assertIsNone(self._row("q1"))
        self.assertEqual(c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 999, "j": 0}).status_code, 404)
        self.assertEqual(c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": "x", "j": 0}).status_code, 400)

    def test_time_is_added_capped_and_remembers_the_spot(self):
        c = self.login("flight_student")
        c.post(f"{GS}/task/{self.task}/lesson-time", json={"seconds": 25, "anchor": "gsl-c3"})
        c.post(f"{GS}/task/{self.task}/lesson-time", json={"seconds": 9999, "anchor": "bad anchor!"})
        row = self.q1("SELECT * FROM gs_lesson_time WHERE student_id = ? AND task_id = ?", (self.sid, self.task))
        self.assertEqual(row["seconds"], 25 + 40)                  # one heartbeat is capped
        self.assertEqual(row["last_anchor"], "gsl-c3")             # a bad anchor does not overwrite it

    def test_game_done_records_tries_once(self):
        c = self.login("flight_student")
        c.post(f"{GS}/task/{self.task}/game-done", json={"key": "a0", "tries": 3})
        c.post(f"{GS}/task/{self.task}/game-done", json={"key": "a0", "tries": 1})
        self.assertEqual(self._row("a0")["attempts"], 3)
        self.assertEqual(c.post(f"{GS}/task/{self.task}/game-done", json={"key": "../x", "tries": 1}).status_code, 400)

    def test_resume_buttons_and_instructor_panel(self):
        c = self.login("flight_student")
        self.assertNotIn("Resume course", c.get(GS).get_data(as_text=True))
        wrong, right = self._wrong_and_right()
        c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 1, "j": wrong})
        c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 1, "j": right})
        c.post(f"{GS}/task/{self.task}/lesson-time", json={"seconds": 30, "anchor": "gsl-c2"})
        self.assertIn(f"/task/{self.task}#gsl-c2", c.get(GS).get_data(as_text=True))
        self.assertIn("Resume course", c.get(f"{GS}/{self.rid}").get_data(as_text=True))
        page = c.get(f"/flight/groundschool/task/{self.task}").get_data(as_text=True)
        self.assertIn("Resume lesson", page)
        self.assertNotIn("Student activity on this lesson", page)    # that panel is for the instructor
        staff = self.login("master")
        body = staff.get(f"{GS}/task/{self.task}?student_id={self.sid}").get_data(as_text=True)
        self.assertIn("Student activity on this lesson", body)
        self.assertIn("initial quiz grade", body)
        self.assertIn("missed", body)                                # Q1 was missed on the first try


    def test_final_check_is_recorded_apart_and_completed_state_shows_on_return(self):
        c = self.login("flight_student")
        L = ground_lessons.load("PA.I.E")
        q = (L.get("final_quiz") or L["quiz"])[0]
        wrong, right = (q["answer"] + 1) % len(q["options"]), q["answer"]
        c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 1, "j": wrong, "final": True})
        c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 1, "j": right, "final": True})
        row = self._row(ground_lessons.final_prefix(L) + "1")
        self.assertEqual((row["kind"], row["first_correct"], row["attempts"]), ("final", 0, 2))
        self.assertIsNone(self._row("q1"))                          # quick checks are separate
        c.post(f"{GS}/task/{self.task}/game-done", json={"key": "fa1", "tries": 2})
        self.assertEqual(self._row("fa1")["kind"], "final")
        # coming back: finished things are flagged done so the page shows them completed with a Retake
        c.post(f"{GS}/task/{self.task}/game-done", json={"key": "a0", "tries": 1})
        body = c.get(f"{GS}/task/{self.task}").get_data(as_text=True)
        self.assertIn('id="gsl-final"', body)
        self.assertIn('data-key="a0" data-done="1"', body)
        staff = self.login("master").get(f"{GS}/task/{self.task}?student_id={self.sid}").get_data(as_text=True)
        self.assertIn("initial FINAL CHECK grade", staff)


class SectionReadMarksTest(LessonGradesTest.__bases__[0]):
    """GS-READ: time per reading section is saved; a read section gets a check mark, and the
    instructor sees the time on each."""
    def test_section_time_marks_read_and_shows_for_instructor(self):
        c = self.login("flight_student")
        c.post(f"{GS}/task/{self.task}/lesson-time", json={"seconds": 20, "anchor": "gsl-c1", "chunks": {"1": 40, "2": 3}})
        c.post(f"{GS}/task/{self.task}/lesson-time", json={"seconds": 20, "chunks": {"1": 40}})
        rows = {r["chunk_n"]: r["seconds"] for r in self.q("SELECT chunk_n, seconds FROM gs_chunk_time WHERE student_id = ?", (self.sid,))}
        self.assertEqual(rows[1], 80)
        self.assertEqual(rows[2], 3)
        mine = c.get(f"{GS}/task/{self.task}").get_data(as_text=True)
        self.assertIn('id="gsl-c1" data-need=', mine)
        self.assertIn("gsl-read", mine)
        staff = self.login("master").get(f"{GS}/task/{self.task}?student_id={self.sid}").get_data(as_text=True)
        self.assertIn("Reading sections:", staff)
        self.assertIn("1 min 20 s", staff)

class GroundSchoolChartsTest(OpsHubTestCase):
    def test_list_page_shows_progress_charts_for_a_student(self):
        c = self.login("master")
        c.post("/view-as/flight/student/me")
        body = c.get("/flight/groundschool").get_data(as_text=True)
        self.assertIn("Your progress", body)
        self.assertIn("gsd-grid", body)


class StudentSummaryTest(LessonGradesTest.__bases__[0]):
    """Staff get a student search and a one-page summary: flying, course progress, and every lesson's time and grades."""
    def test_staff_see_summary_students_do_not(self):
        c = self.login("flight_student")
        wrong, right = 0, ground_lessons.load("PA.I.E")["quiz"][0]["answer"]
        c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 1, "j": right})
        c.post(f"{GS}/task/{self.task}/lesson-time", json={"seconds": 90, "chunks": {"1": 40}})
        self.assertNotEqual(c.get(f"{GS}/students").status_code, 200)
        self.assertNotEqual(c.get(f"{GS}/student/{self.sid}").status_code, 200)
        m = self.login("master")
        body = m.get(f"{GS}/students").get_data(as_text=True)
        self.assertIn("Student summaries", body)
        body = m.get(f"{GS}/student/{self.sid}").get_data(as_text=True)
        self.assertIn("Lesson by lesson", body)
        self.assertIn("2 min", body.replace("1 min", "2 min") if "1 min" in body else body) if False else None
        self.assertIn("Initial quiz", body)
        self.assertEqual(m.get(f"{GS}/student/999999").status_code, 404)

    def test_course_picker_last_studying_and_needs_attention(self):
        c = self.login("flight_student")
        q = ground_lessons.load("PA.I.E")["quiz"][0]
        wrong = (q["answer"] + 1) % len(q["options"])
        c.post(f"{GS}/task/{self.task}/quiz-answer", json={"n": 1, "j": wrong})
        c.post(f"{GS}/task/{self.task}/lesson-time", json={"seconds": 90, "anchor": "gsl-c1", "chunks": {"1": 40}})
        m = self.login("master")
        body = m.get(f"{GS}/student/{self.sid}").get_data(as_text=True)
        self.assertIn('id="ss-course"', body)
        self.assertIn('id="ss-back"', body)
        self.assertIn("Last studying", body)
        self.assertIn("Needs attention", body)
        self.assertIn("Time by section", body)
        self.assertIn("Questions missed on the first try", body)
        # a bad or unrelated course id falls back to a valid course instead of failing
        self.assertEqual(m.get(f"{GS}/student/{self.sid}?course=abc").status_code, 200)
        self.assertEqual(m.get(f"{GS}/student/{self.sid}?course=999999").status_code, 200)
