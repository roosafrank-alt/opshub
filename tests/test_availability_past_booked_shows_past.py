"""Idea "past views": on the Availability view (flight.schedule_availability),
a plain student viewer used to see a past, already-booked slot labeled
"Booked" - the same as a real upcoming booking - even though the flight had
already happened. It now shows "Past" (same grey, dashed treatment as a
past slot nobody ever booked), since a bygone flight isn't something a
student can act on either way. A CFI/admin viewer is unaffected and still
sees "Booked" for a past slot, since that historical detail is useful to
staff.
"""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db


class AvailabilityPastBookedShowsPastTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.day = date.today() - timedelta(days=3)
        conn = db.get_db()
        self.sched = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student,
                              cfi_id=self.cfi, scheduled_date=self.day.isoformat(), scheduled_time="09:00",
                              duration_hours=1.5, status="completed", created_by="Front Desk")
        conn.commit()
        conn.close()

    def test_student_sees_past_booked_slot_as_past_not_booked(self):
        # The 9:00 AM flight falls in the 8:00 AM - 9:30 AM availability
        # slot; a student should see it (and every other slot on this
        # wholly-past day) as plain "Past", never "Booked".
        html = self.login("flight_student").get(
            f"/flight/schedule/availability?view=day&date={self.day.isoformat()}&mode=calendar").get_data(as_text=True)
        self.assertIn('class="avail-slot-past" title="8:00 AM has already passed">Past</td>', html)
        self.assertNotIn('class="avail-slot-booked"', html)

    def test_cfi_still_sees_past_booked_slot_as_booked(self):
        html = self.login("cfi").get(
            f"/flight/schedule/availability?view=day&date={self.day.isoformat()}&mode=calendar").get_data(as_text=True)
        self.assertIn('class="avail-slot-booked"', html)
        self.assertIn("Flight Student", html)

    def test_student_sees_future_booked_slot_as_booked(self):
        future_day = date.today() + timedelta(days=3)
        conn = db.get_db()
        seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student,
                  cfi_id=self.cfi, scheduled_date=future_day.isoformat(), scheduled_time="09:00",
                  duration_hours=1.5, status="scheduled", created_by="Front Desk")
        conn.commit()
        conn.close()
        html = self.login("flight_student").get(
            f"/flight/schedule/availability?view=day&date={future_day.isoformat()}&mode=calendar").get_data(as_text=True)
        self.assertIn('class="avail-slot-booked"', html)
        self.assertIn("Booked", html)
