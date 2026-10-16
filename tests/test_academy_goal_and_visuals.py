"""Flight Academy: the "working toward" goal follows the profile's Training toward
(never a certificate already held), and the leaderboard has its podium, charts and map."""
import db
from harness import OpsHubTestCase, seed_row
from pilotlog import goal_track_code


class GoalTrackTest(OpsHubTestCase):
    def test_goal_rules(self):
        self.assertEqual(goal_track_code({"pilot_certificate": "atp", "pilot_ratings": "", "training_goal": "tailwheel"}), "tailwheel")
        self.assertIsNone(goal_track_code({"pilot_certificate": "atp", "pilot_ratings": "", "training_goal": None}))
        self.assertIsNone(goal_track_code({"pilot_certificate": "atp", "pilot_ratings": "", "training_goal": "atp"}))
        self.assertEqual(goal_track_code({"pilot_certificate": "private", "pilot_ratings": "", "training_goal": None}), "instrument")
        self.assertEqual(goal_track_code({"pilot_certificate": "private", "pilot_ratings": "instrument", "training_goal": None}), "commercial")
        self.assertEqual(goal_track_code({"pilot_certificate": "commercial", "pilot_ratings": "", "training_goal": "multi_engine"}), "multi")
        self.assertEqual(goal_track_code({"pilot_certificate": "commercial", "pilot_ratings": "tailwheel", "training_goal": "tailwheel"}), "cfi")
        self.assertEqual(goal_track_code({"pilot_certificate": None, "pilot_ratings": "", "training_goal": None}), "private")

    def test_logbook_shows_tailwheel_not_atp(self):
        uid = self.users["flight_student"]["id"]
        self.exec("UPDATE users SET academy_access = 1 WHERE id = ?", (uid,))
        self.exec("UPDATE students SET pilot_certificate = 'atp', training_goal = 'tailwheel' WHERE user_id = ?", (uid,))
        r = self.login("flight_student").get("/academy/logbook")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Working toward: Tailwheel Endorsement", html)
        self.assertNotIn("Working toward: Airline Transport Pilot", html)


class LeaderboardVisualsTest(OpsHubTestCase):
    def test_podium_charts_map(self):
        conn = db.get_db()
        for n in ("Ann Alpha", "Bob Bravo", "Cy Charlie"):
            sid = seed_row(conn, "students", name=n, username=n.split()[0].lower(), active=1)
            conn.execute("INSERT INTO academy_entries (student_id, kind, value, entry_date, created_at) VALUES (?, 'landings', ?, '2026-10-01', '2026-10-01')",
                         (sid, len(n)))
        conn.commit()
        conn.close()
        html = self.login("master").get("/academy?level=all").get_data(as_text=True)
        for bit in ("Top of the Board", "raceChart", "trendChart", "lb-map", "Explorer (airports)"):
            self.assertIn(bit, html)
