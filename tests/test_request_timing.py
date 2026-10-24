"""Slow-page timing log: records only slow requests, no private details, and
shows a summary on Admin > System."""
import os
from datetime import datetime, timedelta
from harness import OpsHubTestCase
import request_timing as rt


class RequestTimingTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        if os.path.exists(rt.LOG_PATH):
            os.remove(rt.LOG_PATH)

    def lines(self):
        if not os.path.exists(rt.LOG_PATH):
            return []
        with open(rt.LOG_PATH) as f:
            return f.read().splitlines()

    def test_fast_requests_are_not_recorded(self):
        self.assertFalse(rt.record("/parts", "GET", 200, rt.SLOW_MS - 1))
        self.assertEqual(self.lines(), [])

    def test_slow_request_is_one_tab_separated_line(self):
        self.assertTrue(rt.record("/flight/dashboard", "GET", 200, 2500.4))
        (line,) = self.lines()
        when, ms, status, method, page = line.split("\t")
        self.assertEqual((ms, status, method, page), ("2500", "200", "GET", "/flight/dashboard"))
        datetime.strptime(when, "%Y-%m-%d %H:%M:%S")

    def test_real_request_logs_route_pattern_without_ids_or_query(self):
        orig = rt.SLOW_MS
        rt.SLOW_MS = 0
        try:
            c = self.login("master")
            part = self.make_part()
            c.get(f"/parts/{part}?secret=abc123")
        finally:
            rt.SLOW_MS = orig
        with open(rt.LOG_PATH) as f:
            text = f.read()
        self.assertIn("/parts/<int:part_id>", text)
        self.assertNotIn("abc123", text)
        self.assertNotIn(f"/parts/{part}\t", text)

    def test_static_files_are_not_recorded(self):
        orig = rt.SLOW_MS
        rt.SLOW_MS = 0
        try:
            self.login(None).get("/static/css/style.css")
        finally:
            rt.SLOW_MS = orig
        self.assertNotIn("static", "".join(self.lines()))

    def test_log_trims_itself(self):
        orig = (rt.MAX_BYTES, rt.KEEP_LINES)
        rt.MAX_BYTES, rt.KEEP_LINES = 200, 5
        try:
            for i in range(60):
                rt.record(f"/page{i}", "GET", 200, 1000)
        finally:
            rt.MAX_BYTES, rt.KEEP_LINES = orig
        self.assertLess(len(self.lines()), 60)
        self.assertIn("/page59", "".join(self.lines()))

    def test_summary_groups_pages_worst_first_and_ignores_old(self):
        now = datetime.now()
        rt.record("/a", "GET", 200, 1000, when=now - timedelta(hours=1))
        rt.record("/a", "GET", 200, 3000, when=now - timedelta(hours=2))
        rt.record("/b", "POST", 200, 1500, when=now - timedelta(hours=3))
        rt.record("/old", "GET", 200, 9000, when=now - timedelta(days=30))
        s = rt.summary()
        self.assertEqual(s["total"], 3)
        self.assertEqual([p["page"] for p in s["pages"]], ["/a", "/b"])
        self.assertEqual((s["pages"][0]["count"], s["pages"][0]["avg"], s["pages"][0]["worst"]), (2, 2000, 3000))

    def test_system_page_shows_summary(self):
        c = self.login("master")
        self.assertIn("No slow pages recorded", c.get("/admin/system").get_data(as_text=True))
        rt.record("/flight/dashboard", "GET", 200, 4200)
        body = c.get("/admin/system").get_data(as_text=True)
        self.assertIn("Slow Pages", body)
        self.assertIn("GET /flight/dashboard", body)
        self.assertIn("4.2s", body)
