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
        resp = c.post(f"{GS}/resources/updates/{u['id']}/approve")
        self.assertIn(f"/resources/updates/{u['id']}/send", resp.headers["Location"])   # the "Send this update to..." step
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


def ecfr_json(date):
    return json.dumps({"content_versions": [{"amendment_date": date}]}).encode()


AIM_HTML = b"<html><body><h1>Aeronautical Information Manual</h1><p>Basic with Change 2. Effective: April 3, 2025</p></body></html>"
MULTI = "14: 1, 91; 49: 830"


class BigAndAlertSourcesTest(ScanFlowTest):
    """Big PDFs checked by headers, one file made of several eCFR parts, and the alert-only AIM."""

    def scan(self, answers, heads=None):
        calls = []

        def fetch(url, max_bytes=None, method="GET"):
            calls.append((method, url))
            a = (heads or {})[url] if method == "HEAD" else answers[url]
            if isinstance(a, Exception):
                raise a
            return 200, ({} if method == "GET" else a), (b"" if method == "HEAD" else a)
        conn = db.get_db()
        out = rs.run_scan(conn, "test", fetch=fetch, extract=lambda p: self.pages["v"], sleep=lambda s: None,
                          qdir=self.q_dir)
        conn.close()
        return out, calls

    def test_limit_and_kinds(self):
        self.assertEqual(rs.MAX_PDF_BYTES, 200 * 1024 * 1024)
        self.assertEqual(rs.detect_kind(MULTI), "ecfr_multi")
        self.assertEqual(rs.detect_kind("https://www.faa.gov/air_traffic/publications/atpubs/aim_html/"), "faa_alert")
        self.assertEqual(rs.normalize_ecfr_multi("14:1,91 ;49:830;"), MULTI)
        for bad in ("14: x", "evil", "14:"):
            with self.assertRaises((ValueError, rs.BlockedHost)):
                rs.normalize_ecfr_multi(bad) if bad != "evil" else rs.detect_kind(bad)

    def test_unchanged_big_pdf_is_never_downloaded(self):
        url = "https://www.faa.gov/docs/big.pdf"
        self.add_source(url)
        heads = {url: {"etag": '"a1"', "content-length": "80000000", "last-modified": "Mon, 01 Sep 2025"}}
        out, calls = self.scan({}, heads)
        self.assertEqual(calls, [("HEAD", url)])               # baseline from headers alone
        out, calls = self.scan({}, heads)
        self.assertEqual((out["found"], calls), (0, [("HEAD", url)]))
        self.assertEqual(self.q1("SELECT last_message FROM resource_sources")["last_message"], "Up to date")

    def test_changed_big_pdf_is_downloaded_to_quarantine(self):
        url = "https://www.faa.gov/docs/big.pdf"
        self.add_source(url)
        self.scan({}, {url: {"etag": '"a1"'}})
        out, calls = self.scan({url: GOOD_PDF}, {url: {"etag": '"a2"'}})
        self.assertEqual(out["found"], 1)
        self.assertEqual(calls, [("HEAD", url), ("GET", url)])
        u = self.q1("SELECT * FROM resource_updates")
        self.assertTrue(os.path.isfile(os.path.join(self.q_dir, u["new_filename"])))

    def test_old_hash_baseline_does_not_cause_a_false_update(self):
        url = "https://www.faa.gov/docs/big.pdf"
        self.exec("INSERT INTO resource_sources (resource_id, source_url, source_kind, last_seen_version) VALUES (?,?,?,?)",
                  (self.rid, url, "pdf_link", "0123456789abcdef"))
        out, _ = self.scan({}, {url: {"etag": '"a1"'}})
        self.assertEqual((out["found"], out["failed"]), (0, 0))
        self.assertEqual(self.q1("SELECT last_seen_version FROM resource_sources")["last_seen_version"], 'hdr:"a1"||')

    def test_head_trouble_falls_back_to_a_full_look(self):
        url = "https://www.faa.gov/docs/big.pdf"
        self.add_source(url)
        out, calls = self.scan({url: GOOD_PDF}, {url: OSError("405")})
        self.assertEqual(calls, [("HEAD", url), ("GET", url)])
        self.assertEqual(out["failed"], 0)

    def test_multi_part_file_alerts_with_parts_and_dates(self):
        c = self.add_source(MULTI)
        self.assertEqual(self.q1("SELECT source_kind, source_url FROM resource_sources")["source_kind"], "ecfr_multi")
        api = lambda t, p: rs.ecfr_api_url(f"https://www.ecfr.gov/current/title-{t}/part-{p}")
        old = {api(14, 1): ecfr_json("2024-05-01"), api(14, 91): ecfr_json("2025-01-02"), api(49, 830): ecfr_json("2020-01-01")}
        out, calls = self.scan(old)                                  # doc_date is 2025-01-02: nothing newer
        self.assertEqual((out["found"], len(calls)), (0, 3))
        new = dict(old, **{api(14, 91): ecfr_json("2026-09-12"), api(49, 830): ecfr_json("2026-02-02")})
        out, _ = self.scan(new)
        self.assertEqual(out["found"], 1)
        u = self.q1("SELECT * FROM resource_updates")
        self.assertIsNone(u["new_filename"])
        self.assertIn("2 of the 3 eCFR parts", u["summary"])
        self.assertIn("14 CFR Part 91 (amended 2026-09-12)", u["summary"])
        self.assertIn("49 CFR Part 830 (amended 2026-02-02)", u["summary"])
        self.assertNotIn("Part 1 (", u["summary"])
        self.assertEqual(os.listdir(self.q_dir), [])                  # nothing built or fetched
        out, _ = self.scan(new)                                       # same state: no second alert
        self.assertEqual((out["found"], self.q1("SELECT COUNT(*) n FROM resource_updates")["n"]), (0, 1))
        body = c.get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn("eCFR, several parts (alert only)", body)
        self.assertIn("14: 1, 91; 49: 830", body)
        self.assertIn("Dismiss", body)
        self.assertNotIn("Approve &amp; Publish", body)
        self.assertIn('href="https://www.ecfr.gov/"', body)

    def test_aim_alert_only_one_notice_for_both_parts(self):
        url = "https://www.faa.gov/air_traffic/publications/atpubs/aim_html/"
        self.exec("UPDATE acs_resources SET revision = 'Change 1', effectivity = 'February 1, 2025' WHERE id = ?", (self.rid,))
        self.exec("INSERT INTO acs_resources (title, revision, effectivity, filename, uploaded_at) VALUES ('AIM 2', 'Change 1', "
                  "'2025-02-01', 'a2.pdf', ?)", (db.now_iso(),))
        rid2 = self.q("SELECT id FROM acs_resources ORDER BY id")[1]["id"]
        for r in (self.rid, rid2):
            self.exec("INSERT INTO resource_sources (resource_id, source_url, source_kind) VALUES (?,?,?)",
                      (r, url, "faa_alert"))
        out, calls = self.scan({url: AIM_HTML})
        self.assertEqual(out["found"], 1)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM resource_updates")["n"], 1)
        u = self.q1("SELECT * FROM resource_updates")
        self.assertIsNone(u["new_filename"])
        self.assertIn("Change 2", u["summary"])
        self.assertIn("2025-04-03", u["summary"])
        msgs = [r["last_message"] for r in self.q("SELECT last_message FROM resource_sources ORDER BY id")]
        self.assertEqual(msgs, ["Update found", "Update found (already in the list)"])
        out, _ = self.scan({url: AIM_HTML})
        self.assertEqual((out["found"], self.q1("SELECT COUNT(*) n FROM resource_updates")["n"]), (0, 1))

    def test_aim_matching_the_row_is_up_to_date(self):
        url = "https://www.faa.gov/air_traffic/publications/atpubs/aim_html/"
        self.exec("UPDATE acs_resources SET revision = 'Change 2', effectivity = '2025-04-03' WHERE id = ?", (self.rid,))
        self.exec("INSERT INTO resource_sources (resource_id, source_url, source_kind) VALUES (?,?,?)",
                  (self.rid, url, "faa_alert"))
        out, _ = self.scan({url: AIM_HTML})
        self.assertEqual((out["found"], out["failed"], self.q1("SELECT COUNT(*) n FROM resource_updates")["n"]), (0, 0, 0))

    def test_aim_page_without_dates_fails_quietly_for_others(self):
        url = "https://www.faa.gov/air_traffic/publications/atpubs/aim_html/"
        self.exec("INSERT INTO resource_sources (resource_id, source_url, source_kind) VALUES (?,?,?)",
                  (self.rid, url, "faa_alert"))
        out, _ = self.scan({url: b"<html>nothing useful</html>"})
        self.assertEqual(out["failed"], 1)

    def test_parse_helpers(self):
        self.assertEqual(rs.parse_date("Oct 2, 2025"), "2025-10-02")
        self.assertEqual(rs.parse_date("October 2, 2025"), "2025-10-02")
        self.assertIsNone(rs.parse_date("Rev B"))
        self.assertEqual(rs.parse_aim_page(AIM_HTML.decode()), {"effective": "2025-04-03", "change": "Change 2"})


