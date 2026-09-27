"""Idea "un confirmed drop down in fly with kate!": the Unconfirmed badge on
the Dashboard should carry each unconfirmed flight's id so the page's own
JS (dashCheckNewUnconfirmed/dashMarkUnconfirmedSeen in dashboard.html) can
color it yellow when one hasn't been seen on this browser yet, grey once
the section's been opened. This only tests the server-rendered contract
the JS depends on (the badge id/data attribute) - the seen/yellow-vs-grey
logic itself is client-side (localStorage), not something a server test
can drive.
"""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class UnconfirmedBadgeTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.day = (date.today() + timedelta(days=3)).isoformat()

    def make_unconfirmed(self):
        conn = db.get_db()
        sid = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student, cfi_id=self.cfi,
                       scheduled_date=self.day, scheduled_time="09:00", duration_hours="1.5",
                       status="scheduled", confirm_required=1, confirmed_at=None, created_by="CFI")
        conn.commit()
        conn.close()
        return sid

    def test_badge_carries_the_unconfirmed_flight_id(self):
        sid = self.make_unconfirmed()
        html = self.login("master").get("/flight/dashboard").get_data(as_text=True)
        self.assertIn(f'id="unconfirmed-count-badge"', html)
        self.assertIn(f'data-unconfirmed-ids="{sid}"', html)

    def test_no_unconfirmed_card_does_not_render_for_a_plain_cfi(self):
        self.make_unconfirmed()
        html = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        self.assertNotIn('id="unconfirmed-count-badge"', html)

    def test_confirmed_flight_is_not_in_the_ids(self):
        sid = self.make_unconfirmed()
        self.login("flight_student").post(f"/flight/schedule/{sid}/confirm")
        html = self.login("master").get("/flight/dashboard").get_data(as_text=True)
        self.assertNotIn('id="unconfirmed-count-badge"', html)
