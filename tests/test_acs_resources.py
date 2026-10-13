"""ACS-RESOURCES: an ACS course also lives in Resources, is watched for new FAA versions like any
other resource, and approving a new version merges it into the Ground School course in place, so
lesson plans, sign-offs and reading progress survive and changed Tasks are flagged for a refresh."""
import os
import tempfile
from harness import OpsHubTestCase
import db
import groundschool as gs
import resource_scan as rs
import acs_sync

GS = "/flight/groundschool"
GOOD_PDF = b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\n%%EOF"
URL = "https://www.faa.gov/training_testing/testing/acs/private_airplane_acs_6.pdf"


def el(code, text):
    return {"code": code, "text": text}


def parsed(k1="Know the thing.", with_k2=True, with_k3=False, task_b=False, extra_tasks=0):
    knowledge = [el("PA.I.A.K1", k1)] + ([el("PA.I.A.K2", "Second thing.")] if with_k2 else []) + \
                ([el("PA.I.A.K3", "Brand new thing.")] if with_k3 else [])
    tasks = [{"code": "A", "title": "Pilot Qualifications", "references": "14 CFR 61", "objective": "Be qualified.",
              "notes": [], "knowledge": knowledge, "risk_management": [], "skills": [el("PA.I.A.S1", "Do it.")]}]
    if task_b:
        tasks.append({"code": "B", "title": "Airworthiness", "references": "14 CFR 91", "objective": "Be airworthy.",
                      "notes": [], "knowledge": [el("PA.I.B.K1", "Airworthy.")], "risk_management": [], "skills": []})
    for i in range(extra_tasks):
        tasks.append({"code": chr(ord("C") + i), "title": f"Filler {i}", "references": "", "objective": "x",
                      "notes": [], "knowledge": [el(f"PA.I.{chr(ord('C') + i)}.K1", "x")], "risk_management": [],
                      "skills": []})
    return [{"code": "I", "title": "Preflight Preparation", "tasks": tasks}]


class AcsBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.q_dir = tempfile.mkdtemp()
        gs.SCAN_ASYNC = False
        self.addCleanup(setattr, gs, "SCAN_ASYNC", True)
        self._orig_parse = acs_sync.parse_pdf
        self.addCleanup(setattr, acs_sync, "parse_pdf", self._orig_parse)
        self._orig_q = rs.quarantine_dir
        self.addCleanup(setattr, rs, "quarantine_dir", self._orig_q)
        rs.quarantine_dir = lambda d=self.q_dir: d
        # a course built from the "old" ACS, with a lesson plan and student work on it
        conn = db.get_db()
        self.rating = conn.execute(
            "INSERT INTO acs_ratings (name, slug, doc_number, pdf_filename, uploaded_at, status) "
            "VALUES ('Private Pilot - Airplane', 'ppl', 'FAA-S-ACS-6C', 'acs_old.pdf', ?, 'parsed')",
            (db.now_iso(),)).lastrowid
        conn.commit()
        acs_sync.apply_acs(conn, self.rating, parsed(task_b=True))
        conn.commit()
        self.tA = conn.execute("SELECT id FROM acs_tasks WHERE code = 'A'").fetchone()["id"]
        self.k1 = conn.execute("SELECT id FROM acs_task_elements WHERE code = 'PA.I.A.K1'").fetchone()["id"]
        self.k2 = conn.execute("SELECT id FROM acs_task_elements WHERE code = 'PA.I.A.K2'").fetchone()["id"]
        conn.execute("INSERT INTO acs_lesson_content (task_id, content, updated_at) VALUES (?, 'My lesson plan', ?)",
                     (self.tA, db.now_iso()))
        conn.execute("INSERT INTO acs_element_lesson_items (element_id, order_index, kind, title, required) "
                     "VALUES (?,1,'link','Read it',1)", (self.k1,))
        conn.commit()
        conn.close()

    def new_pdf(self, structure):
        acs_sync.parse_pdf = lambda path: structure


class SyncTest(AcsBase):
    def test_course_appears_in_resources_with_faa_source_and_is_not_duplicated(self):
        c = self.login("master")
        body = c.get(f"{GS}/resources").get_data(as_text=True)
        r = self.q1("SELECT * FROM acs_resources WHERE acs_rating_id = ?", (self.rating,))
        self.assertEqual((r["title"], r["doc_type"], r["filename"], r["designation"]),
                         ("Private Pilot - Airplane", "ACS", "acs_old.pdf", "FAA-S-ACS-6C"))
        self.assertEqual(r["categories"], "Private")
        self.assertIn("Private Pilot - Airplane", body)
        s = self.q1("SELECT * FROM resource_sources WHERE resource_id = ?", (r["id"],))
        self.assertEqual((s["source_url"], s["source_kind"]), (URL, "pdf_link"))
        c.get(f"{GS}/resources")
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM acs_resources")["n"], 1)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM resource_sources")["n"], 1)

    def test_source_choice_by_doc_number_then_name(self):
        self.assertTrue(acs_sync.default_source_url("Whatever", "FAA-S-ACS-8C").endswith("instrument_rating_airplane_acs_8.pdf"))
        self.assertTrue(acs_sync.default_source_url("Commercial Pilot", None).endswith("commercial_airplane_acs_7.pdf"))
        self.assertIsNone(acs_sync.default_source_url("Mystery course", None))

    def test_acs_resource_cannot_be_deleted_from_resources(self):
        c = self.login("master")
        c.get(f"{GS}/resources")
        rid = self.q1("SELECT id FROM acs_resources")["id"]
        c.post(f"{GS}/resources/{rid}/delete")
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM acs_resources")["n"], 1)

    def test_deleting_the_course_takes_its_resource_row_too(self):
        c = self.login("master")
        c.get(f"{GS}/resources")
        c.post(f"{GS}/{self.rating}/delete")
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM acs_resources")["n"], 0)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM resource_sources")["n"], 0)


