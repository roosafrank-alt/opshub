"""Flight Academy Resources update scanner: allow-list, PDF safety, version compare, change summary,
and the approve / reject / NEW flow. Saved sample pages only, never the internet."""
import json
import os
import tempfile
from harness import OpsHubTestCase
import db
import groundschool as gs
import resource_scan as rs

GS = "/flight/groundschool"
HERE = os.path.dirname(os.path.abspath(__file__))
OLD = [(1, "91.103 Preflight action\nEach pilot must become familiar with all available information.\n"
           "91.105 Flight crewmembers at stations\nCrew stay at stations.\n91.107 Seat belts\nWear them.")]
NEW = [(1, "91.103 Preflight action\nEach pilot must become familiar with all available information and runway lengths.\n"
           "91.105 Flight crewmembers at stations\nCrew stay at stations.\n91.109 Flight instruction\nNew rule text here."),
       ]


def sample(name):
    with open(os.path.join(HERE, "samples", name), "rb") as f:
        return f.read()


GOOD_PDF = b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\n%%EOF"


class ScanPureTest(OpsHubTestCase):
    def test_host_allow_list(self):
        for ok in ("https://www.faa.gov/x", "https://faa.gov/a.pdf", "https://www.ecfr.gov/current/title-14/part-91",
                   "https://govinfo.gov/x"):
            rs.check_host(ok)
        for bad in ("https://evil.com/faa.gov", "http://www.faa.gov/x", "https://www.faa.gov.evil.com/",
                    "https://user:pw@www.faa.gov/", "ftp://faa.gov/x", "https://example.org", ""):
            with self.assertRaises(rs.BlockedHost):
                rs.check_host(bad)

    def test_kinds(self):
        self.assertEqual(rs.detect_kind("https://www.ecfr.gov/current/title-14/part-91"), "ecfr")
        self.assertEqual(rs.detect_kind("https://www.faa.gov/a/b.pdf"), "pdf_link")
        self.assertEqual(rs.detect_kind("https://www.faa.gov/regulations_policies/advisory_circulars/index"), "faa_page")

    def test_pdf_safety(self):
        self.assertTrue(rs.validate_pdf(GOOD_PDF)[0])
        self.assertFalse(rs.validate_pdf(b"<html>nope")[0])
        self.assertFalse(rs.validate_pdf(b"%PDF-1.4 /OpenAction 3 0 R")[0])
        for tok in (b"/JavaScript", b"/JS (x)", b"/Launch", b"/EmbeddedFile"):
            self.assertFalse(rs.validate_pdf(b"%PDF-1.4 " + tok)[0], tok)
        self.assertTrue(rs.validate_pdf(b"%PDF-1.4 /JSON")[0])
        self.assertFalse(rs.validate_pdf(b"%PDF" + b"0" * (rs.MAX_PDF_BYTES + 1))[0])

    def test_version_compare(self):
        self.assertFalse(rs.is_new_version("", "2026-01-01"))       # first look is only a baseline
        self.assertFalse(rs.is_new_version("2026-01-01", "2026-01-01"))
        self.assertTrue(rs.is_new_version("2026-01-01", "2026-09-12"))

    def test_parse_samples(self):
        self.assertEqual(rs.parse_ecfr_versions(sample("ecfr_versions_part91.json")), "2026-09-12")
        info = rs.parse_faa_page(sample("faa_page_ac.html").decode(), "https://www.faa.gov/ac/61-65.html")
        self.assertEqual(info["pdf_url"], "https://www.faa.gov/documents/AC_61-65H.pdf")
        self.assertEqual(info["revision"], "Change 2")
        self.assertEqual(info["date"], "March 3, 2026")
        self.assertIn("title-14.json?part=91", rs.ecfr_api_url("https://www.ecfr.gov/current/title-14/chapter-I/part-91"))

    def test_summary(self):
        s = rs.build_summary(OLD, NEW)
        self.assertTrue(s.startswith("1 sections changed, 1 added, 1 removed."), s)
        self.assertIn("Changed: Section 91.103 (page 1)", s)
        self.assertIn("runway lengths", s)
        self.assertIn("Added: Section 91.109", s)
        self.assertIn("Removed: Section 91.107", s)

    def test_summary_too_big_shows_biggest(self):
        old = [(1, "\n".join(f"{i}.{i} Rule {i}\nold text {i}" for i in range(1, 60)))]
        new = [(1, "\n".join(f"{i}.{i} Rule {i}\nnew words {i} here" for i in range(1, 60)))]
        s = rs.build_summary(old, new)
        self.assertIn("Showing the biggest 25 of 59", s)
        self.assertEqual(len(s.splitlines()), 2 + 25)


class ScanFlowTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.q_dir = tempfile.mkdtemp()
        self.exec("INSERT INTO acs_resources (title, designation, doc_type, filename, uploaded_at, doc_date) "
                  "VALUES ('Rules', '14 CFR Part 91', '14 CFR Part', 'old.pdf', ?, '2025-01-02')", (db.now_iso(),))
        self.rid = self.q1("SELECT id FROM acs_resources")["id"]
        for i, (n, t) in enumerate(OLD, 1):
            self.exec("INSERT INTO acs_resource_pages VALUES (?,?,?)", (self.rid, n, t))
        gs.SCAN_ASYNC = False
        self.addCleanup(setattr, gs, "SCAN_ASYNC", True)
        self.pages = {"v": NEW}

    def scan(self, answers):
        calls = []

        def fetch(url, max_bytes=None):
            calls.append(url)
            a = answers[url]
            if isinstance(a, Exception):
                raise a
            return 200, {}, a
        conn = db.get_db()
        out = rs.run_scan(conn, "test", fetch=fetch, extract=lambda p: self.pages["v"], sleep=lambda s: None,
                          qdir=self.q_dir)
        conn.close()
        return out, calls

    def add_source(self, url):
        c = self.login("master")
        c.post(f"{GS}/resources/{self.rid}/edit", data={"title": "Rules", "source_url": url})
        return c

    def test_source_box_saves_and_refuses_other_hosts(self):
        c = self.add_source("https://www.ecfr.gov/current/title-14/part-91")
        self.assertEqual(self.q1("SELECT source_kind FROM resource_sources")["source_kind"], "ecfr")
        c.post(f"{GS}/resources/{self.rid}/edit", data={"title": "Rules", "source_url": "https://evil.com/x.pdf"})
        self.assertEqual(self.q1("SELECT source_url FROM resource_sources")["source_url"],
                         "https://www.ecfr.gov/current/title-14/part-91")
        c.post(f"{GS}/resources/{self.rid}/edit", data={"title": "Rules", "source_url": ""})
        self.assertIsNone(self.q1("SELECT * FROM resource_sources"))

    def test_pdf_link_update_and_approve(self):
        url = "https://www.faa.gov/docs/part91.pdf"
        c = self.add_source(url)
        out, _ = self.scan({url: GOOD_PDF})                       # first look: baseline only
        self.assertEqual((out["found"], self.q1("SELECT COUNT(*) n FROM resource_updates")["n"]), (0, 0))
        out, _ = self.scan({url: GOOD_PDF + b"\n%changed"})
        self.assertEqual(out["found"], 1)
        u = self.q1("SELECT * FROM resource_updates")
        self.assertEqual(u["status"], "pending")
        self.assertIn("1 sections changed", u["summary"])
        self.assertTrue(os.path.isfile(os.path.join(self.q_dir, u["new_filename"])))
        self.assertEqual(self.q1("SELECT filename FROM acs_resources")["filename"], "old.pdf")   # not replaced yet
        out, _ = self.scan({url: GOOD_PDF + b"\n%changed"})                                       # same version: no repeat
        self.assertEqual(out["found"], 0)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM resource_updates")["n"], 1)
        body = c.get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn("1 update waiting for approval", body)
        self.assertIn("Approve &amp; Publish", body)
        # approving needs the quarantine folder the app uses
        import resource_scan
        resource_scan.quarantine_dir = lambda d=self.q_dir: d
        c.post(f"{GS}/resources/updates/{u['id']}/approve")
        r = self.q1("SELECT * FROM acs_resources")
        self.assertEqual(r["prev_filename"], "old.pdf")
        self.assertNotEqual(r["filename"], "old.pdf")
        self.assertTrue(r["new_until"])
        self.assertEqual(self.q1("SELECT status FROM resource_updates")["status"], "approved")
        self.assertEqual(self.q1("SELECT text FROM acs_resource_pages")["text"], NEW[0][1])
        body = c.get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn(">NEW<", body)
        self.assertIn("Previous version", body)
        self.assertNotIn("waiting for approval", body)

    def test_reject_keeps_old_and_does_not_reappear(self):
        url = "https://www.faa.gov/docs/part91.pdf"
        c = self.add_source(url)
        self.scan({url: GOOD_PDF})
        self.scan({url: GOOD_PDF + b"\n%v2"})
        uid = self.q1("SELECT id FROM resource_updates")["id"]
        c.post(f"{GS}/resources/updates/{uid}/reject")
        self.assertEqual(self.q1("SELECT status FROM resource_updates")["status"], "rejected")
        self.assertEqual(self.q1("SELECT filename FROM acs_resources")["filename"], "old.pdf")
        self.scan({url: GOOD_PDF + b"\n%v2"})
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM resource_updates")["n"], 1)

    def test_unsafe_pdf_is_not_kept(self):
        url = "https://www.faa.gov/docs/part91.pdf"
        self.add_source(url)
        self.scan({url: GOOD_PDF})
        self.scan({url: b"%PDF-1.4 /JavaScript (app.alert(1))"})
        u = self.q1("SELECT * FROM resource_updates")
        self.assertIsNone(u["new_filename"])
        self.assertIn("failed the safety check", u["summary"])
        self.assertEqual(os.listdir(self.q_dir), [])

    def test_ecfr_notice_and_failure_does_not_stop_others(self):
        self.add_source("https://www.ecfr.gov/current/title-14/part-91")
        self.exec("INSERT INTO acs_resources (title, filename, uploaded_at) VALUES ('Other','o.pdf',?)", (db.now_iso(),))
        rid2 = self.q("SELECT id FROM acs_resources ORDER BY id")[1]["id"]
        self.exec("INSERT INTO resource_sources (resource_id, source_url, source_kind) VALUES (?,?,?)",
                  (rid2, "https://www.faa.gov/down.pdf", "pdf_link"))
        api = rs.ecfr_api_url("https://www.ecfr.gov/current/title-14/part-91")
        old = json.dumps({"content_versions": [{"amendment_date": "2025-01-02"}]}).encode()
        self.scan({api: old, "https://www.faa.gov/down.pdf": OSError("timed out")})
        s = self.q("SELECT status, last_message FROM resource_sources ORDER BY id")
        self.assertEqual(s[1]["status"], "failed")
        self.assertIn("Could not reach", s[1]["last_message"])
        out, _ = self.scan({api: sample("ecfr_versions_part91.json"), "https://www.faa.gov/down.pdf": OSError("x")})
        self.assertEqual(out["found"], 1)
        u = self.q1("SELECT * FROM resource_updates")
        self.assertIsNone(u["new_filename"])
        self.assertEqual(u["new_doc_date"], "2026-09-12")
        self.assertIn("last amended on 2026-09-12", u["summary"])

    def test_blocked_host_source_is_refused_by_scanner(self):
        self.exec("INSERT INTO resource_sources (resource_id, source_url, source_kind, last_seen_version) VALUES (?,?,?,?)",
                  (self.rid, "https://evil.example.com/a.pdf", "pdf_link", "x"))
        called = []
        conn = db.get_db()
        out = rs.run_scan(conn, "t", sleep=lambda s: None, qdir=self.q_dir)   # real fetch: must refuse before any network
        conn.close()
        self.assertEqual(out["failed"], 1)
        self.assertIn("Blocked", self.q1("SELECT last_message FROM resource_sources")["last_message"])

    def test_check_now_and_visibility(self):
        c = self.add_source("https://www.faa.gov/docs/part91.pdf")
        import resource_scan
        orig = resource_scan.default_fetch
        resource_scan.default_fetch = lambda u, m=None: (200, {}, GOOD_PDF)
        self.addCleanup(setattr, resource_scan, "default_fetch", orig)
        orig_run = resource_scan.run_scan
        resource_scan.run_scan = lambda conn, trig="x", **k: orig_run(conn, trig, fetch=resource_scan.default_fetch,
                                                                       sleep=lambda s: None, **k)
        self.addCleanup(setattr, resource_scan, "run_scan", orig_run)
        c.post(f"{GS}/resources/scan")
        self.assertEqual(self.q1("SELECT checked FROM resource_scan_runs")["checked"], 1)
        body = c.get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn("Update checks", body)
        self.assertIn("Up to date (now watching)", body)
        stu = self.login("flight_student")
        sb = stu.get(f"{GS}/resources").get_data(as_text=True)
        self.assertNotIn("Update checks", sb)
        self.assertEqual(stu.post(f"{GS}/resources/scan").status_code, 302)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM resource_scan_runs")["n"], 1)
