"""QA finding feat-grounded-move-plane: bookings flagged because their plane
is grounded get a one-tap 'Move to <free plane>' on Alerts."""
from datetime import date, timedelta

import db
from harness import OpsHubTestCase, seed_row


class GroundedMovePlane(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.bad = self._plane("N2231Q", 10)
        self.good = self._plane("N81PA", 20)
        self.day = (date.today() + timedelta(days=3)).isoformat()
        self.sf = self.exec("INSERT INTO scheduled_flights (asset_id, student_id, cfi_id, scheduled_date, scheduled_time, "
                            "duration_hours, status) VALUES (?,?,?,?,?,?, 'scheduled')",
                            (self.bad, self.student, self.cfi, self.day, "09:00", 2.0))
        self.login("master").post(f"/flight/planes/{self.bad}/ground", data={"reason": "tire"})

    def _plane(self, tag, order):
        return self.exec("INSERT INTO assets (tag, name, is_flight_asset, schedule_order, solo_allowed, created_at, updated_at) "
                         "VALUES (?,?,1,?,1,?,?)", (tag, tag, order, db.now_iso(), db.now_iso()))

    def test_alerts_offers_button_and_move_keeps_booking(self):
        html = self.login("master").get("/flight/alerts").get_data(as_text=True)
        self.assertIn("Move to N81PA", html)
        self.assertIn("Other time", html)
        r = self.client.post(f"/flight/schedule/{self.sf}/move-plane", data={"asset_id": self.good})
        self.assertEqual(r.status_code, 302)
        b = self.q1("SELECT * FROM scheduled_flights WHERE id = ?", (self.sf,))
        self.assertEqual((b["asset_id"], b["scheduled_date"], b["scheduled_time"], b["student_id"], b["cfi_id"], b["status"]),
                         (self.good, self.day, "09:00", self.student, self.cfi, "scheduled"))
        self.assertFalse(b["needs_review"])

    def test_cfi_may_move_student_may_not(self):
        self.login("flight_student").post(f"/flight/schedule/{self.sf}/move-plane", data={"asset_id": self.good})
        self.assertEqual(self.q1("SELECT asset_id FROM scheduled_flights WHERE id = ?", (self.sf,))["asset_id"], self.bad)
        self.login("cfi").post(f"/flight/schedule/{self.sf}/move-plane", data={"asset_id": self.good})
        self.assertEqual(self.q1("SELECT asset_id FROM scheduled_flights WHERE id = ?", (self.sf,))["asset_id"], self.good)

    def test_busy_target_not_offered_and_refused(self):
        other = self.q1("SELECT id FROM students WHERE id != ? LIMIT 1", (self.student,))
        sid = other["id"] if other else seed_row(db.get_db(), "students", name="Other S", created_at=db.now_iso())
        self.exec("INSERT INTO scheduled_flights (asset_id, student_id, scheduled_date, scheduled_time, duration_hours, status) "
                  "VALUES (?,?,?,?,?, 'scheduled')", (self.good, sid, self.day, "10:00", 1.0))
        html = self.login("master").get("/flight/alerts").get_data(as_text=True)
        self.assertNotIn("Move to N81PA", html)
        self.client.post(f"/flight/schedule/{self.sf}/move-plane", data={"asset_id": self.good})
        self.assertEqual(self.q1("SELECT asset_id FROM scheduled_flights WHERE id = ?", (self.sf,))["asset_id"], self.bad)

    def test_grounded_or_deleted_target_refused(self):
        self.exec("UPDATE assets SET grounded_at = ? WHERE id = ?", (db.now_iso(), self.good))
        self.login("master").post(f"/flight/schedule/{self.sf}/move-plane", data={"asset_id": self.good})
        self.assertEqual(self.q1("SELECT asset_id FROM scheduled_flights WHERE id = ?", (self.sf,))["asset_id"], self.bad)
        self.exec("UPDATE assets SET grounded_at = NULL, deleted_at = ? WHERE id = ?", (db.now_iso(), self.good))
        self.client.post(f"/flight/schedule/{self.sf}/move-plane", data={"asset_id": self.good})
        self.assertEqual(self.q1("SELECT asset_id FROM scheduled_flights WHERE id = ?", (self.sf,))["asset_id"], self.bad)