def make_pdf(lines):
    """A small real PDF (one paragraph per line) so the preview can read its text back."""
    import io
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=letter).build([Paragraph(x, getSampleStyleSheet()["BodyText"]) for x in lines])
    return buf.getvalue()


AIM_URL = "https://www.faa.gov/air_traffic/publications/atpubs/aim_html/"
AIM_PAGE = (b"<html><body><h1>AIM</h1><p>Change 2. Effective: April 3, 2025</p>"
            b'<a href="/docs/AIM_full.pdf">Download the AIM</a></body></html>')
AIM_OLD = [(1, "5-1-1 Preflight\nCheck the weather before every flight.\n5-1-2 Flight plans\nFile one.\n5-1-7 Alternates\nPlan an alternate."),]
AIM_NEW_LINES = ["5-1-1 Preflight", "Check the weather and NOTAMs before every flight.", "5-1-2 Flight plans", "File one.",
                 "5-1-8 Diversions", "Tell ATC at once."]
ECFR_XML = (b'<?xml version="1.0"?><ECFR><DIV5 TYPE="PART" N="91"><DIV8 TYPE="SECTION" N="91.103"><HEAD>&#xA7; 91.103 Preflight action</HEAD>'
            b'<P>Each pilot must become familiar with all available information and runway lengths.</P></DIV8>'
            b'<DIV8 TYPE="SECTION" N="91.105"><HEAD>&#xA7; 91.105 Flight crewmembers at stations</HEAD><P>Crew stay at stations.</P></DIV8>'
            b'<DIV8 TYPE="SECTION" N="91.109"><HEAD>&#xA7; 91.109 Flight instruction</HEAD><P>New rule text here.</P></DIV8></DIV5></ECFR>')


