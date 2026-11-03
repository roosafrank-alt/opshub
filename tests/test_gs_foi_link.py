"""GS-FOI-LINK: the Fundamentals of Instructing is its own built-in course, and the CFII course
(FAA-S-8081-9E, built-in) and any course with an FOI Area point to it."""
import json
import os

from harness import OpsHubTestCase
import db
import ground_lessons

GS = "/flight/groundschool"


class FoiLinkTest(OpsHubTestCase):
    def _install(self, code):
        c = self.login("master")
        r = c.post(f"{GS}/builtin/{code}/install")
        self.assertEqual(r.status_code, 302)
        return self.q1("SELECT * FROM acs_ratings WHERE doc_number = ?",
                       (ground_lessons.builtin_course(code)["doc_number"],))

    def test_outlines_are_well_formed(self):
        for code in ("FII", "FOI"):
            c = ground_lessons.builtin_course(code)
            self.assertTrue(c, code)
            seen = set()
            for a in c["areas"]:
                for t in a["tasks"]:
                    els = t["knowledge"] + t["skills"]
                    self.assertTrue(els, (code, a["code"], t["code"]))
                    for e in els:
                        self.assertTrue(e["code"].startswith(f"{code}.{a['code']}.{t['code']}."), e["code"])
                        self.assertNotIn(e["code"], seen)
                        seen.add(e["code"])
        fii = ground_lessons.builtin_course("FII")
        self.assertEqual([a["code"] for a in fii["areas"]], ["II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"])
        self.assertEqual([t["code"] for t in fii["areas"][7]["tasks"]], ["A", "B"])  # IX: no multiengine C, D
        self.assertEqual(ground_lessons.see_also("FAA-S-8081-9E")[0]["doc_number"], ground_lessons.FOI_DOC)

    def test_cfii_points_to_foi_course_once_installed(self):
        fii = self._install("FII")
        body = self.login("master").get(f"{GS}/{fii['id']}").get_data(as_text=True)
        self.assertIn("Area I. Fundamentals of Instructing", body)
        self.assertIn("Taught in its own course", body)
        self.assertIn("Fundamentals of Instructing (FOI).", body)   # named, not yet a link
        foi = self._install("FOI")
        body = self.login("master").get(f"{GS}/{fii['id']}").get_data(as_text=True)
        self.assertIn(f'{GS}/{foi["id"]}', body)
        # the FOI course itself shows no link to itself
        body = self.login("master").get(f"{GS}/{foi['id']}").get_data(as_text=True)
        self.assertNotIn("gs-foi-link", body)

    def test_cfi_course_foi_area_links_to_foi_course(self):
        foi = self._install("FOI")
        rid = self.exec("INSERT INTO acs_ratings (name, slug, doc_number, uploaded_at, status) VALUES (?,?,?,?,?)",
                        ("Flight Instructor Airplane", "cfi", "FAA-S-ACS-25", db.now_iso(), "parsed"))
        self.exec("INSERT INTO acs_areas (rating_id, code, title, order_index) VALUES (?,?,?,0)",
                  (rid, "I", "Fundamentals of Instructing"))
        self.exec("INSERT INTO acs_areas (rating_id, code, title, order_index) VALUES (?,?,?,1)",
                  (rid, "II", "Technical Subject Areas"))
        body = self.login("master").get(f"{GS}/{rid}").get_data(as_text=True)
        self.assertEqual(body.count("gs-foi-link"), 1)
        self.assertIn(f'{GS}/{foi["id"]}', body)
