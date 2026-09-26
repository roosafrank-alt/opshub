"""Master admin System page: the Pi temperature reading (queued "Pi temp
monitoring" idea). Skips on code that doesn't have that feature yet."""
import subprocess
import unittest

import harness
from harness import OpsHubTestCase
import app as app_module


@unittest.skipUnless(hasattr(app_module, "_read_pi_temp_c"), "Pi temperature reading not on this branch yet")
class PiTempTest(OpsHubTestCase):
    def _with_output(self, out, raises=None):
        def fake(args, *a, **k):
            harness.SIDE_EFFECTS.append(("subprocess", args))
            if raises:
                raise raises
            return subprocess.CompletedProcess(args, 0, out if k.get("text") else out.encode(), "")
        saved = app_module.subprocess.run
        app_module.subprocess.run = fake
        try:
            return app_module._read_pi_temp_c()
        finally:
            app_module.subprocess.run = saved

    def test_reads_a_real_vcgencmd_line(self):
        self.assertEqual(self._with_output("temp=48.3'C\n"), 48.3)

    def test_off_pi_or_garbage_gives_no_reading(self):
        self.assertIsNone(self._with_output(""))
        self.assertIsNone(self._with_output("VCHI initialization failed\n"))
        self.assertIsNone(self._with_output("", raises=FileNotFoundError("vcgencmd")))

    def test_system_page_only_for_master_admin_and_renders_off_pi(self):
        self.assertEqual(self.login("master").get("/admin/system").status_code, 200)
        for role in ("shop_admin", "tech", "cfi"):
            with self.subTest(role=role):
                self.assertNotEqual(self.login(role).get("/admin/system").status_code, 200)


if __name__ == "__main__":
    unittest.main()
