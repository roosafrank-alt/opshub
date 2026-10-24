"""Idea "scheduled flights get a hovering info box": a booked flight block
carries the same pop-up card as the late and waitlist chips (data-late-tip),
saying what its icons mean; students never get names or money."""
from datetime import date, timedelta
import html

from harness import OpsHubTestCase
import db


class ScheduleBlockHoverTipTest(OpsHubTestCase):
    def _tip(self, login, **cols):
        asset = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", ("N22689", "Plane", 1, 100.0, 90.0, db.now_iso(), db.now_iso()))
        cfi_id = self.q1("SELECT id FROM cfis WHERE username = 'cfi'")["id"]
        student_id = self.q1("SELECT id FROM students WHERE username = 'flight_student'")["id"]
        day = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        base = {"asset_id": asset, "cfi_id": cfi_id, "student_id": student_id, "scheduled_date": day,
                "scheduled_time": "10:00", "duration_hours": 1.0, "status": "scheduled",
                "created_at": db.now_iso()}
        base.update(cols)
        self.exec("INSERT INTO scheduled_flights (%s) VALUES (%s)" % (", ".join(base), ",".join("?" * len(base))),
                  tuple(base.values()))
        body = self.login(login).get("/flight/schedule?view=day&date=" + day).get_data(as_text=True)
        marker = 'data-late-tip="'
        if marker not in body:
            return None
        i = body.index(marker) + len(marker)
        return html.unescape(body[i:body.index('"', i)])

    def test_staff_tip_names_state_plane_and_people(self):
        lines = self._tip("cfi", status="balance_hold").split("\n")
        self.assertEqual(lines[0], "Pending - Balance Hold")
        self.assertIn("Plane: N22689", lines)
        self.assertTrue(any(l.startswith("Student: ") for l in lines))
        self.assertTrue(any(l.startswith("Instructor: ") for l in lines))
        self.assertTrue(any(l.startswith("Hold: ") for l in lines))

    def test_confirmation_is_explained(self):
        tip = self._tip("cfi", confirm_required=1)
        self.assertIn("Student OK: Booked for the student, waiting for their OK", tip)

    def test_student_tip_has_no_names(self):
        tip = self._tip("flight_student")
        self.assertIsNotNone(tip)
        self.assertNotIn("Student: ", tip)
        self.assertNotIn("Instructor: ", tip)
        self.assertIn("Plane: N22689", tip)
