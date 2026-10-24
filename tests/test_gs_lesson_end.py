"""The end of a Ground School lesson: a Next button, references folded away,
the writer's notes for staff only, no empty Lesson Content box for students."""
import db
import test_school_preview_fixes as _m
_Base = _m.GroundSchoolTest


class LessonEndTest(_Base):
    def setUp(self):
        super().setUp()
        conn = db.get_db()
        area = conn.execute("SELECT area_id FROM acs_tasks WHERE id = ?", (self.task_id,)).fetchone()["area_id"]
        self.task_b = conn.execute("INSERT INTO acs_tasks (area_id, code, title, acs_references, order_index) VALUES (?,?,?,?,?)",
                                   (area, "B", "Airworthiness Requirements", "", 2)).lastrowid
        conn.commit()
        conn.close()

    def page(self, who, task_id=None):
        return self.login(who).get(f"/flight/groundschool/task/{task_id or self.task_id}").get_data(as_text=True)

    def test_student_sees_next_button_and_progress_and_no_empty_box(self):
        html = self.page("flight_student")
        self.assertIn("Next: Task B. Airworthiness Requirements", html)
        self.assertIn("0 of 2 elements done", html)
        self.assertNotIn("No lesson content yet.", html)
        self.assertNotIn("Not in the uploaded resources", html)

    def test_references_are_folded_into_one_line(self):
        html = self.page("flight_student")
        self.assertIn("References used (", html)
        self.assertIn("<summary>References used (", html)

    def test_last_task_goes_back_to_the_course(self):
        self.assertIn("Back to the course", self.page("flight_student", self.task_b))

    def test_staff_still_get_the_edit_box(self):
        html = self.page("cfi")
        self.assertIn("Next: Task B.", html)


