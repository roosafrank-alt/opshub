"""Students can pick a leaderboard name so the boards don't show their real name."""
import re

import db
import academy
from harness import OpsHubTestCase, seed_row


class LeaderboardNameTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.exec("UPDATE users SET academy_access = 1 WHERE id = ?", (self.users["flight_student"]["id"],))
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.real = self.q1("SELECT name FROM students WHERE id = ?", (self.sid,))["name"]
        self.exec("INSERT INTO academy_entries (student_id, kind, value, entry_date, created_at) VALUES (?, 'landings', 9, '2026-10-01', '2026-10-01')", (self.sid,))

    def save(self, name):
        return self.login("flight_student").post("/academy/username", data={"leaderboard_name": name}, follow_redirects=True)

    def board(self):
        return self.login("flight_student").get("/academy?level=all").get_data(as_text=True)

    def test_chosen_name_replaces_real_name_on_boards(self):
        self.assertIn(self.real, self.board())
        self.save("SkyKing_7")
        html = self.board()
        self.assertIn("SkyKing_7", html)
        self.assertEqual(len(re.findall(r'text-truncate">\s*' + re.escape(self.real) + r"\b", html)), 0)

    def test_blank_goes_back_to_real_name(self):
        self.save("SkyKing_7")
        self.save("")
        self.assertIn(self.real, self.board())

    def test_inappropriate_names_are_refused(self):
        conn = db.get_db()
        for bad in ("fuck", "Sh1t", "b!tch", "fuuuck you", "xXnazi", "A$$hole", "a s s h o l e"):
            name, err = academy.check_leaderboard_name(conn, bad, self.sid)
            self.assertIsNone(name, bad)
            self.assertTrue(err)
        for ok in ("SkyKing_7", "Maverick", "Cessna Fan"):
            self.assertEqual(academy.check_leaderboard_name(conn, ok, self.sid), (ok, None))
        conn.close()
        self.save("fuck")
        self.assertIsNone(self.q1("SELECT leaderboard_name FROM students WHERE id = ?", (self.sid,))["leaderboard_name"])

    def test_names_must_be_unique_and_not_someone_elses_real_name(self):
        conn = db.get_db()
        other = seed_row(conn, "students", name="Zed Zulu", username="zed_z", active=1, leaderboard_name="Goose")
        conn.commit()
        for taken in ("goose", "ZED ZULU", "zed-zulu"):
            self.assertIsNone(academy.check_leaderboard_name(conn, taken, self.sid)[0], taken)
        conn.close()

    def test_length_and_characters(self):
        conn = db.get_db()
        for bad in ("ab", "x" * 21, "<script>"):
            self.assertIsNone(academy.check_leaderboard_name(conn, bad, self.sid)[0], bad)
        conn.close()
