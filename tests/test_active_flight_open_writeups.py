"""Idea "open write-ups before you fly": the Active Flight card shows a small
yellow box listing the plane's open write-ups (flight squawks and shop
squawks), only while some are open."""
from harness import OpsHubTestCase
import db


class ActiveFlightOpenWriteupsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("cfi")
        cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        stu = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.asset_id = self.make_asset("N123")
        self.exec("INSERT INTO flights (asset_id, cfi_id, student_id, started_at, paused_seconds, flight_date) "
                  "VALUES (?,?,?,?,0,?)", (self.asset_id, cfi, stu, db.now_iso(), db.now_iso()[:10]))

    def page(self):
        return self.client.get("/flight/log/active").get_data(as_text=True)

    def test_nothing_shows_without_open_writeups(self):
        self.assertNotIn("open write-up", self.page())

    def test_lists_open_writeups_with_age_and_seen(self):
        self.exec("INSERT INTO plane_squawks (asset_id, notes, reported_at) VALUES (?,?,datetime('now','-3 days'))",
                  (self.asset_id, "Left brake feels soft"))
        self.exec("INSERT INTO plane_squawks (asset_id, notes, reported_at, acknowledged_at) "
                  "VALUES (?,?,datetime('now','-6 days'),datetime('now'))", (self.asset_id, "Right nav light out"))
        h = self.page()
        self.assertIn("2 open write-ups on N123", h)
        self.assertIn("Left brake feels soft", h)
        self.assertIn("3 days ago, not looked at yet", h)
        self.assertIn("6 days ago, shop has seen it", h)
        self.assertIn("Check these on your preflight", h)

    def test_repaired_ones_disappear(self):
        self.exec("INSERT INTO plane_squawks (asset_id, notes, repaired_at) VALUES (?,?,datetime('now'))",
                  (self.asset_id, "Old problem"))
        self.assertNotIn("Old problem", self.page())

    def test_other_planes_not_shown(self):
        other = self.make_asset("N999")
        self.exec("INSERT INTO plane_squawks (asset_id, notes) VALUES (?,?)", (other, "Wrong plane issue"))
        self.assertNotIn("Wrong plane issue", self.page())
