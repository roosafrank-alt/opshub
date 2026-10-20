"""GS-BUILTIN-COURSE: courses written in OpsHub (no ACS PDF), like the Part 91 Refresher,
are added from ground_lessons/courses/<CODE>.json and updated in place without losing progress."""
import json
import os
import re

from harness import OpsHubTestCase
import db
import ground_lessons

GS = "/flight/groundschool"


class BuiltinCoursesTest(OpsHubTestCase):
    def test_shipped_outlines_are_valid(self):
        courses = ground_lessons.builtin_courses()
        self.assertTrue(any(c["code"] == "RF" for c in courses))
        for c in courses:
            seen = set()
            for a in c["areas"]:
                self.assertTrue(a["tasks"], a["code"])
                for t in a["tasks"]:
                    els = t["knowledge"] + t["risk_management"] + t["skills"]
                    self.assertTrue(els)
                    for e in els:
                        self.assertTrue(re.fullmatch(r"%s\.%s\.%s\.[KRS]\d+" % (c["code"], a["code"], t["code"]), e["code"]), e["code"])
                        self.assertNotIn(e["code"], seen)
                        seen.add(e["code"])

    def test_admin_adds_then_updates_course_keeping_progress(self):
        c = self.login("master")
        self.assertIn("Part 91 Refresher", c.get(GS).get_data(as_text=True))
        r = c.post(f"{GS}/builtin/RF/install")
        self.assertEqual(r.status_code, 302)
        conn = db.get_db()
        rt = conn.execute("SELECT * FROM acs_ratings WHERE doc_number = 'OPSHUB-RF'").fetchone()
        self.assertIsNotNone(rt)
        self.assertIsNone(rt["pdf_filename"])
        self.assertEqual(rt["status"], "parsed")
        n_tasks = conn.execute("SELECT COUNT(*) FROM acs_tasks t JOIN acs_areas a ON a.id = t.area_id "
                               "WHERE a.rating_id = ?", (rt["id"],)).fetchone()[0]
        outline = ground_lessons.builtin_course("RF")
        self.assertEqual(n_tasks, sum(len(a["tasks"]) for a in outline["areas"]))
        el = conn.execute("SELECT e.id FROM acs_task_elements e JOIN acs_tasks t ON t.id = e.task_id "
                          "JOIN acs_areas a ON a.id = t.area_id WHERE a.rating_id = ? AND e.code = 'RF.I.A.K1'",
                          (rt["id"],)).fetchone()
        self.assertIsNotNone(el)
        # no Resources row for a course without a PDF
        self.assertIsNone(conn.execute("SELECT 1 FROM acs_resources WHERE acs_rating_id = ?", (rt["id"],)).fetchone())
        conn.close()
        # second tap updates in place: same element id, still one course
        self.assertEqual(c.post(f"{GS}/builtin/RF/install").status_code, 302)
        conn = db.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM acs_ratings WHERE doc_number = 'OPSHUB-RF'").fetchone()[0], 1)
        el2 = conn.execute("SELECT e.id FROM acs_task_elements e WHERE e.code = 'RF.I.A.K1'").fetchone()
        self.assertEqual(el["id"], el2["id"])
        conn.close()
        # the course page opens, and re-read and delete work without a PDF
        self.assertEqual(c.get(f"{GS}/{rt['id']}").status_code, 200)
        self.assertEqual(c.post(f"{GS}/{rt['id']}/reparse").status_code, 302)
        self.assertEqual(c.post(f"{GS}/{rt['id']}/delete").status_code, 302)
        conn = db.get_db()
        self.assertIsNone(conn.execute("SELECT 1 FROM acs_ratings WHERE doc_number = 'OPSHUB-RF'").fetchone())
        conn.close()

    def test_only_admins_install_and_unknown_code_404s(self):
        self.login("cfi").post(f"{GS}/builtin/RF/install")
        conn = db.get_db()
        self.assertIsNone(conn.execute("SELECT 1 FROM acs_ratings WHERE doc_number = 'OPSHUB-RF'").fetchone())
        conn.close()
        self.assertEqual(self.login("master").post(f"{GS}/builtin/ZZ/install").status_code, 404)
