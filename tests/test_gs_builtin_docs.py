"""BUILTIN-DOCS and DOC-CITE: a built-in course's FAA standard (the CFII PTS) goes into Resources when the
course is installed, and lessons can cite an ACS/PTS by its FAA document number, which finds that
document's Resources row whatever its id is on this OpsHub."""
import os
import tempfile

from harness import OpsHubTestCase
import acs_sync
import db
import ground_lessons
import groundschool as gs

GS = "/flight/groundschool"
PTS = "FAA-S-8081-9E"
PTS_URL = "https://www.faa.gov/training_testing/testing/acs/cfi_instrument_pts_9.pdf"


class BuiltinDocsBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.up = tempfile.mkdtemp()
        self.addCleanup(setattr, gs, "UPLOAD_DIR", gs.UPLOAD_DIR)
        gs.UPLOAD_DIR = self.up
        gs.SCAN_ASYNC = False
        self.addCleanup(setattr, gs, "SCAN_ASYNC", True)

    def install(self, code):
        r = self.login("master").post(f"{GS}/builtin/{code}/install")
        self.assertEqual(r.status_code, 302)
        return self.q1("SELECT * FROM acs_ratings WHERE doc_number = ?",
                       (ground_lessons.builtin_course(code)["doc_number"],))

    def pts_rows(self):
        conn = db.get_db()
        rows = conn.execute("SELECT * FROM acs_resources WHERE designation = ?", (PTS,)).fetchall()
        conn.close()
        return rows


class BuiltinDocsTest(BuiltinDocsBase):
    def test_outline_ships_the_pts(self):
        docs = ground_lessons.builtin_documents(ground_lessons.builtin_course("FII"))
        self.assertEqual([d["designation"] for d in docs], [PTS])
        with open(docs[0]["path"], "rb") as fh:
            self.assertEqual(fh.read(5), b"%PDF-")

    def test_installing_cfii_puts_the_pts_in_resources(self):
        self.assertEqual(self.pts_rows(), [])
        rating = self.install("FII")
        rows = self.pts_rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["doc_type"], "PTS")
        self.assertIn("CFII", r["categories"])
        self.assertIsNone(r["acs_rating_id"])          # the course is still re-read from its outline
        self.assertIsNone(self.q1("SELECT pdf_filename FROM acs_ratings WHERE id = ?", (rating["id"],))["pdf_filename"])
        self.assertTrue(os.path.isfile(os.path.join(self.up, r["filename"])))
        src = self.q1("SELECT * FROM resource_sources WHERE resource_id = ?", (r["id"],))
        self.assertEqual(src["source_url"], PTS_URL)
        n = self.q1("SELECT COUNT(*) AS n FROM acs_resource_pages WHERE resource_id = ?", (r["id"],))["n"]
        self.assertGreater(n, 20)
        body = self.login("master").get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn(PTS, body)

    def test_foi_after_cfii_adds_no_second_copy(self):
        self.install("FII")
        self.install("FOI")
        self.login("master").get(f"{GS}/resources")
        self.assertEqual(len(self.pts_rows()), 1)

    def test_an_existing_upload_of_the_pts_is_kept(self):
        rid = self.exec("INSERT INTO acs_resources (title, doc_type, filename, uploaded_at, designation) "
                        "VALUES ('My PTS copy', 'PTS', 'mine.pdf', ?, ?)", (db.now_iso(), PTS))
        self.install("FII")
        rows = self.pts_rows()
        self.assertEqual([r["id"] for r in rows], [rid])
        self.assertEqual(rows[0]["filename"], "mine.pdf")

    def test_not_installed_means_not_added(self):
        self.login("master").get(f"{GS}/resources")
        self.assertEqual(self.pts_rows(), [])

    def test_pts_is_never_watched_as_the_instrument_acs(self):
        self.assertEqual(acs_sync.default_source_url("Flight Instructor Instrument (CFII)", PTS), PTS_URL)
        self.assertEqual(acs_sync.default_source_url("Flight Instructor Instrument (CFII)", None), PTS_URL)
        self.assertTrue(acs_sync.default_source_url("Instrument Rating - Airplane", "FAA-S-ACS-8C")
                        .endswith("instrument_rating_airplane_acs_8.pdf"))
        self.assertIsNone(acs_sync.default_source_url("Some PTS", "FAA-S-8081-99Z"))


class DocCiteTest(BuiltinDocsBase):
    def render(self, text):
        with self.app.test_request_context("/"):
            return str(ground_lessons.rich(text))

    def add(self, designation, url=None):
        rid = self.exec("INSERT INTO acs_resources (title, doc_type, filename, uploaded_at, designation) "
                        "VALUES ('x', 'ACS', 'x.pdf', ?, ?)", (db.now_iso(), designation))
        if url:
            self.exec("INSERT INTO resource_sources (resource_id, source_url, source_kind) VALUES (?, ?, 'pdf')",
                      (rid, url))
        return rid

    def test_number_cites_still_work(self):
        self.assertIn("/resources/23/excerpt?pages=180", self.render("x [[23:180|IFH 7-19]]").replace("&amp;", "&"))

    def test_cites_by_document_number(self):
        self.add("FAA-S-ACS-6C")
        rid = self.add("FAA-S-ACS-8C")
        html = self.render("Tolerances [[FAA-S-ACS-8C:12|IR ACS IR.VI.B]].")
        self.assertIn(f"/resources/{rid}/excerpt?pages=12", html)
        self.assertIn(">IR ACS IR.VI.B</a>", html)

    def test_newer_revision_and_faa_address_are_found(self):
        rid = self.add("FAA-S-ACS-8D")
        self.assertIn(f"/resources/{rid}/", self.render("[[FAA-S-ACS-8C:3|IR ACS]]"))
        rid2 = self.add(None, "https://www.faa.gov/training_testing/testing/acs/cfi_airplane_acs_25.pdf")
        self.assertIn(f"/resources/{rid2}/", self.render("[[FAA-S-ACS-25:3|CFI ACS]]"))

    def test_missing_document_shows_its_label_without_a_link(self):
        html = self.render("See [[FAA-S-ACS-8C:12|IR ACS IR.VI.B]].")
        self.assertIn('<span class="gsl-cite">IR ACS IR.VI.B</span>', html)
        self.assertNotIn("href", html)

    def test_reference_list_handles_both_kinds(self):
        rid = self.add(PTS)
        with self.app.test_request_context("/"):
            refs = ground_lessons.references({"p": "[[FAA-S-8081-9E:9|PTS VI.B]] [[23:180|IFH 7-19]] [[FAA-S-ACS-8C:5|IR ACS]]"})
        d = dict(refs)
        self.assertIn(f"/resources/{rid}/", d["PTS VI.B"])
        self.assertIsNone(d["IR ACS"])
        self.assertIn("/resources/23/", d["IFH 7-19"])
