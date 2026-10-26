"""Progress page: an instructor / admin picks the specific logbook flights that meet a checkoff requirement."""
from harness import OpsHubTestCase


class ProgressPickFlightsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.other = self.exec("INSERT INTO students (name, username, password_hash, active) VALUES ('Other S', 'other_s', 'x', 1)")
        mk = lambda sid, d, tail: self.exec("""INSERT INTO pilot_logbook (student_id, entry_date, tail, route_from, route_to, total, status, created_at, updated_at)
                                              VALUES (?, ?, ?, 'N89', 'KPOU', 2.0, 'complete', 'x', 'x')""", (sid, d, tail))
        self.f1, self.f2, self.fx = mk(self.sid, "2026-10-01", "N1"), mk(self.sid, "2026-10-05", "N2"), mk(self.other, "2026-10-06", "N3")
        self.exec("UPDATE users SET academy_access = 1 WHERE username IN ('flight_student', 'cfi')")

    def post(self, who, ids, **kw):
        return self.login(who).post("/academy/progress/milestone/flights", data={
            "student_id": self.sid, "track": "private", "item": "long_xc", "entry_id": ids, **kw})

    def test_picking_flights_links_them_and_checks_the_requirement_off(self):
        r = self.post("cfi", [str(self.f1), str(self.f2)])
        self.assertEqual(r.status_code, 302)
        links = self.q("SELECT entry_id FROM academy_milestone_flights WHERE student_id = ?", (self.sid,))
        self.assertEqual({x["entry_id"] for x in links}, {self.f1, self.f2})
        m = self.q1("SELECT * FROM academy_milestones WHERE student_id=? AND item='long_xc'", (self.sid,))
        self.assertEqual(m["done_date"], "2026-10-05")
        page = self.login("flight_student").get("/academy/progress?track=private").get_data(as_text=True)
        self.assertIn("N2", page)

    def test_another_students_flight_is_ignored(self):
        self.post("cfi", [str(self.fx)])
        self.assertIsNone(self.q1("SELECT id FROM academy_milestone_flights"))
        self.assertIsNone(self.q1("SELECT id FROM academy_milestones"))

    def test_students_cannot_pick(self):
        self.post("flight_student", [str(self.f1)])
        self.assertIsNone(self.q1("SELECT id FROM academy_milestone_flights"))

    def test_edit_button_only_for_staff(self):
        self.assertIn("pick-flights", self.login("cfi").get(f"/academy/progress?student_id={self.sid}&track=private").get_data(as_text=True))
        self.assertNotIn("pick-flights", self.login("flight_student").get("/academy/progress?track=private").get_data(as_text=True))

    def test_clearing_keeps_the_check_but_drops_the_flights(self):
        self.post("cfi", [str(self.f1)])
        self.post("cfi", [])
        self.assertIsNone(self.q1("SELECT id FROM academy_milestone_flights"))
