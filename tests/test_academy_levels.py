"""ACAD-1: the Academy leaderboard can be limited to one pilot level."""
import re

import db
from harness import OpsHubTestCase, seed_row
import academy


class LevelFilterTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        conn = db.get_db()
        self.ids = {}
        for name, cert in (("Sam Student", "student"), ("Pat PreSolo", None), ("Rae Rec", "recreational"),
                           ("Pip Private", "private"), ("Cal Commercial", "commercial"), ("Al ATP", "atp")):
            self.ids[name] = seed_row(conn, "students", name=name, username=name.lower().replace(" ", "_"), active=1, pilot_certificate=cert)
        conn.commit()
        conn.close()

    @staticmethod
    def on_boards(html):
        """Student names shown in the leaderboard rows (not the staff picker)."""
        return set(re.findall(r'<span class="flex-grow-1 text-truncate">\s*([^<]+?)\s*(?:<|$)', html))

    def test_level_buckets(self):
        conn = db.get_db()
        stats = academy.student_stats(conn)
        conn.close()
        got = {n: academy.level_of(stats[i]["student"]) for n, i in self.ids.items()}
        self.assertEqual(got, {"Sam Student": "student", "Pat PreSolo": "student", "Rae Rec": "sport_rec",
                               "Pip Private": "private", "Cal Commercial": "commercial", "Al ATP": "commercial"})

    def test_only_level_keeps_just_that_group(self):
        conn = db.get_db()
        stats = academy.student_stats(conn)
        conn.close()
        names = lambda lv: {st["name"] for st in academy.only_level(stats, lv).values() if st["name"] in self.ids}
        self.assertEqual(names("student"), {"Sam Student", "Pat PreSolo"})
        self.assertEqual(names("commercial"), {"Cal Commercial", "Al ATP"})
        self.assertTrue({"Sam Student", "Al ATP"} <= names("all"))

    def test_page_has_the_filter_and_it_changes_the_boards(self):
        self.exec("UPDATE students SET pilot_certificate = 'private' WHERE name = 'Pip Private'")
        conn = db.get_db()
        # give everyone some points so they appear on a board
        for i in self.ids.values():
            conn.execute("INSERT INTO academy_entries (student_id, kind, value, entry_date, created_at) VALUES (?, 'landings', 5, '2026-10-01', '2026-10-01')", (i,))
        conn.commit()
        conn.close()
        html = self.login("master").get("/academy").get_data(as_text=True)
        self.assertIn("Student pilots", html)
        self.assertIn("Commercial / ATP", html)
        board = self.on_boards(self.login("master").get("/academy?level=student").get_data(as_text=True))
        self.assertIn("Sam Student", board)
        self.assertNotIn("Cal Commercial", board)
        self.assertNotIn("Pip Private", board)
        everyone = self.on_boards(self.login("master").get("/academy?level=all").get_data(as_text=True))
        self.assertTrue({"Sam Student", "Cal Commercial", "Pip Private"} <= everyone)

    def test_a_student_lands_on_their_own_level_but_can_pick_everyone(self):
        self.exec("UPDATE users SET academy_access = 1 WHERE id = ?", (self.users["flight_student"]["id"],))
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE students SET pilot_certificate = 'private' WHERE id = ?", (sid,))
        conn = db.get_db()
        for name in ("Sam Student", "Cal Commercial"):
            conn.execute("INSERT INTO academy_entries (student_id, kind, value, entry_date, created_at) VALUES (?, 'landings', 9, '2026-10-01', '2026-10-01')", (self.ids[name],))
        conn.commit()
        conn.close()
        c = self.login("flight_student")
        default = self.on_boards(c.get("/academy").get_data(as_text=True))
        self.assertNotIn("Cal Commercial", default)
        self.assertNotIn("Sam Student", default)
        everyone = self.on_boards(c.get("/academy?level=all").get_data(as_text=True))
        self.assertIn("Cal Commercial", everyone)