class MergeTest(AcsBase):
    def test_merge_keeps_ids_lessons_and_flags_changed_tasks(self):
        conn = db.get_db()
        rep = acs_sync.apply_acs(conn, self.rating, parsed(k1="Know the thing, and more.", with_k2=False,
                                                           with_k3=True, task_b=True), update_id=7)
        conn.commit()
        conn.close()
        self.assertEqual(self.q1("SELECT id FROM acs_tasks WHERE code = 'A'")["id"], self.tA)      # same task row
        self.assertEqual(self.q1("SELECT text FROM acs_task_elements WHERE id = ?", (self.k1,))["text"],
                         "Know the thing, and more.")
        self.assertEqual(self.q1("SELECT content FROM acs_lesson_content WHERE task_id = ?", (self.tA,))["content"],
                         "My lesson plan")
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM acs_element_lesson_items WHERE element_id = ?", (self.k1,))["n"], 1)
        self.assertIsNone(self.q1("SELECT id FROM acs_task_elements WHERE id = ?", (self.k2,)))    # untouched: deleted
        self.assertIsNotNone(self.q1("SELECT id FROM acs_task_elements WHERE code = 'PA.I.A.K3'"))
        kinds = sorted(r["kind"] for r in self.q("SELECT kind FROM acs_task_changes WHERE task_id = ?", (self.tA,)))
        self.assertEqual(kinds, ["added", "changed", "removed"])
        self.assertEqual([lbl for _t, lbl in rep["lessons"]], ["Area I Task A (Pilot Qualifications)"])

    def test_dropped_element_with_lesson_work_is_retired_not_deleted(self):
        conn = db.get_db()
        parsed_new = parsed()
        parsed_new[0]["tasks"][0]["knowledge"] = [el("PA.I.A.K2", "Second thing.")]    # K1 (has a lesson item) is gone
        acs_sync.apply_acs(conn, self.rating, parsed_new)
        conn.commit()
        conn.close()
        row = self.q1("SELECT retired_at FROM acs_task_elements WHERE id = ?", (self.k1,))
        self.assertTrue(row["retired_at"])
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM acs_element_lesson_items WHERE element_id = ?", (self.k1,))["n"], 1)

    def test_dropped_task_with_a_lesson_is_retired_and_without_is_deleted(self):
        conn = db.get_db()
        new = parsed(extra_tasks=4)           # drops Task B (no work), keeps A; adds C-F so the sanity check passes
        acs_sync.apply_acs(conn, self.rating, new)
        conn.commit()
        self.assertIsNone(self.q1("SELECT id FROM acs_tasks WHERE code = 'B'"))
        new2 = parsed(extra_tasks=4)
        new2[0]["tasks"] = [t for t in new2[0]["tasks"] if t["code"] != "A"]
        acs_sync.apply_acs(conn, self.rating, new2)
        conn.commit()
        conn.close()
        self.assertTrue(self.q1("SELECT retired_at FROM acs_tasks WHERE code = 'A'")["retired_at"])

    def test_unreadable_new_acs_is_refused(self):
        conn = db.get_db()
        acs_sync.apply_acs(conn, self.rating, parsed(task_b=True, extra_tasks=4))
        conn.commit()
        with self.assertRaises(ValueError):
            acs_sync.apply_acs(conn, self.rating, parsed())       # 1 of 6 Tasks found
        conn.close()

    def test_task_page_flags_lesson_and_resolve_clears_it(self):
        conn = db.get_db()
        acs_sync.apply_acs(conn, self.rating, parsed(k1="Changed text."), update_id=1)
        conn.commit()
        conn.close()
        c = self.login("master")
        self.assertIn("Refresh this lesson plan", c.get(f"{GS}/task/{self.tA}").get_data(as_text=True))
        self.assertIn("Lesson needs refresh", c.get(f"{GS}/{self.rating}").get_data(as_text=True))
        c.post(f"{GS}/task/{self.tA}/changes/resolve")
        self.assertNotIn("Refresh this lesson plan", c.get(f"{GS}/task/{self.tA}").get_data(as_text=True))
        self.assertNotIn("Lesson needs refresh", c.get(f"{GS}/{self.rating}").get_data(as_text=True))

    def test_reparse_keeps_lessons(self):
        acs_sync.parse_pdf = lambda path: parsed(task_b=True)
        c = self.login("master")
        c.post(f"{GS}/{self.rating}/reparse")
        self.assertEqual(self.q1("SELECT content FROM acs_lesson_content WHERE task_id = ?", (self.tA,))["content"],
                         "My lesson plan")
        self.assertEqual(self.q1("SELECT status FROM acs_ratings WHERE id = ?", (self.rating,))["status"], "parsed")


