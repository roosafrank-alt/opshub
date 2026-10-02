"""Idea "view change": when viewing as a CFI, clicking View as Student (or
any other preview) was refused - you had to Exit back to the real admin
view first before starting a different one. start_view_as() now lets a
master admin switch straight from one preview to another, same program or
not, without exiting first. The very first preview started still snapshots
the real admin session (auth._VIEW_AS_SNAPSHOT_KEYS) so Exit always
restores it correctly however many times you've switched since."""
from harness import OpsHubTestCase


class ViewAsSwitchWithoutExitTest(OpsHubTestCase):
    def test_switching_from_cfi_to_student_without_exiting_works(self):
        c = self.login("master")
        c.post("/view-as/flight/cfi")
        with c.session_transaction() as s:
            self.assertEqual(s["flight_role"], "cfi")
            first_cfi_id = s.get("cfi_id")
            self.assertIsNotNone(first_cfi_id)
        r = c.post("/view-as/flight/student")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("Can&#39;t switch to that role", c.get(r.headers["Location"]).get_data(as_text=True))
        with c.session_transaction() as s:
            self.assertEqual(s["flight_role"], "student")
            self.assertIsNone(s.get("cfi_id"))
            self.assertIsNotNone(s.get("student_id"))

    def test_switching_from_a_flight_preview_to_a_shop_preview_works(self):
        c = self.login("master")
        with c.session_transaction() as s:
            real_cfi_id = s.get("cfi_id")  # master's own real CFI profile
        c.post("/view-as/flight/cfi")
        with c.session_transaction() as s:
            # The CFI preview picked master's own profile too, so this
            # alone wouldn't distinguish "still real" from "still preview".
            self.assertEqual(s.get("cfi_id"), real_cfi_id)
        c.post("/view-as/flight/student")
        with c.session_transaction() as s:
            self.assertIsNone(s.get("cfi_id"))  # now previewing Student, not CFI
        r = c.post("/view-as/shop/tech")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertEqual(s["shop_role"], "tech")
            # Switching to Shop falls back to the real admin's flight_role
            # and cfi_id, not whatever the Student preview left behind.
            self.assertEqual(s["flight_role"], "cfi")
            self.assertEqual(s.get("cfi_id"), real_cfi_id)

    def test_exiting_after_several_switches_restores_the_real_admin_session(self):
        c = self.login("master")
        c.post("/view-as/shop/tech")
        c.post("/view-as/flight/cfi")
        c.post("/view-as/shop/apprentice")
        c.post("/view-as/exit")
        with c.session_transaction() as s:
            self.assertTrue(s["is_master_admin"])
            self.assertEqual(s["shop_role"], "admin")
            self.assertEqual(s["flight_role"], "cfi")

    def test_view_as_chips_show_the_current_program_after_switching(self):
        c = self.login("master")
        c.post("/view-as/flight/cfi")
        c.post("/view-as/shop/tech")
        body = c.get("/shop").get_data(as_text=True)
        # The Admin chip is the way back now (idea "view as for
        # multi-role accounts") - it posts to the exit route.
        self.assertIn('action="/view-as/exit"', body)
        self.assertIn('title="Back to your normal view">Admin</button>', body)

    def test_a_non_master_admin_still_cannot_start_a_preview(self):
        c = self.login("shop_admin")
        r = c.post("/view-as/shop/tech", follow_redirects=True)
        self.assertIn("Can&#39;t switch to that role", r.get_data(as_text=True))
