"""Idea: an admin who is also a CFI can switch to their OWN CFI profile from
the CFI picker ("Use it as a CFI myself"): not look-only, admin tools dropped,
and never falls back to somebody else's CFI profile."""
from harness import OpsHubTestCase


class ViewAsCfiMeTest(OpsHubTestCase):
    def test_picker_offers_it_and_it_switches_to_own_profile(self):
        c = self.login("flight_admin_cfi")
        uid = self.users["flight_admin_cfi"]["id"]
        self.assertIn("Use it as a CFI myself", c.get("/view-as/flight/cfi").get_data(as_text=True))
        r = c.post("/view-as/flight/cfi/me")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            self.assertEqual(s.get("flight_role"), "cfi")
            self.assertFalse(s.get("is_master_admin"))
            self.assertFalse(s.get("_view_as_readonly"))
            own = self.q1("SELECT id FROM cfis WHERE user_id = ?", (uid,))["id"]
            self.assertEqual(s.get("cfi_id"), own)

    def test_no_own_cfi_profile_never_uses_someone_elses(self):
        c = self.login("flight_admin")
        self.exec("UPDATE cfis SET active = 0 WHERE user_id = ?", (self.users["flight_admin"]["id"],))
        c.post("/view-as/flight/cfi/me")
        with c.session_transaction() as s:
            self.assertNotEqual(s.get("flight_role"), "cfi")


class PickerHidesOwnNameTest(OpsHubTestCase):
    def test_own_name_not_in_either_list(self):
        c = self.login("flight_admin_cfi")
        uid = self.users["flight_admin_cfi"]["id"]
        mine = self.q1("SELECT name FROM cfis WHERE user_id = ?", (uid,))
        others = self.q1("SELECT name FROM cfis WHERE user_id IS NOT ? AND active = 1 AND COALESCE(is_station,0)=0", (uid,))
        body = c.get("/view-as/flight/cfi").get_data(as_text=True)
        self.assertNotIn(f'data-name="{mine["name"].lower()}"', body)
        if others:
            self.assertIn(f'data-name="{others["name"].lower()}"', body)