class UpdateFlowTest(AcsBase):
    def setUp(self):
        super().setUp()
        self.c = self.login("master")
        self.c.get(f"{GS}/resources")                                       # creates the Resources row + source
        self.rid = self.q1("SELECT id FROM acs_resources WHERE acs_rating_id = ?", (self.rating,))["id"]
        self.exec("INSERT INTO acs_resource_pages VALUES (?,?,?)", (self.rid, 1, "Old ACS page text"))

    def scan(self, body):
        def fetch(url, max_bytes=None):
            return 200, {}, body
        conn = db.get_db()
        out = rs.run_scan(conn, "test", fetch=fetch, extract=lambda p: [(1, "New ACS page text")],
                          sleep=lambda s: None, qdir=self.q_dir)
        conn.close()
        return out

    def test_new_faa_version_is_queued_with_the_course_changes_then_merged_on_approve(self):
        self.new_pdf(parsed(k1="Know the thing, and more.", with_k2=False, with_k3=True, task_b=True))
        self.scan(GOOD_PDF)                                                  # baseline
        out = self.scan(GOOD_PDF + b"\n%FAA edition 6D")
        self.assertEqual(out["found"], 1)
        u = self.q1("SELECT * FROM resource_updates")
        self.assertIn("ACS standards: 1 Task changed", u["summary"])
        self.assertIn("PA.I.A.K1 was", u["summary"])
        self.assertIn("[lesson plan to refresh]", u["summary"])
        body = self.c.get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn("also updates the lessons", body)
        # nothing is merged before approval
        self.assertEqual(self.q1("SELECT text FROM acs_task_elements WHERE id = ?", (self.k1,))["text"], "Know the thing.")
        self.assertEqual(self.q1("SELECT pdf_filename FROM acs_ratings")["pdf_filename"], "acs_old.pdf")
        resp = self.c.post(f"{GS}/resources/updates/{u['id']}/approve")
        self.assertIn("/send", resp.headers["Location"])
        r = self.q1("SELECT * FROM acs_ratings")
        self.assertNotEqual(r["pdf_filename"], "acs_old.pdf")
        self.assertEqual(r["pdf_filename"], self.q1("SELECT filename FROM acs_resources")["filename"])
        self.assertEqual(self.q1("SELECT text FROM acs_task_elements WHERE id = ?", (self.k1,))["text"],
                         "Know the thing, and more.")
        self.assertEqual(self.q1("SELECT content FROM acs_lesson_content WHERE task_id = ?", (self.tA,))["content"],
                         "My lesson plan")
        self.assertIn("lesson plan", self.q1("SELECT acs_report FROM resource_updates")["acs_report"])
        self.assertIn("What this did to the Ground School course",
                      self.c.get(f"{GS}/resources/updates/{u['id']}/send").get_data(as_text=True))
        self.assertIn("Refresh this lesson plan", self.c.get(f"{GS}/task/{self.tA}").get_data(as_text=True))

    def test_unreadable_new_acs_publishes_file_but_leaves_lessons_alone(self):
        def boom(path):
            raise ValueError("layout not recognised")
        acs_sync.parse_pdf = boom
        self.scan(GOOD_PDF)
        self.scan(GOOD_PDF + b"\n%FAA edition 6D")
        u = self.q1("SELECT * FROM resource_updates")
        self.assertIn("couldn't read the Tasks", u["summary"])
        self.c.post(f"{GS}/resources/updates/{u['id']}/approve")
        self.assertEqual(self.q1("SELECT status FROM resource_updates")["status"], "approved")
        self.assertEqual(self.q1("SELECT text FROM acs_task_elements WHERE id = ?", (self.k1,))["text"], "Know the thing.")
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM acs_task_changes")["n"], 0)
        self.assertIn("could not be merged", self.q1("SELECT acs_report FROM resource_updates")["acs_report"])


class FileLessonTest(AcsBase):
    def test_a_lesson_plan_file_counts_as_a_built_lesson(self):
        conn = db.get_db()
        conn.execute("DELETE FROM acs_lesson_content")
        conn.execute("DELETE FROM acs_element_lesson_items")
        conn.commit()
        self.assertEqual(acs_sync.lesson_task_keys(conn, self.rating), {("I", "A"), ("I", "B")})  # PA.I.A.json and PA.I.B.json ship
        conn.close()
