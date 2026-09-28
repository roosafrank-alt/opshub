"""QA fix qa-viewas-marks-alerts-read: "View as someone" is meant to be
look-only. Before the fix, an admin opening a student's Alerts page while
viewing as them silently marked all of that student's alerts read (so the
student never saw the "New" highlight later), and letting the phone's
"check for new push alerts" pass run while viewing as someone could pick up
and clear that person's queued push alerts instead of leaving them for
their own device.

Both tests below fail without the fix (they were run against the old code
to confirm that) and pass with it."""
from harness import OpsHubTestCase, seed_row
import db


class ViewAsAlertsStayUnreadTest(OpsHubTestCase):
    def _student_id(self):
        return self.q1("SELECT id FROM students WHERE user_id = ?",
                        (self.users["flight_student"]["id"],))["id"]

    def test_viewing_as_student_does_not_mark_their_alert_read(self):
        student_id = self._student_id()
        conn = db.get_db()
        seed_row(conn, "student_notifications", student_id=student_id,
                 category="approval", message="Your flight was approved",
                 created_at=db.now_iso(), read_at=None)
        conn.commit()
        conn.close()

        admin = self.login("master")
        admin.post(f"/view-as/person/{self.users['flight_student']['id']}")

        # The admin, viewing as the student, opens Alerts: the page still
        # shows it as new (badge + message)...
        html = admin.get("/flight/notifications").get_data(as_text=True)
        self.assertIn("New", html)
        self.assertIn("Your flight was approved", html)

        # ...but it must NOT have been marked read on their behalf.
        row = self.q1("SELECT read_at FROM student_notifications WHERE student_id = ?", (student_id,))
        self.assertIsNone(row["read_at"])

        # Back to the admin's own view, then the student logs in for real:
        # they still see it highlighted as new themselves.
        admin.post("/view-as/person/exit")
        student_client = self.login("flight_student")
        html2 = student_client.get("/flight/notifications").get_data(as_text=True)
        self.assertIn("New", html2)
        self.assertIn("Your flight was approved", html2)

        # And it IS marked read now, for the student's own real visit -
        # confirms the fix only skips the write during a person view, it
        # doesn't break the normal case.
        row2 = self.q1("SELECT read_at FROM student_notifications WHERE student_id = ?", (student_id,))
        self.assertIsNotNone(row2["read_at"])

    def test_viewing_as_someone_the_phone_alert_check_touches_no_ones_queue(self):
        cfi_user_id = self.users["cfi"]["id"]
        conn = db.get_db()
        # No created_at on purpose, so SQLite's DEFAULT (datetime('now'), UTC)
        # fills it exactly as push.queue_and_push leaves it. Passing
        # db.now_iso() instead wrote LOCAL time, which the pickup route -
        # comparing against datetime('now', '-30 minutes'), also UTC - read
        # as four hours old on the Pi and deleted as stale, so this failed
        # there and passed on any UTC machine.
        seed_row(conn, "push_pending", user_id=cfi_user_id, title="30 min left",
                 body="Session ends soon")
        conn.commit()
        conn.close()

        admin = self.login("master")
        admin.post(f"/view-as/person/{cfi_user_id}")

        # The phone-alert pickup route runs while viewing as the CFI - it
        # must not deliver the CFI's queued alert to the admin's device...
        resp = admin.get("/flight/push/pending")
        self.assertEqual(resp.get_json(), {"alerts": []})

        # ...and must leave the CFI's queued alert exactly where it was,
        # for the CFI's own device to pick up later.
        row = self.q1("SELECT * FROM push_pending WHERE user_id = ?", (cfi_user_id,))
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "30 min left")

        # Back to the admin's own view: the admin's own (empty) queue is
        # unaffected, and normal pickup still works once nobody is being
        # viewed - confirms the fix only skips this during a person view.
        admin.post("/view-as/person/exit")
        resp2 = admin.get("/flight/push/pending")
        self.assertEqual(resp2.get_json(), {"alerts": []})

        cfi_client = self.login("cfi")
        resp3 = cfi_client.get("/flight/push/pending")
        alerts = resp3.get_json()["alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["title"], "30 min left")
        # And it's cleared now that the real CFI picked it up.
        self.assertIsNone(self.q1("SELECT id FROM push_pending WHERE user_id = ?", (cfi_user_id,)))
