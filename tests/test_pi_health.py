"""pi_health.py: the cron job that watches Pi temperature/throttling.
Alerts once per crossing (no repeats while hot), recovers below 70C, reads
vcgencmd output safely. No real vcgencmd, ntfy or network is used."""
import os
import subprocess
import tempfile
import unittest

import harness
from harness import OpsHubTestCase
import db
import pi_health


class PiHealthTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self._saved = (pi_health.read_temp_c, pi_health.read_throttled, pi_health.notify)
        self.sent = []
        pi_health.notify = lambda m: self.sent.append(m)

    def tearDown(self):
        pi_health.read_temp_c, pi_health.read_throttled, pi_health.notify = self._saved
        super().tearDown()

    def _run(self, temp, throttled=0):
        pi_health.read_temp_c = lambda: temp
        pi_health.read_throttled = lambda: throttled
        pi_health.main()

    def _alerts(self):
        conn = db.get_db()
        try:
            return conn.execute("SELECT message, level, resolved_at FROM system_alerts ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_cool_pi_raises_nothing(self):
        self._run(48.0)
        self.assertEqual(self._alerts(), [])
        self.assertEqual(self.sent, [])

    def test_exactly_at_threshold_is_not_an_alert(self):
        self._run(75.0)
        self.assertEqual(self._alerts(), [])

    def test_hot_pi_alerts_once_not_every_run(self):
        self._run(80.0)
        self._run(81.0)
        self._run(90.0)
        rows = self._alerts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(rows[0]["level"], "warning")
        self.assertIn("80", rows[0]["message"])

    def test_throttling_is_critical(self):
        self._run(50.0, throttled=0x4)
        rows = self._alerts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "critical")
        self.assertIn("throttling", rows[0]["message"])

    def test_soft_temp_limit_alerts(self):
        self._run(60.0, throttled=0x8)
        self.assertEqual(len(self._alerts()), 1)

    def test_past_throttle_bits_alone_do_not_alert(self):
        # bits 16-19 mean "has happened since boot", not "happening now"
        self._run(50.0, throttled=0x50000)
        self.assertEqual(self._alerts(), [])

    def test_stays_open_between_70_and_75_then_recovers_below_70(self):
        self._run(80.0)
        self._run(72.0)
        self.assertIsNone(self._alerts()[0]["resolved_at"])
        self._run(65.0)
        self.assertIsNotNone(self._alerts()[0]["resolved_at"])

    def test_new_crossing_after_recovery_alerts_again(self):
        self._run(80.0)
        self._run(60.0)
        self._run(82.0)
        self.assertEqual(len(self._alerts()), 2)
        self.assertEqual(len(self.sent), 2)

    def test_still_throttled_blocks_recovery(self):
        self._run(80.0)
        self._run(55.0, throttled=0x4)
        self.assertIsNone(self._alerts()[0]["resolved_at"])

    def test_no_reading_off_the_pi_does_nothing(self):
        self._run(None, 0)
        self.assertEqual(self._alerts(), [])

    def test_alert_time_uses_local_clock_helper(self):
        self._run(90.0)
        conn = db.get_db()
        try:
            created = conn.execute("SELECT created_at FROM system_alerts").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(created[:4], db.now_iso()[:4])


class VcgencmdParsingTest(unittest.TestCase):
    def _with(self, out):
        saved = pi_health.subprocess.run
        pi_health.subprocess.run = lambda args, *a, **k: subprocess.CompletedProcess(args, 0, out, "")
        try:
            return pi_health.read_temp_c(), pi_health.read_throttled()
        finally:
            pi_health.subprocess.run = saved

    def test_real_output(self):
        self.assertEqual(self._with("temp=51.2'C\n")[0], 51.2)
        self.assertEqual(self._with("throttled=0x50005\n")[1], 0x50005)

    def test_garbage_and_empty(self):
        self.assertEqual(self._with(""), (None, 0))
        self.assertEqual(self._with("VCHI initialization failed"), (None, 0))

    def test_command_missing(self):
        saved = pi_health.subprocess.run
        def boom(*a, **k):
            raise FileNotFoundError("vcgencmd")
        pi_health.subprocess.run = boom
        try:
            self.assertEqual((pi_health.read_temp_c(), pi_health.read_throttled()), (None, 0))
        finally:
            pi_health.subprocess.run = saved


class NotifyTest(unittest.TestCase):
    def test_no_topic_file_is_quiet(self):
        saved = pi_health.NTFY_URL_FILE
        pi_health.NTFY_URL_FILE = os.path.join(tempfile.gettempdir(), "no-such-ntfy-file")
        try:
            pi_health.notify("x")  # must not raise or hit the network
        finally:
            pi_health.NTFY_URL_FILE = saved


if __name__ == "__main__":
    unittest.main()
