"""Logbook > Stats: airports, miles and hours for all time, 1/6/12 months or a custom range."""
from datetime import date, timedelta
from unittest import mock

from harness import OpsHubTestCase
import pilotlog


class AcademyStatsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE users SET academy_access = 1 WHERE username = 'flight_student'")
        today = date.today()
        for days, a, b in ((5, "N89", "KPOU"), (100, "KPOU", "KBOS"), (400, "N89", "KALB")):
            self.exec("""INSERT INTO pilot_logbook (student_id, entry_date, route_from, route_to, total, day_ldg, status)
                         VALUES (?, ?, ?, ?, 1.5, 2, 'complete')""", (self.sid, (today - timedelta(days=days)).isoformat(), a, b))
        self.coords = {"N89": {"lat": 41.73, "lon": -74.38, "name": ""}, "KPOU": {"lat": 41.63, "lon": -73.88, "name": ""},
                       "KBOS": {"lat": 42.36, "lon": -71.01, "name": ""}, "KALB": {"lat": 42.75, "lon": -73.80, "name": ""}}

    def get(self, **q):
        with mock.patch.object(pilotlog, "_lookup_airports", lambda conn, codes: {c: self.coords[c] for c in codes if c in self.coords}):
            r = self.login("flight_student").get("/academy/stats", query_string=q)
        return r

    def test_ranges(self):
        for rng, flights in (("all", 3), ("1", 1), ("6", 2), ("12", 2)):
            html = self.get(range=rng).get_data(as_text=True)
            self.assertIn("Airports visited", html)
            self.assertIn('<div class="fs-4 fw-bold">%d</div><div class="small text-muted">Flights' % flights, html.replace("\n", "").replace("  ", ""), rng)

    def test_custom_range_and_distance(self):
        d = date.today() - timedelta(days=120)
        html = self.get(range="custom", **{"from": d.isoformat(), "to": date.today().isoformat()}).get_data(as_text=True)
        self.assertIn("Custom range", html)
        self.assertIn("Nautical miles", html)

    def test_stats_is_first_subtab(self):
        html = self.get().get_data(as_text=True)
        self.assertLess(html.index("bi-graph-up\"></i> Stats"), html.index("bi-journal-text\"></i> Logbook"))


class LogbookRangeFilterTest(AcademyStatsTest):
    def test_logbook_filters_by_range(self):
        c = self.login("flight_student")
        all_html = c.get("/academy/logbook").get_data(as_text=True)
        one = c.get("/academy/logbook", query_string={"range": "1"}).get_data(as_text=True)
        self.assertIn("KALB", all_html)
        self.assertNotIn("KALB", one)
        self.assertIn("KPOU", one)
        self.assertIn("this range only", one)
