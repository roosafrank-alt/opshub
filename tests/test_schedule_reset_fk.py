"""Frank reported Reset Schedule crashed with "Reset didn't run - nothing was
deleted. (FOREIGN KEY constraint failed)". Cause: when a booking is cancelled
the waitlist makes an offer for the freed slot, and waitlist_offers.
cancelled_flight_id is a hard foreign key to scheduled_flights. _wipe_schedule
un-linked flights, notification_log and flight_alerts but never the waitlist
offer, so deleting that booking violated the constraint and rolled the whole
reset back."""
from datetime import date

from harness import OpsHubTestCase
import db


class ScheduleResetWithWaitlistOfferTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.asset = self.make_asset("N556PL")
        today = date.today().isoformat()
        self.booking = self.exec(
            """INSERT INTO scheduled_flights (student_id, cfi_id, asset_id, scheduled_date, scheduled_time,
                                              status, solo, created_at)
               VALUES (?,?,?,?,?,'cancelled',0,?)""",
            (self.student, self.cfi, self.asset, today, "09:00", db.now_iso()))
        self.offer = self.exec(
            """INSERT INTO waitlist_offers (student_id, cancelled_flight_id, asset_id, cfi_id,
                                            scheduled_date, scheduled_time, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (self.student, self.booking, self.asset, self.cfi, today, "09:00", db.now_iso()))

    def test_reset_schedule_does_not_crash_with_a_waitlist_offer(self):
        c = self.login("master")
        html = c.post("/admin/reset/schedule", follow_redirects=True).get_data(as_text=True)
        self.assertNotIn("FOREIGN KEY constraint failed", html)
        self.assertIn("Schedule cleared", html)
        self.assertIsNone(self.q1("SELECT id FROM scheduled_flights WHERE id = ?", (self.booking,)))

    def test_waitlist_offer_kept_but_unlinked(self):
        c = self.login("master")
        c.post("/admin/reset/schedule")
        offer = self.q1("SELECT * FROM waitlist_offers WHERE id = ?", (self.offer,))
        self.assertIsNotNone(offer)
        self.assertIsNone(offer["cancelled_flight_id"])


class ScheduleResetCoversEveryForeignKeyTest(OpsHubTestCase):
    """Guard for the future: every table with a hard foreign key to
    scheduled_flights must be handled by _wipe_schedule, or Reset Schedule
    will start failing again the first time that table has a row."""
    HANDLED = {("flights", "scheduled_flight_id"), ("waitlist_offers", "cancelled_flight_id"),
               ("weather_offers", "cancelled_flight_id")}

    def test_all_foreign_keys_to_bookings_are_handled(self):
        conn = db.get_db()
        tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        found = set()
        for t in tables:
            for fk in conn.execute(f"PRAGMA foreign_key_list('{t}')").fetchall():
                if fk["table"] == "scheduled_flights":
                    found.add((t, fk["from"]))
        conn.close()
        self.assertEqual(found - self.HANDLED, set(),
                         "New foreign key to scheduled_flights - un-link it in _wipe_schedule in app.py")


class OtherResetsWithWaitlistTest(OpsHubTestCase):
    """Reset Students / Planes / Instructors hit the same kind of crash when
    the cancellation waitlist (flight_waitlist, waitlist_offers) pointed at
    the student, plane or instructor being removed."""
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.asset = self.make_asset("N557PL")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.asset,))
        self.wl = self.exec(
            """INSERT INTO flight_waitlist (student_id, days, periods, asset_id, cfi_id, created_at)
               VALUES (?,?,?,?,?,?)""", (self.student, "0,1", "morning", self.asset, self.cfi, db.now_iso()))
        self.exec(
            """INSERT INTO waitlist_offers (waitlist_id, student_id, asset_id, cfi_id,
                                            scheduled_date, scheduled_time, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (self.wl, self.student, self.asset, self.cfi, date.today().isoformat(), "09:00", db.now_iso()))

    def _reset(self, what):
        html = self.login("master").post(f"/admin/reset/{what}", follow_redirects=True).get_data(as_text=True)
        self.assertNotIn("FOREIGN KEY constraint failed", html)
        self.assertNotIn("Reset didn't run", html)

    def test_reset_students(self):
        self._reset("students")
        self.assertIsNone(self.q1("SELECT id FROM students WHERE id = ?", (self.student,)))

    def test_reset_planes(self):
        self._reset("planes")
        self.assertIsNone(self.q1("SELECT id FROM assets WHERE id = ?", (self.asset,)))

    def test_reset_instructors(self):
        self._reset("instructors")
