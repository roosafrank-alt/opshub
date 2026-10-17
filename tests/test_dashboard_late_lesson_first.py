"""QA ux-dash-two-lesson-buttons: when a CFI's earlier lesson is running late
and another lesson is coming up, the Next Lesson card used to show two
identical green Start Session / Log Flight pairs with the late one tucked
underneath. The late lesson now leads (tagged "Running late", named "Now")
with the only solid green Start button; the later one sits under "Then"
with quiet grey buttons. With nothing late the card is unchanged."""
import datetime
import unittest

from harness import OpsHubTestCase
import db


class DashboardLateLessonFirstTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", ("N2231Q", "Cessna 172", 1, 1000.0, 900.0, db.now_iso(), db.now_iso()))
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE username = 'cfi'")["id"]
        self.student_id = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]
        self.now = datetime.datetime.now()

    def _book(self, when, hours=1.0):
        self.exec(
            "INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date, scheduled_time, "
            "duration_hours, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (self.plane, self.cfi_id, self.student_id, when.strftime("%Y-%m-%d"),
             when.strftime("%H:%M"), hours, "scheduled", db.now_iso()))

    def _card(self):
        body = self.login("cfi").get("/flight/dashboard").get_data(as_text=True)
        return body

    def test_late_lesson_leads_and_later_lesson_gets_quiet_buttons(self):
        late = self.now - datetime.timedelta(minutes=20)
        later = self.now + datetime.timedelta(minutes=90)
        if late.date() != self.now.date() or later.date() != self.now.date():
            self.skipTest("too close to midnight")
        self._book(late)
        self._book(later)
        body = self._card()
        self.assertIn("Running late - booked", body)
        self.assertIn("Now &middot; N2231Q", body)
        self.assertLess(body.index("Running late - booked"), body.index(">Then<"))
        self.assertEqual(body.count("btn btn-success w-100 fw-bold"), 1)
        self.assertIn("btn-outline-secondary", body)

    def test_nothing_late_card_is_unchanged(self):
        later = self.now + datetime.timedelta(minutes=90)
        if later.date() != self.now.date():
            self.skipTest("too close to midnight")
        self._book(later)
        body = self._card()
        self.assertNotIn("Running late - booked", body)
        self.assertNotIn(">Then<", body)


if __name__ == "__main__":
    unittest.main()
