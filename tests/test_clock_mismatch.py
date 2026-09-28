"""Python writes timestamps on the LOCAL clock (db.now_iso() is
datetime.now()), SQLite's datetime('now') is UTC. Mix the two in one
comparison and the code is wrong by the machine's UTC offset - fine in a
UTC container, four hours out on the Pi (EDT).

Frank hit this on 2026-09-28: the suite passed everywhere but the Pi, where
test_viewas_alerts_stay_unread failed because it seeded push_pending with
db.now_iso() while the pickup route compares against datetime('now'), so a
just-queued alert looked four hours old and was dropped as stale. The app
itself was never affected there - it lets SQLite's DEFAULT fill created_at -
but adsb.py did have the real version of the bug, in its track-point prune
and its since_minutes window.

These tests run with TZ forced to a non-UTC zone, which is what makes the
mismatch visible at all."""
import os
import time
import unittest

from harness import OpsHubTestCase
import adsb
import db
import pi_health
import push


@unittest.skipUnless(hasattr(time, "tzset"), "needs tzset to force a timezone")
class NonUtcTimezoneTest(OpsHubTestCase):
    """Everything here runs as if on the Pi: America/New_York, UTC-4/-5."""

    def setUp(self):
        super().setUp()
        self._old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        self.addCleanup(self._restore_tz)

    def _restore_tz(self):
        if self._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old_tz
        time.tzset()

    def test_the_two_clocks_really_do_disagree_here(self):
        """Guard on the premise: if these ever agree, the tests below stop
        proving anything and this says so rather than passing quietly."""
        conn = db.get_db()
        sql_now = conn.execute("SELECT datetime('now') n").fetchone()["n"]
        conn.close()
        self.assertNotEqual(sql_now[:13], db.now_iso()[:13],
                            "expected SQLite UTC and Python local time to differ in this timezone")

    def test_a_queued_push_alert_is_not_dropped_as_stale(self):
        """The app's own queue path, end to end: queue_and_push then the
        service worker's pickup. The alert must come back, not be pruned by
        the route's 30-minute staleness cutoff."""
        uid = self.users["cfi"]["id"]
        conn = db.get_db()
        push.queue_and_push(conn, uid, "30 min left", "Session ends soon")
        conn.close()
        alerts = self.login("cfi").get("/flight/push/pending").get_json()["alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["title"], "30 min left")

    def test_the_prune_keeps_a_point_inside_the_age_limit(self):
        """The trail is meant to hold TRACK_MAX_AGE_HOURS of points, so one
        an hour short of that must survive the prune. A fresh point wouldn't
        prove anything: the UTC offset is smaller than the age limit, so the
        mismatch only cuts the trail short (8 hours instead of 12 on the
        Pi) rather than deleting brand new points."""
        keep_age = adsb.TRACK_MAX_AGE_HOURS - 1
        conn = db.get_db()
        conn.execute("INSERT INTO adsb_track_points (icao24, lat, lon, recorded_at) "
                     "VALUES ('a1b2c3', 41.9, -74.1, datetime('now', 'localtime', ?))",
                     ("-%d hours" % keep_age,))
        conn.commit()
        adsb._record_track_points(conn, [
            {"icao24": "a1b2c3", "lat": 41.8, "lon": -74.2, "is_school_plane": True},
        ])
        kept = conn.execute("SELECT COUNT(*) c FROM adsb_track_points").fetchone()["c"]
        conn.close()
        self.assertEqual(kept, 2, "a point only %d hours old was pruned, but the trail keeps %d hours"
                                  % (keep_age, adsb.TRACK_MAX_AGE_HOURS))

    def test_the_since_minutes_window_still_finds_recent_points(self):
        """get_track(since_minutes=...) must not filter out points recorded
        inside the window. Nothing calls it with since_minutes today, which
        is exactly why the mismatch could sit here unnoticed."""
        conn = db.get_db()
        adsb._record_track_points(conn, [
            {"icao24": "a1b2c3", "lat": 41.9, "lon": -74.1, "is_school_plane": True},
        ])
        points = adsb.get_track(conn, "a1b2c3", since_minutes=30)
        conn.close()
        self.assertEqual(len(points), 1, "a point from seconds ago fell outside a 30-minute window")

    def test_a_pi_health_alert_is_stamped_on_the_local_clock(self):
        """pi_health.main() raises the alert the dashboard banner shows, and
        the banner prints created_at with no timezone conversion. Stamped in
        UTC it read four hours into the future on the Pi."""
        self.addCleanup(setattr, pi_health, "read_temp_c", pi_health.read_temp_c)
        pi_health.read_temp_c = lambda: pi_health.ALERT_TEMP_C + 15
        pi_health.main()

        row = self.q1("SELECT * FROM system_alerts WHERE resolved_at IS NULL")
        self.assertIsNotNone(row, "no alert was raised")
        self.assertEqual(row["created_at"][:13], db.now_iso()[:13],
                         "alert time %s is not on the same clock as local now %s"
                         % (row["created_at"], db.now_iso()))

    def test_the_banner_shows_the_alert_at_the_local_time(self):
        """The symptom Frank would actually have seen: an alert raised now,
        shown on the dashboard as happening hours from now."""
        self.addCleanup(setattr, pi_health, "read_temp_c", pi_health.read_temp_c)
        pi_health.read_temp_c = lambda: pi_health.ALERT_TEMP_C + 15
        pi_health.main()

        html = self.login("shop_admin").get("/shop").get_data(as_text=True)
        self.assertIn("Pi Health Alert", html)
        # usdate(show_time=True) renders DD-MM-YYYY HH:MM, local clock.
        self.assertIn(db.now_iso()[11:16], html,
                      "banner doesn't show the alert at the current local time")

    def test_resolving_an_alert_uses_the_local_clock_too(self):
        """pi_health clears an alert once the Pi cools down, and app.py's
        Acknowledge button clears the same column with now_iso() - both must
        write the same clock or the two disagree by the UTC offset."""
        conn = db.get_db()
        conn.execute("INSERT INTO system_alerts (message, level, created_at) VALUES (?, 'warning', ?)",
                     ("OpsHub Pi: temp 90C", db.now_iso()))
        conn.commit()
        conn.close()
        # The faked vcgencmd reports nothing, so main() sees a cool Pi and
        # treats the open alert as recovered.
        pi_health.main()

        row = self.q1("SELECT resolved_at FROM system_alerts")
        self.assertIsNotNone(row["resolved_at"], "the alert wasn't resolved")
        self.assertEqual(row["resolved_at"][:13], db.now_iso()[:13],
                         "resolved_at %s is not on the same clock as local now %s"
                         % (row["resolved_at"], db.now_iso()))
