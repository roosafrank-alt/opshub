"""Logbook 'complex / turbine / TAA' box feeds the commercial 10 hours; a satisfactory PSI report checks the knowledge test."""
import io
from harness import OpsHubTestCase
import pilotlog


class ComplexAndPsiTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE users SET academy_access = 1 WHERE username = 'flight_student'")
        self.exec("UPDATE students SET pilot_certificate = 'private', training_goal = 'commercial' WHERE id = ?", (self.sid,))

    def item(self, track, code):
        from db import get_db
        with self.app.app_context():
            conn = get_db()
            t = next(t for t in pilotlog.build_progress(conn, conn.execute("SELECT * FROM students WHERE id=?", (self.sid,)).fetchone()) if t["code"] == track)
            return next(i for i in t["items"] if i["code"] == code)

    def test_complex_box_counts_toward_commercial(self):
        self.exec("""INSERT INTO pilot_logbook (student_id, entry_date, total, dual, complex_tae, status) VALUES (?, '2026-10-01', 2.0, 1.5, 1, 'complete')""", (self.sid,))
        self.exec("""INSERT INTO pilot_logbook (student_id, entry_date, total, dual, complex_tae, status) VALUES (?, '2026-10-02', 3.0, 3.0, 0, 'complete')""", (self.sid,))
        it = self.item("commercial", "complex")
        self.assertEqual(it["kind"], "hours")
        self.assertEqual(it["have"], 1.5)

    def test_satisfactory_psi_report_checks_the_knowledge_test(self):
        self.exec("UPDATE students SET pilot_certificate = 'student', training_goal = 'private' WHERE id = ?", (self.sid,))
        c = self.login("flight_student")
        r = c.post("/academy/progress/test-report", data={"track": "private", "score": "64", "taken_date": "2026-10-20"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(self.item("private", "knowledge")["done"])
        r = c.post("/academy/progress/test-report", data={"track": "private", "score": "88", "taken_date": "2026-10-22",
                                                           "report": (io.BytesIO(b"%PDF-1.4 test"), "psi.pdf")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.item("private", "knowledge")["done"])
        html = c.get("/academy/progress").get_data(as_text=True)
        self.assertIn("Satisfactory", html)
        self.assertIn("Unsatisfactory", html)
        rid = self.q1("SELECT id FROM academy_test_reports WHERE filename IS NOT NULL")["id"]
        self.assertEqual(c.get(f"/academy/progress/test-report/{rid}/file").status_code, 200)


class KnowledgeTestsTabTest(ComplexAndPsiTest):
    def test_tab_lists_reports_after_endorsements(self):
        self.exec("UPDATE students SET pilot_certificate = 'student', training_goal = 'private' WHERE id = ?", (self.sid,))
        c = self.login("flight_student")
        r = c.post("/academy/progress/test-report", data={"track": "private", "score": "91", "back": "tests",
                                                           "report": (io.BytesIO(b"%PDF-1.4 x"), "p.pdf")}, content_type="multipart/form-data")
        self.assertIn("/academy/knowledge-tests", r.headers["Location"])
        html = c.get("/academy/knowledge-tests").get_data(as_text=True)
        self.assertIn("Satisfactory", html)
        self.assertIn("View report", html)
        self.assertLess(html.index("Endorsements</a>"), html.index("Knowledge tests</a>"))


class PlaneFeaturesTest(OpsHubTestCase):
    def test_new_features_apply_going_forward_only(self):
        from datetime import date, timedelta
        import pilotlog
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        aid = self.exec("INSERT INTO assets (tag, name, is_flight_asset, school_listed) VALUES ('N1TW', 'TW', 1, 1)")
        old = (date.today() - timedelta(days=30)).isoformat()
        fid = self.exec("INSERT INTO flights (student_id, asset_id, flight_date) VALUES (?, ?, ?)", (sid, aid, old))
        eid = self.exec("""INSERT INTO pilot_logbook (student_id, asset_id, flight_id, entry_date, total, dual, status)
                           VALUES (?, ?, ?, ?, 1.0, 1.0, 'pending')""", (sid, aid, fid, old))
        r = self.login("flight_admin").post(f"/flight/planes/{aid}/edit", data={"tag": "N1TW", "make": "Cessna", "model": "T", "is_flight_asset": "on",
                                                                                   "ac_features": ["taa", "complex"]})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self.q1("SELECT ac_features, complex_tae FROM pilot_logbook WHERE id = ?", (eid,))["ac_features"], None)
        from db import get_db
        with self.app.app_context():
            conn = get_db()
            pilotlog.ensure_entry(conn, fid)            # a refresh of the old, still pending entry must not add them either
            conn.commit()
        self.assertIsNone(self.q1("SELECT ac_features FROM pilot_logbook WHERE id = ?", (eid,))["ac_features"])
        new = date.today().isoformat()
        fid2 = self.exec("INSERT INTO flights (student_id, asset_id, flight_date) VALUES (?, ?, ?)", (sid, aid, new))
        with self.app.app_context():
            conn = get_db()
            pilotlog.ensure_entry(conn, fid2)
            conn.commit()
        e = self.q1("SELECT ac_features, complex_tae FROM pilot_logbook WHERE flight_id = ?", (fid2,))
        self.assertEqual((e["ac_features"], e["complex_tae"]), ("complex,taa", 1))
        html = self.login("flight_admin").get(f"/flight/planes/{aid}/edit").get_data(as_text=True)
        self.assertIn("Pressurized", html)


class LogAFlightTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE users SET academy_access = 1 WHERE username = 'flight_student'")

    def test_big_button_and_popup_on_own_logbook(self):
        html = self.login("flight_student").get("/academy/logbook").get_data(as_text=True)
        self.assertIn("Log a flight", html)
        self.assertIn('id="logFlightModal"', html)

    def test_log_a_flight_saves_capped_entry(self):
        c = self.login("flight_student")
        r = c.post("/academy/logbook/new", data={"entry_date": "2026-10-20", "tail": "n123ab", "make_model": "Cessna 172", "route_from": "N89",
                                                  "route_to": "KPOU", "total": "1.5", "xc": "9", "day_ldg": "3", "ac_features": ["complex", "tailwheel"]})
        self.assertEqual(r.status_code, 302)
        e = self.q1("SELECT * FROM pilot_logbook WHERE student_id = ? AND source = 'manual'", (self.sid,))
        self.assertEqual((e["tail"], e["total"], e["xc"], e["day_ldg"], e["complex_tae"]), ("N123AB", 1.5, 1.5, 3, 1))
        self.assertEqual(e["ac_features"], "tailwheel,complex")

    def test_admin_cannot_log_into_someone_elses_logbook(self):
        c = self.login("master")
        c.post("/academy/logbook/new", data={"student_id": self.sid, "entry_date": "2026-10-20", "total": "1.0"})
        self.assertIsNone(self.q1("SELECT id FROM pilot_logbook WHERE student_id = ? AND source = 'manual'", (self.sid,)))
