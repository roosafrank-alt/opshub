"""Idea "Weather cancellation" (revision): the hover pop-up on an X'd-out
weather-cancelled lesson uses the waitlist-style card (header line, then
"Label: value" lines) and names who cancelled it - see _decorate_schedule_row
in flight.py."""
from datetime import date, timedelta
import html

from harness import OpsHubTestCase
import db


class WeatherCancelTipTest(OpsHubTestCase):
    def _tip(self, **cols):
        asset = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", ("N22689", "Plane", 1, 100.0, 90.0, db.now_iso(), db.now_iso()))
        cfi_id = self.q1("SELECT id FROM cfis WHERE username = 'cfi'")["id"]
        student_id = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]
        day = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        base = {"asset_id": asset, "cfi_id": cfi_id, "student_id": student_id, "scheduled_date": day,
                "scheduled_time": "10:00", "duration_hours": 1.0, "status": "cancelled",
                "cancel_reason": "Weather cancellation", "created_at": db.now_iso()}
        base.update(cols)
        self.exec("INSERT INTO scheduled_flights (%s) VALUES (%s)" % (", ".join(base), ",".join("?" * len(base))),
                  tuple(base.values()))
        body = self.login("cfi").get("/flight/schedule?view=day&date=" + day).get_data(as_text=True)
        marker = 'data-late-tip="'
        if marker not in body:
            return None
        i = body.index(marker) + len(marker)
        return html.unescape(body[i:body.index('"', i)])

    def test_tip_lists_student_plane_instructor_canceller_and_note(self):
        tip = self._tip(cancelled_by="Kate CFI", cancel_note="Low ceilings")
        lines = tip.split("\n")
        self.assertEqual(lines[0], "Cancelled for weather")
        self.assertIn("Plane: N22689", lines)
        self.assertIn("Cancelled by: Kate CFI", lines)
        self.assertEqual(lines[-1], "Note: Low ceilings")
        self.assertTrue(any(l.startswith("Student: ") for l in lines))
        self.assertTrue(any(l.startswith("Instructor: ") for l in lines))

    def test_old_cancellations_without_a_canceller(self):
        tip = self._tip()
        self.assertEqual(tip.split("\n")[0], "Cancelled for weather")
        self.assertNotIn("Cancelled by", tip)

    def test_other_cancellations_have_no_tip(self):
        self.assertIsNone(self._tip(cancel_reason="Flight cancellation"))
