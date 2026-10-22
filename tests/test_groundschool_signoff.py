"""Ground school: ACS task sign-offs and homework assignments (who may do
what, bad input, double-submits, missing records, element progress agreeing
with the sign-off).
"""
import db
from harness import OpsHubTestCase, seed_row


class GSBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        for k in ("cfi", "flight_student", "master"):
            self.exec("UPDATE users SET groundschool_access=1 WHERE id=?", (self.users[k]["id"],))
        self.student = self.q1("SELECT id FROM students WHERE user_id=?", (self.users["flight_student"]["id"],))["id"]
        conn = db.get_db()
        r = seed_row(conn, "acs_ratings", name="Private Pilot", slug="private", uploaded_at=db.now_iso(), status="ready")
        a = seed_row(conn, "acs_areas", rating_id=r, code="I", title="Preflight", order_index=1)
        self.task = seed_row(conn, "acs_tasks", area_id=a, code="A", title="Pilot Qualifications", order_index=1)
        self.task2 = seed_row(conn, "acs_tasks", area_id=a, code="B", title="Airworthiness", order_index=2)
        self.el1 = seed_row(conn, "acs_task_elements", task_id=self.task, kind="knowledge", code="PA.I.A.K1", text="x", order_index=1)
        self.el2 = seed_row(conn, "acs_task_elements", task_id=self.task, kind="skills", code="PA.I.A.S1", text="y", order_index=2)
        conn.commit()
        conn.close()

    def signoffs(self, task=None):
        return self.q("SELECT * FROM acs_task_signoff WHERE task_id=?", (task or self.task,))

    def sign(self, **form):
        data = dict(student_id=self.student, notes="good")
        data.update(form)
        return self.client.post(f"/flight/groundschool/task/{self.task}/signoff", data=data)

    def verified(self):
        return self.q("SELECT * FROM acs_element_completion WHERE student_id=? AND cfi_verified_at IS NOT NULL",
                      (self.student,))


class SignoffTest(GSBase):
    def test_cfi_signs_off_and_elements_verified(self):
        self.login("cfi")
        self.assertEqual(self.sign().status_code, 302)
        s = self.signoffs()
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["notes"], "good")
        self.assertEqual({v["element_id"] for v in self.verified()}, {self.el1, self.el2})

    def test_admin_can_sign_off(self):
        self.login("master")
        self.sign()
        self.assertEqual(len(self.signoffs()), 1)

    def test_resign_updates_in_place(self):
        self.login("cfi")
        self.sign()
        self.sign(notes="second look")
        s = self.signoffs()
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["notes"], "second look")
        self.assertEqual(len(self.verified()), 2)

    def test_student_cannot_sign_off(self):
        self.login("flight_student")
        self.sign()
        self.assertEqual(len(self.signoffs()), 0)
        self.assertEqual(len(self.verified()), 0)

    def test_logged_out_cannot_sign_off(self):
        self.login(None)
        r = self.sign()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.signoffs()), 0)

    def test_bad_student_values_make_no_signoff_and_no_crash(self):
        self.login("cfi")
        for bad in ("", "abc", "99999", "-1", "nan"):
            with self.subTest(student_id=bad):
                r = self.sign(student_id=bad)
                self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.signoffs()), 0)

    def test_missing_task_is_404(self):
        self.login("cfi")
        r = self.client.post("/flight/groundschool/task/99999/signoff", data=dict(student_id=self.student))
        self.assertEqual(r.status_code, 404)

    def test_blank_notes_stored_as_empty(self):
        self.login("cfi")
        self.sign(notes="   ")
        self.assertIsNone(self.signoffs()[0]["notes"])

    def test_signoff_only_touches_its_own_task(self):
        e3 = self.exec("INSERT INTO acs_task_elements (task_id, kind, code, text, order_index) VALUES (?,?,?,?,?)",
                       (self.task2, "knowledge", "PA.I.B.K1", "z", 1))
        self.login("cfi")
        self.sign()
        self.assertEqual(len(self.signoffs(self.task2)), 0)
        self.assertNotIn(e3, {v["element_id"] for v in self.verified()})

    def test_remove_signoff(self):
        self.login("cfi")
        self.sign()
        sid = self.signoffs()[0]["id"]
        self.client.post(f"/flight/groundschool/task/{self.task}/signoff/{sid}/remove")
        self.assertEqual(len(self.signoffs()), 0)

    def test_remove_with_wrong_task_keeps_signoff(self):
        self.login("cfi")
        self.sign()
        sid = self.signoffs()[0]["id"]
        self.client.post(f"/flight/groundschool/task/{self.task2}/signoff/{sid}/remove")
        self.assertEqual(len(self.signoffs()), 1)

    def test_student_cannot_remove_signoff(self):
        self.login("cfi")
        self.sign()
        sid = self.signoffs()[0]["id"]
        self.login("flight_student")
        self.client.post(f"/flight/groundschool/task/{self.task}/signoff/{sid}/remove")
        self.assertEqual(len(self.signoffs()), 1)

    def test_remove_missing_signoff_does_not_crash(self):
        self.login("cfi")
        r = self.client.post(f"/flight/groundschool/task/{self.task}/signoff/99999/remove")
        self.assertEqual(r.status_code, 302)

    def test_task_page_renders_for_each_role(self):
        self.login("cfi")
        self.sign()
        for role in ("cfi", "master", "flight_student"):
            self.login(role)
            with self.subTest(role=role):
                self.assertEqual(self.client.get(f"/flight/groundschool/task/{self.task}").status_code, 200)
        self.login("cfi")
        self.assertEqual(self.client.get("/flight/groundschool/task/99999").status_code in (302, 404), True)


