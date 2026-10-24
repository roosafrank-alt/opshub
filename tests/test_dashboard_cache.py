"""The Flight School dashboard must never wait on outside services: it reads
the saved weather/NOTAM copy, and a background job keeps that copy fresh."""
import json
from harness import OpsHubTestCase, SIDE_EFFECTS
import db
import dashboard_cache as dc
import flight
import notams
import weather


class DashboardCacheTest(OpsHubTestCase):
    def put(self, key, payload, age_seconds):
        from datetime import datetime, timedelta
        when = (datetime.now() - timedelta(seconds=age_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        self.exec("INSERT OR REPLACE INTO weather_cache (cache_key, payload, fetched_at) VALUES (?,?,?)",
                  (key, json.dumps(payload), when))

    def setUp(self):
        super().setUp()
        self.started = []
        self._orig = dc._start_refresh
        dc._start_refresh = lambda do_weather=True, do_notams=True: self.started.append((do_weather, do_notams))

    def tearDown(self):
        dc._start_refresh = self._orig
        super().tearDown()

    def conn(self):
        return db.get_db()

    def test_fresh_copy_is_used_and_nothing_refreshes(self):
        self.put(dc._weather_key(), {"zip": "x", "conditions": {"t": 1}}, 60)
        c = self.conn()
        self.assertEqual(dc.cached_weather(c)["conditions"], {"t": 1})
        c.close()
        self.assertEqual(self.started, [])

    def test_slightly_old_copy_is_shown_and_refresh_starts(self):
        self.put(dc._weather_key(), {"conditions": {"t": 2}}, 20 * 60)
        c = self.conn()
        self.assertEqual(dc.cached_weather(c)["conditions"], {"t": 2})
        c.close()
        self.assertEqual(self.started, [(True, False)])

    def test_copy_over_two_hours_old_is_hidden(self):
        self.put(dc._weather_key(), {"conditions": {"t": 3}}, 3 * 3600)
        c = self.conn()
        self.assertIsNone(dc.cached_weather(c))
        c.close()
        self.assertEqual(self.started, [(True, False)])

    def test_missing_copy_returns_none_and_starts_refresh(self):
        c = self.conn()
        self.assertIsNone(dc.cached_weather(c))
        self.assertIsNone(dc.cached_notams(c))
        c.close()
        self.assertEqual(self.started, [(True, False), (False, True)])

    def test_dashboard_makes_no_outside_calls(self):
        before = len(SIDE_EFFECTS)
        r = self.login("cfi").get("/flight/dashboard")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([e for e in SIDE_EFFECTS[before:] if e[0] == "urlopen"], [],
                         "opening the dashboard must not call weather/NOTAM services")

    def test_refresh_if_stale_only_asks_for_what_is_old(self):
        self.put(dc._weather_key(), {"conditions": {}}, 60)           # fresh
        self.put(notams.CACHE_KEY, {"notams": []}, 40 * 60)             # old
        dc.refresh_if_stale()
        self.assertEqual(self.started, [(False, True)])

    def test_start_refresh_respects_background_flag_and_gap(self):
        dc._start_refresh = self._orig
        self.assertFalse(dc._start_refresh())      # BACKGROUND is off in tests


class WeatherFailureTest(OpsHubTestCase):
    """A weather timeout must not blank the saved copy or be saved itself."""

    def setUp(self):
        super().setUp()
        self._saved = (weather._get_json, weather.time.sleep)
        weather.time.sleep = lambda s: None
        self.calls = []

    def tearDown(self):
        weather._get_json, weather.time.sleep = self._saved
        super().tearDown()

    def down(self, url, timeout=0):
        self.calls.append(url)
        raise OSError("timed out")

    def test_zip_lookup_makes_no_call(self):
        weather._get_json = self.down
        self.assertEqual(weather._zip_to_latlon("12458"), (weather.FALLBACK_LAT, weather.FALLBACK_LON))
        self.assertEqual(self.calls, [])

    def test_open_meteo_retried_once(self):
        def flaky(url, timeout=0):
            self.calls.append(url)
            if len(self.calls) == 1:
                raise OSError("timed out")
            return {"current": {"temperature_2m": 50, "weather_code": 0}}
        weather._get_json = flaky
        self.assertEqual(weather._fetch_conditions(1, 2)["temp_f"], 50)
        self.assertEqual(len(self.calls), 2)

    def test_failed_fetch_keeps_last_good_dashboard_copy(self):
        good = {"zip": "12458", "conditions": {"temp_f": 61}, "error": None}
        self.exec("INSERT OR REPLACE INTO weather_cache (cache_key, payload, fetched_at) VALUES (?,?,?)",
                  (f"dashboard_wx_{weather.ZIP_CODE}", json.dumps(good), "2020-01-01 00:00:00"))
        weather._get_json = self.down
        c = db.get_db()
        out = weather.get_dashboard_weather(c, force=True)
        row = c.execute("SELECT payload, fetched_at FROM weather_cache WHERE cache_key = ?",
                        (f"dashboard_wx_{weather.ZIP_CODE}",)).fetchone()
        c.close()
        self.assertEqual(out["conditions"], {"temp_f": 61})
        self.assertEqual(json.loads(row["payload"])["conditions"], {"temp_f": 61})
        self.assertEqual(row["fetched_at"], "2020-01-01 00:00:00")

    def test_failed_lesson_forecast_is_not_saved(self):
        weather._get_json = self.down
        c = db.get_db()
        out = weather.get_forecast_for(c, "2030-01-01", "10:00")
        n = c.execute("SELECT COUNT(*) AS n FROM weather_cache WHERE cache_key LIKE 'lesson_wx_%'").fetchone()["n"]
        c.close()
        self.assertIn("Couldn't reach", out["error"])
        self.assertEqual(n, 0)
