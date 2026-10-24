"""Idea "under edit aircraft in Fly With Kate!": a Solo Flights Allowed
checkbox on a plane's edit page (Planes > Edit > Solo Color page). Unchecked,
that plane can't be booked solo - checked (the default) it works exactly as
before.
"""
from datetime import date, timedelta

from harness import OpsHubTestCase


class SoloAllowedTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N999XX")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.day = (date.today() + timedelta(days=3)).isoformat()

    def book_solo(self, role="cfi"):
        return self.login(role).post("/flight/schedule/new", data=dict(
            asset_id=str(self.plane), student_id=str(self.student), solo="on",
            scheduled_date=self.day, scheduled_time="09:00", duration_hours="1.5"))

    def test_defaults_to_allowed(self):
        self.assertEqual(self.q1("SELECT solo_allowed FROM assets WHERE id=?", (self.plane,))["solo_allowed"], 1)

    def test_solo_booking_works_when_allowed(self):
        before = len(self.q("SELECT id FROM scheduled_flights"))
        r = self.book_solo()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.q("SELECT id FROM scheduled_flights")), before + 1)

    def test_solo_booking_refused_when_not_allowed(self):
        self.exec("UPDATE assets SET solo_allowed = 0 WHERE id = ?", (self.plane,))
        before = len(self.q("SELECT id FROM scheduled_flights"))
        r = self.book_solo()
        self.assertIn("isn&#39;t approved for solo flights", r.get_data(as_text=True))
        self.assertEqual(len(self.q("SELECT id FROM scheduled_flights")), before)

    def test_dual_booking_still_works_on_a_no_solo_plane(self):
        self.exec("UPDATE assets SET solo_allowed = 0 WHERE id = ?", (self.plane,))
        cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        r = self.login("cfi").post("/flight/schedule/new", data=dict(
            asset_id=str(self.plane), student_id=str(self.student), cfi_id=str(cfi),
            scheduled_date=self.day, scheduled_time="09:00", duration_hours="1.5"))
        self.assertEqual(r.status_code, 302)

    def test_editing_an_existing_booking_solo_onto_a_no_solo_plane_is_refused(self):
        r = self.book_solo()
        sid = self.q("SELECT id FROM scheduled_flights ORDER BY id")[-1]["id"]
        other = self.make_asset("N888YY")
        self.exec("UPDATE assets SET is_flight_asset = 1, solo_allowed = 0 WHERE id = ?", (other,))
        r = self.login("cfi").post(f"/flight/schedule/{sid}/edit", data=dict(
            asset_id=str(other), student_id=str(self.student), solo="on",
            scheduled_date=self.day, scheduled_time="09:00", duration_hours="1.5"))
        self.assertIn("isn&#39;t approved for solo flights", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT asset_id FROM scheduled_flights WHERE id=?", (sid,))["asset_id"], self.plane)

    def test_edit_page_has_the_checkbox(self):
        html = self.login("master").get(f"/flight/planes/{self.plane}/rate").get_data(as_text=True)
        self.assertIn("Solo flights allowed", html)
        self.assertIn('id="solo-allowed-check" checked', html)

    def test_unchecking_the_box_saves_it(self):
        c = self.login("master")
        c.post(f"/flight/planes/{self.plane}/rate", data=dict(color="", solo_color=""))
        self.assertEqual(self.q1("SELECT solo_allowed FROM assets WHERE id=?", (self.plane,))["solo_allowed"], 0)
