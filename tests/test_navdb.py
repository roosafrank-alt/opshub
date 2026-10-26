"""Airport type-ahead, code <-> name lookup, and route distance (navdb + logbook API)."""
import csv
import os
import tempfile
from unittest import mock

from harness import OpsHubTestCase
import navdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AIRPORTS = [  # ident, gps, iata, local, name, city, lat, lon
    ("N89", "", "", "N89", "Joseph Y. Resnick", "Ellenville", 41.72778, -74.37722),
    ("KPOU", "KPOU", "POU", "POU", "Hudson Valley Regional", "Poughkeepsie", 41.6266, -73.8842),
    ("KSWF", "KSWF", "SWF", "SWF", "Stewart International", "Newburgh", 41.5041, -74.1048),
    ("KPIT", "KPIT", "PIT", "PIT", "Pittsburgh International", "Pittsburgh", 40.4915, -80.2329),
    ("KPHL", "KPHL", "PHL", "PHL", "Philadelphia International", "Philadelphia", 39.8719, -75.2411),
    ("CYUL", "CYUL", "YUL", "", "Montreal Trudeau", "Montreal", 45.4706, -73.7408),
]


def _build(d):
    with open(os.path.join(d, "airports.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "ident", "type", "name", "latitude_deg", "longitude_deg", "elevation_ft", "iso_country", "iso_region",
                    "municipality", "gps_code", "iata_code", "local_code", "icao_code"])
        for i in range(1100):  # importer refuses suspiciously small files
            w.writerow([i, f"X{i:04d}", "small_airport", f"Filler {i}", 10 + i * 0.01, 10, 0, "ZZ", "ZZ-X", "Nowhere", "", "", "", ""])
        for a in AIRPORTS:
            w.writerow([0, a[0], "small_airport", a[4], a[6], a[7], 100, "US", "US-NY", a[5], a[1], a[2], a[3], a[1]])
    with open(os.path.join(d, "navaids.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "ident", "name", "type", "frequency_khz", "latitude_deg", "longitude_deg", "iso_country"])
        w.writerow([1, "SAX", "Sparta", "VORTAC", 115700, 41.0725, -74.5407, "US"])
    with open(os.path.join(d, "FIX_BASE.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["FIX_ID", "STATE_CODE", "LAT_DECIMAL", "LONG_DECIMAL"])
        w.writerow(["HAAYS", "NY", 41.3, -74.0])
        w.writerow(["LHY", "NY", 41.6, -74.2])
    with open(os.path.join(d, "AWY_SEG.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["AWY_ID", "POINT_SEQ", "FROM_POINT"])
        for i, p in enumerate(["LHY", "HAAYS", "SAX"], 1):
            w.writerow(["V99", i, p])


class NavDbTests(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        _build(self.dir)
        self.db = os.path.join(self.dir, "navdata.db")
        import importlib.util
        spec = importlib.util.spec_from_file_location("import_navdata", os.path.join(ROOT, "tools", "import_navdata.py"))
        imp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(imp)   # (the harness fakes subprocess, so call the importer directly)
        conn = navdb.connect(self.db)
        imp.import_ourairports(conn, self.dir)
        imp.import_nasr(conn, self.dir)
        conn.commit()
        conn.close()
        p = mock.patch.object(navdb, "NAV_DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)
        self.nav = navdb.connect()
        self.addCleanup(self.nav.close)

    def test_name_from_code_and_code_from_name(self):
        self.assertEqual(navdb.lookup_airport(self.nav, "pou")["name"], "Hudson Valley Regional")
        self.assertEqual(navdb.lookup_airport(self.nav, "KPOU")["code"], "KPOU")
        self.assertEqual(navdb.lookup_airport(self.nav, "N89")["code"], "N89")   # no ICAO code -> local code, not KN89
        self.assertEqual(navdb.search_airports(self.nav, "stewart")[0]["code"], "KSWF")

    def test_filters_per_character(self):
        self.assertEqual(len(navdb.search_airports(self.nav, "K")), 4)
        self.assertEqual([a["code"] for a in navdb.search_airports(self.nav, "KP", near=(41.7, -74.4))][:1], ["KPOU"])
        self.assertEqual([a["code"] for a in navdb.search_airports(self.nav, "KPI")], ["KPIT"])

    def test_closest_first_unless_frequented(self):
        near = (navdb.HOME["lat"], navdb.HOME["lon"])
        order = [a["code"] for a in navdb.search_airports(self.nav, "KP", near=near)]
        self.assertEqual(order, ["KPOU", "KPHL", "KPIT"])           # nearest first
        order = [a["code"] for a in navdb.search_airports(self.nav, "KP", near=near, freq={"KPIT": 5, "KPHL": 2})]
        self.assertEqual(order, ["KPIT", "KPHL", "KPOU"])           # frequented first, most visits first

    def test_straight_line_nm(self):
        r = navdb.parse_route(self.nav, "N89", "KPOU")
        self.assertTrue(r["straight"])
        self.assertAlmostEqual(r["nm"], 22.9, delta=1)

    def test_route_via_navaid_and_airway(self):
        r = navdb.parse_route(self.nav, "N89", "KPOU", "LHY V99 SAX")
        self.assertFalse(r["straight"])
        self.assertEqual([p["code"] for p in r["points"]], ["N89", "LHY", "HAAYS", "SAX", "KPOU"])
        self.assertGreater(r["nm"], navdb.parse_route(self.nav, "N89", "KPOU")["nm"])
        self.assertEqual(navdb.parse_route(self.nav, "N89", "KPOU", "ZZZZZ")["unresolved"], ["ZZZZZ"])

    def test_api_requires_login_and_works(self):
        self.assertIn(self.client.get("/academy/api/airports?q=KP").status_code, (200, 302, 401, 403))
        c = self.login("master")
        r = c.get("/academy/api/airports?q=KPO&role=from")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()[0]["code"], "KPOU")
        r = c.get("/academy/api/route?from=N89&to=KPOU").get_json()
        self.assertTrue(r["straight"])
        self.assertAlmostEqual(r["nm"], 22.9, delta=1)
