"""100-hour countdown and grounding (QA feat-100hr-countdown-grounding)."""
from datetime import date, timedelta

from harness import OpsHubTestCase, seed_row
import db
import notify


class HundredHourTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N2231Q")
        self.exec("UPDATE assets SET is_flight_asset = 1, tach_hours = 850 WHERE id = ?", (self.plane,))
        self.item = self.exec("""INSERT INTO maintenance_items (asset_id, name, type, category, hour_type, interval_hours,
                                 last_done_hours, active) VALUES (?, '100-Hour Inspection', 'hours', '100hour', 'tach', 100, 758.4, 1)""",
                              (self.plane,))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        conn = db.get_db()
        self.student = seed_row(conn, "students", name="Stu", active=1)
        conn.commit()
        conn.close()

    def book(self, days=2, time="10:00"):
        conn = db.get_db()
        sid = seed_row(conn, "scheduled_flights", asset_id=self.plane, student_id=self.student, cfi_id=self.cfi,
                       scheduled_date=(date.today() + timedelta(days=days)).isoformat(), scheduled_time=time,
                       duration_hours=1.5, status="scheduled")
        conn.commit()
        conn.close()
        return sid

    def test_admin_sees_hours_left_others_do_not(self):
        html = self.login("master").get("/flight/planes").get_data(as_text=True)
        self.assertIn("8.4 left", html)
        alerts = self.login("master").get("/flight/alerts").get_data(as_text=True)
        self.assertIn("100-Hour Inspection Due", alerts)
        self.assertIn("8.4 hrs left", alerts)
        for role in ("cfi",):
            with self.subTest(role=role):
                c = self.login(role)
                self.assertNotIn("8.4 left", c.get("/flight/planes").get_data(as_text=True))
                self.assertNotIn("100-Hour Inspection Due", c.get("/flight/alerts").get_data(as_text=True))

    def test_ground_blocks_bookings_and_flags_existing(self):
        sid = self.book()
        self.login("cfi").post(f"/flight/planes/{self.plane}/ground")  # not an admin
        self.assertIsNone(self.q1("SELECT grounded_at FROM assets WHERE id=?", (self.plane,))["grounded_at"])
        self.login("master").post(f"/flight/planes/{self.plane}/ground")
        self.assertIsNotNone(self.q1("SELECT grounded_at FROM assets WHERE id=?", (self.plane,))["grounded_at"])
        f = self.q1("SELECT needs_review, review_reason FROM scheduled_flights WHERE id=?", (sid,))
        self.assertEqual(f["needs_review"], 1)
        self.assertIn("grounded", f["review_reason"])
        # New booking on it is refused with the reason.
        before = len(self.q("SELECT id FROM scheduled_flights"))
        r = self.login("cfi").post("/flight/schedule/new", data=dict(
            asset_id=str(self.plane), student_id=str(self.student), cfi_id=str(self.cfi),
            scheduled_date=(date.today() + timedelta(days=3)).isoformat(), scheduled_time="14:00", duration_hours="1.5"))
        self.assertIn("down for maintenance", r.get_data(as_text=True))
        self.assertEqual(len(self.q("SELECT id FROM scheduled_flights")), before)
        # Return to service clears the flag.
        self.login("master").post(f"/flight/planes/{self.plane}/return-to-service")
        self.assertIsNone(self.q1("SELECT grounded_at FROM assets WHERE id=?", (self.plane,))["grounded_at"])
        self.assertEqual(self.q1("SELECT needs_review FROM scheduled_flights WHERE id=?", (sid,))["needs_review"], 0)

    def test_completing_100hr_project_resets_and_returns_to_service(self):
        self.login("master").post(f"/flight/planes/{self.plane}/ground")
        pid = self.make_project(name="100hr Inspection - N2231Q", asset_id=self.plane)
        self.login("shop_admin").post(f"/projects/{pid}/status", data=dict(status="completed"))
        self.assertEqual(self.q1("SELECT last_done_hours FROM maintenance_items WHERE id=?", (self.item,))["last_done_hours"], 850)
        self.assertIsNone(self.q1("SELECT grounded_at FROM assets WHERE id=?", (self.plane,))["grounded_at"])
        self.assertIn("100.0 left", self.login("master").get("/flight/planes").get_data(as_text=True))

    def test_not_yet_hides_box_until_zero(self):
        self.login("master").post(f"/flight/planes/{self.plane}/100hr-not-yet")
        self.assertNotIn("8.4 hrs left", self.login("master").get("/flight/alerts").get_data(as_text=True))
        self.exec("UPDATE assets SET tach_hours = 860 WHERE id = ?", (self.plane,))
        self.assertIn("hrs past", self.login("master").get("/flight/alerts").get_data(as_text=True))

    def test_admin_notified_once_at_ten_and_again_at_zero(self):
        from flight import check_hundred_hr_alerts
        self.exec("UPDATE users SET notify_email=1, email='boss@example.com' WHERE username='master'")
        sent = []
        orig = notify.notify_user
        notify.notify_user = lambda st, u, subj, body, brand=None: (sent.append(subj) or True)
        try:
            with self.app.test_request_context():
                conn = db.get_db()
                check_hundred_hr_alerts(conn)
                check_hundred_hr_alerts(conn)
                self.assertEqual(len(sent), 1)
                conn.execute("UPDATE assets SET tach_hours = 860 WHERE id = ?", (self.plane,))
                conn.commit()
                check_hundred_hr_alerts(conn)
                conn.close()
        finally:
            notify.notify_user = orig
        self.assertEqual(len(sent), 2)
        self.assertIn("URGENT", sent[1])