class PreviewUpdateTest(OpsHubTestCase):
    """Preview update (AIM and combined eCFR alerts): fetch, change view, Update, Cancel, permissions."""

    def setUp(self):
        super().setUp()
        self.q_dir = tempfile.mkdtemp()
        self.exec("INSERT INTO acs_resources (title, designation, filename, uploaded_at, doc_date, revision, effectivity) "
                  "VALUES ('Rules', '14 CFR Part 91', 'old.pdf', ?, '2025-01-02', 'Change 1', '2025-02-01')", (db.now_iso(),))
        self.rid = self.q1("SELECT id FROM acs_resources")["id"]
        self.pages = OLD
        for n, t in OLD:
            self.exec("INSERT INTO acs_resource_pages VALUES (?,?,?)", (self.rid, n, t))
        gs.SCAN_ASYNC = False
        gs.PREVIEW_ASYNC = False
        self.addCleanup(setattr, gs, "SCAN_ASYNC", True)
        self.addCleanup(setattr, gs, "PREVIEW_ASYNC", True)
        self.fetched = []
        self.answers = {}
        orig_q, orig_make = rs.quarantine_dir, rs.make_preview
        rs.quarantine_dir = lambda d=self.q_dir: d
        rs.make_preview = lambda conn, src, res, upd, **k: orig_make(conn, src, res, upd, fetch=self.fetch, qdir=self.q_dir,
                                                                      sleep=lambda s: None)
        self.addCleanup(setattr, rs, "quarantine_dir", orig_q)
        self.addCleanup(setattr, rs, "make_preview", orig_make)
        self.orig_extract = rs.extract_pages

    def fetch(self, url, max_bytes=None, method="GET"):
        rs.check_host(url)                       # the real allow-list applies to every call
        self.fetched.append(url)
        a = self.answers[url]
        if isinstance(a, Exception):
            raise a
        return 200, {}, a

    def alert(self, source, answers):
        self.answers.update(answers)
        self.exec("INSERT INTO resource_sources (resource_id, source_url, source_kind, last_seen_version) VALUES (?,?,?,?)",
                  (self.rid, source, rs.detect_kind(source), "x"))
        conn = db.get_db()
        rs.run_scan(conn, "t", fetch=self.fetch, sleep=lambda s: None, qdir=self.q_dir)
        conn.close()
        return self.q1("SELECT * FROM resource_updates")

    def aim_alert(self, pdf=None, old=None):
        self.exec("DELETE FROM acs_resource_pages")
        self.exec("INSERT INTO acs_resource_pages VALUES (?,1,?)", (self.rid, old or AIM_OLD[0][1]))
        return self.alert(AIM_URL, {AIM_URL: AIM_PAGE, "https://www.faa.gov/docs/AIM_full.pdf": pdf or make_pdf(AIM_NEW_LINES)})

    def multi_alert(self):
        v = rs.ecfr_api_url("https://www.ecfr.gov/current/title-14/part-91")
        return self.alert("14: 91", {v: ecfr_json("2026-09-12"),
                                     "https://www.ecfr.gov/api/versioner/v1/full/2026-09-12/title-14.xml?part=91": ECFR_XML})

    def test_diff_marks(self):
        b, a = rs.diff_html("pay the fee now", "pay the <b>fee</b> soon")
        self.assertEqual(str(b), "pay the <del>fee now</del>")
        self.assertEqual(str(a), "pay the <ins>&lt;b&gt;fee&lt;/b&gt; soon</ins>")
        ch = {c["key"]: c for c in rs.list_changes(OLD, NEW)}
        self.assertEqual({k: v["kind"] for k, v in ch.items()}, {"91.103": "changed", "91.107": "removed", "91.109": "added"})
        self.assertEqual(ch["91.103"]["group"], "Part 91")
        self.assertEqual(rs.list_changes([(1, "5-1-7 A\nx\n5-1-8 B\ny\n5-2-1 C\nz")], [(1, "5-1-7 A\nx\n5-1-8 B\ny\n5-2-1 C\nz")]), [])
        self.assertEqual(rs._chapter("5-1-7"), "Chapter 5")

    def test_aim_preview_update_and_previous_version(self):
        u = self.aim_alert()
        self.assertIsNone(u["new_filename"])
        c = self.login("master")
        body = c.get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn("Preview update", body)
        self.assertIn("Open source", body)
        resp = c.post(f"{GS}/resources/updates/{u['id']}/preview")
        self.assertIn(f"/updates/{u['id']}/changes", resp.headers["Location"])
        u = self.q1("SELECT * FROM resource_updates")
        self.assertEqual((u["preview_state"], u["new_revision"], u["new_effectivity"]), ("ready", "Change 2", "2025-04-03"))
        self.assertTrue(os.path.isfile(os.path.join(self.q_dir, u["new_filename"])))
        self.assertEqual(self.q1("SELECT filename FROM acs_resources")["filename"], "old.pdf")     # nothing live yet
        self.assertEqual(self.fetched[-2:], [AIM_URL, "https://www.faa.gov/docs/AIM_full.pdf"])
        view = c.get(f"{GS}/resources/updates/{u['id']}/changes").get_data(as_text=True)
        self.assertIn("Change 1", view)
        self.assertIn("Change 2", view)
        self.assertIn("1 section changed, 1 added, 1 removed", view)
        self.assertIn("<ins>and NOTAMs</ins>", view)
        self.assertIn("Chapter 5", view)
        self.assertIn("5-1-8", view)
        self.assertIn(">Update<", view)
        self.assertIn("Cancel", view)
        only = c.get(f"{GS}/resources/updates/{u['id']}/changes?show=added").get_data(as_text=True)
        self.assertIn("5-1-8", only)
        self.assertNotIn("data-change-item=\"removed\"", only)
        self.assertNotIn("NOTAMs", only)
        found = c.get(f"{GS}/resources/updates/{u['id']}/changes?q=alternate").get_data(as_text=True)
        self.assertIn("5-1-7", found)
        self.assertNotIn("5-1-8", found)
        resp = c.post(f"{GS}/resources/updates/{u['id']}/approve")
        self.assertIn(f"/resources/updates/{u['id']}/send", resp.headers["Location"])
        r = self.q1("SELECT * FROM acs_resources")
        self.assertEqual((r["prev_filename"], r["revision"], r["effectivity"]), ("old.pdf", "Change 2", "2025-04-03"))
        self.assertNotEqual(r["filename"], "old.pdf")
        self.assertTrue(r["new_until"])
        self.assertIn("NOTAMs", self.q1("SELECT text FROM acs_resource_pages")["text"])
        self.assertEqual(self.q1("SELECT status FROM resource_updates")["status"], "approved")
        body = c.get(f"{GS}/resources").get_data(as_text=True)
        self.assertNotIn("waiting for approval", body)
        self.assertIn(">NEW<", body)

    def test_cancel_keeps_old_and_alert(self):
        u = self.aim_alert()
        before = u["summary"]
        c = self.login("master")
        c.post(f"{GS}/resources/updates/{u['id']}/preview")
        c.post(f"{GS}/resources/updates/{u['id']}/cancel")
        u2 = self.q1("SELECT * FROM resource_updates")
        self.assertEqual((u2["status"], u2["new_filename"], u2["summary"]), ("pending", None, before))
        self.assertEqual(os.listdir(self.q_dir), [])
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM resource_update_pages")["n"], 0)
        self.assertEqual(self.q1("SELECT filename FROM acs_resources")["filename"], "old.pdf")
        self.assertIn("Preview update", c.get(f"{GS}/resources").get_data(as_text=True))

    def test_combined_cfr_preview_builds_pdf_from_listed_parts(self):
        u = self.multi_alert()
        c = self.login("master")
        c.post(f"{GS}/resources/updates/{u['id']}/preview")
        u = self.q1("SELECT * FROM resource_updates")
        self.assertEqual((u["preview_state"], u["new_doc_date"]), ("ready", "2026-09-12"))
        self.assertTrue(u["new_filename"].endswith(".pdf"))
        with open(os.path.join(self.q_dir, u["new_filename"]), "rb") as f:
            self.assertTrue(rs.validate_pdf(f.read())[0])
        self.assertTrue(all(x.startswith("https://www.ecfr.gov/") for x in self.fetched))
        view = c.get(f"{GS}/resources/updates/{u['id']}/changes").get_data(as_text=True)
        self.assertIn("eCFR amendment dates: 14 CFR 91: 2026-09-12", view)
        self.assertIn("1 section changed, 1 added, 1 removed", view)
        self.assertIn("<ins>information and runway lengths.</ins>", view)
        self.assertIn("Part 91", view)
        self.assertIn("91.109", view)
        c.post(f"{GS}/resources/updates/{u['id']}/approve")
        self.assertEqual(self.q1("SELECT doc_date FROM acs_resources")["doc_date"], "2026-09-12")
        self.assertEqual(self.q1("SELECT prev_filename FROM acs_resources")["prev_filename"], "old.pdf")

    def test_big_diff_shows_biggest_first_with_paging(self):
        u = self.aim_alert(old="\n".join(f"{i}.{i} Rule {i}\nold text {i}" for i in range(1, 90)),
                           pdf=make_pdf([f"{i}.{i} Rule {i}" if j == 0 else ("new words " * (i if i != 50 else 60))
                                     for i in range(1, 90) for j in (0, 1)]))
        c = self.login("master")
        c.post(f"{GS}/resources/updates/{u['id']}/preview")
        view = c.get(f"{GS}/resources/updates/{u['id']}/changes").get_data(as_text=True)
        self.assertIn("showing the biggest changes first", view)
        self.assertIn("Section 89.89", view)
        self.assertNotIn("Section 1.1<", view)
        self.assertIn("Page 1 of 5", view)
        self.assertIn("Page 2 of 5", c.get(f"{GS}/resources/updates/{u['id']}/changes?page=2").get_data(as_text=True))

    def test_non_admins_cannot_preview_or_update(self):
        u = self.aim_alert()
        for who in ("flight_student", "cfi"):
            c = self.login(who)
            for path, post in (("preview", True), ("cancel", True), ("approve", True), ("changes", False)):
                url = f"{GS}/resources/updates/{u['id']}/{path}"
                resp = c.post(url) if post else c.get(url)
                self.assertEqual(resp.status_code, 302, (who, path))
        self.assertIn(self.q1("SELECT preview_state FROM resource_updates")["preview_state"], (None, ""))
        self.assertEqual(self.fetched.count("https://www.faa.gov/docs/AIM_full.pdf"), 0)
        self.assertNotIn("Preview update", self.login("flight_student").get(f"{GS}/resources").get_data(as_text=True))

    def test_host_allow_list_and_unsafe_pdf_still_enforced(self):
        # a PDF link on a host that isn't allowed is never followed
        page = b'<html>Change 2. Effective: April 3, 2025 <a href="https://evil.example.com/a.pdf">x</a></html>'
        u = self.alert(AIM_URL, {AIM_URL: page})
        c = self.login("master")
        c.post(f"{GS}/resources/updates/{u['id']}/preview")
        u = self.q1("SELECT * FROM resource_updates")
        self.assertTrue(u["preview_state"].startswith("failed"), u["preview_state"])
        self.assertIsNone(u["new_filename"])
        self.assertNotIn("evil.example.com", "".join(self.fetched))
        self.assertIn("Preview update", c.get(f"{GS}/resources").get_data(as_text=True))   # can try again
        # a PDF with active content is refused and nothing stays in quarantine
        self.answers.update({AIM_URL: AIM_PAGE, "https://www.faa.gov/docs/AIM_full.pdf": b"%PDF-1.4 /JavaScript (x)"})
        c.post(f"{GS}/resources/updates/{u['id']}/preview")
        u = self.q1("SELECT * FROM resource_updates")
        self.assertIn("safety check", u["preview_state"])
        self.assertEqual(os.listdir(self.q_dir), [])
        # a blocked eCFR host: the real check_host refuses before any request
        with self.assertRaises(rs.BlockedHost):
            rs.default_fetch("https://evil.example.com/api/versioner/v1/full/x.xml")