class ElementVerifyTest(GSBase):
    def test_verify_element(self):
        self.login("cfi")
        self.client.post(f"/flight/groundschool/element/{self.el1}/verify", data=dict(student_id=self.student))
        self.assertEqual([v["element_id"] for v in self.verified()], [self.el1])

    def test_verify_twice_one_row(self):
        self.login("cfi")
        for _ in range(2):
            self.client.post(f"/flight/groundschool/element/{self.el1}/verify", data=dict(student_id=self.student))
        self.assertEqual(len(self.q("SELECT * FROM acs_element_completion")), 1)

    def test_student_cannot_verify(self):
        self.login("flight_student")
        self.client.post(f"/flight/groundschool/element/{self.el1}/verify", data=dict(student_id=self.student))
        self.assertEqual(len(self.verified()), 0)

    def test_verify_bad_student(self):
        self.login("cfi")
        for bad in ("", "abc", "99999"):
            r = self.client.post(f"/flight/groundschool/element/{self.el1}/verify", data=dict(student_id=bad))
            self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.verified()), 0)


class HomeworkTest(GSBase):
    def assign(self, **form):
        data = dict(student_id=self.student, task_id=[self.task])
        data.update(form)
        return self.client.post("/flight/groundschool/assign", data=data)

    def rows(self):
        return self.q("SELECT * FROM gs_assignments ORDER BY id")

    def test_cfi_assigns(self):
        self.login("cfi")
        self.assign(note="read ch 3", due_on="2030-01-05")
        r = self.rows()
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["task_id"], r[0]["note"], r[0]["due_on"]), (self.task, "read ch 3", "2030-01-05"))

    def test_assign_twice_no_duplicate(self):
        self.login("cfi")
        self.assign()
        self.assign()
        self.assertEqual(len(self.rows()), 1)

    def test_two_lessons_at_once(self):
        self.login("cfi")
        self.assign(task_id=[self.task, self.task2])
        self.assertEqual(len(self.rows()), 2)

    def test_student_cannot_assign(self):
        self.login("flight_student")
        r = self.assign()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(len(self.rows()), 0)

    def test_bad_due_date_ignored(self):
        self.login("cfi")
        for bad in ("garbage", "2030-13-45", "NaN"):
            self.exec("DELETE FROM gs_assignments")
            r = self.assign(due_on=bad)
            self.assertEqual(r.status_code, 302)
            self.assertIsNone(self.rows()[0]["due_on"])

    def test_missing_student_or_lessons(self):
        self.login("cfi")
        self.assign(student_id="99999")
        self.assign(student_id="abc")
        self.assign(task_id=[])
        self.assign(task_id=["abc", "-5"])
        self.assertEqual(len(self.rows()), 0)

    def test_nonexistent_lesson_id_is_not_stored(self):
        self.login("cfi")
        self.assign(task_id=["99999"])
        self.assertEqual(len([r for r in self.rows() if r["task_id"] == 99999]), 0)

    def test_inactive_student_not_assignable(self):
        self.exec("UPDATE students SET active=0 WHERE id=?", (self.student,))
        self.login("cfi")
        self.assign()
        self.assertEqual(len(self.rows()), 0)

    def test_long_note_trimmed(self):
        self.login("cfi")
        self.assign(note="x" * 2000)
        self.assertLessEqual(len(self.rows()[0]["note"]), 500)

    def test_next_redirect_stays_on_site(self):
        self.login("cfi")
        for nxt in ("//evil.example/x", "https://evil.example/x"):
            r = self.assign(next=nxt)
            self.assertNotIn("evil.example", r.headers["Location"])

    def test_unticking_cancels(self):
        self.login("cfi")
        self.assign()
        self.client.post("/flight/groundschool/assign", data=dict(student_id=self.student, was_assigned=[self.task]))
        self.assertIsNotNone(self.rows()[0]["cancelled_at"])

    def test_reassign_after_cancel(self):
        self.login("cfi")
        self.assign()
        aid = self.rows()[0]["id"]
        self.client.post(f"/flight/groundschool/assignment/{aid}/cancel")
        self.assign()
        open_rows = [r for r in self.rows() if r["cancelled_at"] is None]
        self.assertEqual(len(open_rows), 1)

    def test_review_and_unreview(self):
        self.login("cfi")
        self.assign()
        aid = self.rows()[0]["id"]
        self.client.post(f"/flight/groundschool/assignment/{aid}/reviewed")
        self.assertIsNotNone(self.rows()[0]["reviewed_at"])
        self.client.post(f"/flight/groundschool/assignment/{aid}/unreviewed")
        self.assertIsNone(self.rows()[0]["reviewed_at"])

    def test_assignment_action_rules(self):
        self.login("cfi")
        self.assign()
        aid = self.rows()[0]["id"]
        self.assertEqual(self.client.post(f"/flight/groundschool/assignment/{aid}/explode").status_code, 403)
        self.assertEqual(self.client.post("/flight/groundschool/assignment/99999/cancel").status_code, 404)
        self.login("flight_student")
        self.assertEqual(self.client.post(f"/flight/groundschool/assignment/{aid}/cancel").status_code, 403)
        self.assertIsNone(self.rows()[0]["cancelled_at"])

    def test_student_summary_pages_render(self):
        self.login("cfi")
        self.assign()
        self.assertEqual(self.client.get(f"/flight/groundschool/student/{self.student}").status_code, 200)
        self.assertIn(self.client.get("/flight/groundschool/student/99999").status_code, (302, 404))
        self.assertEqual(self.client.get("/flight/groundschool/students?q=%25&q2=\x00").status_code, 200)


class SignoffRemoveElementsTest(GSBase):
    from harness import open_finding as _of

    @_of("qa-signoff-remove-keeps-verified")
    def test_removing_signoff_unverifies_elements_it_verified(self):
        self.login("cfi")
        self.sign()
        sid = self.signoffs()[0]["id"]
        self.client.post(f"/flight/groundschool/task/{self.task}/signoff/{sid}/remove")
        self.assertEqual(len(self.verified()), 0)
