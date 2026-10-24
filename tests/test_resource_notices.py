"""Flight Academy Resources: the 'Send this update to...' notice after an admin approves an update:
audience selection, who sees it, unread dot / mark read, and the no-double-send check."""
from harness import OpsHubTestCase, SIDE_EFFECTS
import db
import groundschool as gs

GS = "/flight/groundschool"


class ResourceNoticeTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        uid = lambda k: self.users[k]["id"]
        # flight_student: Private student with Instrument; cfi: has CFI + Complex credentials
        self.exec("UPDATE users SET groundschool_access = 1 WHERE id IN (?, ?)", (uid("flight_student"), uid("cfi")))
        self.exec("UPDATE students SET pilot_certificate = 'private', pilot_ratings = 'instrument' WHERE user_id = ?",
                  (uid("flight_student"),))
        self.exec("UPDATE cfis SET cred_cfi = 1, cred_complex = 1 WHERE user_id = ?", (uid("cfi"),))
        self.exec("INSERT INTO acs_resources (title, designation, filename, uploaded_at, doc_date, revision) "
                  "VALUES ('Rules', '14 CFR Part 91', 'x.pdf', ?, '2026-01-01', 'Amdt 1')", (db.now_iso(),))
        rid = self.q1("SELECT id FROM acs_resources")["id"]
        self.exec("INSERT INTO resource_updates (resource_id, summary, status, created_at, old_doc_date, old_revision) "
                  "VALUES (?, 'Added 91.109 Flight instruction.', 'approved', ?, '2025-01-01', 'Amdt 0')", (rid, db.now_iso()))
        self.uid = uid
        self.update_id = self.q1("SELECT id FROM resource_updates")["id"]

    def people(self):
        conn = db.get_db()
        try:
            return gs.notice_people(conn)
        finally:
            conn.close()

    def pick(self, mode, cats=(), ids=()):
        return gs.pick_audience(self.people(), mode, cats, ids)[2]

    def test_audience_selection(self):
        s, c, m = self.uid("flight_student"), self.uid("cfi"), self.uid("master")
        self.assertEqual(set(self.pick("all")) >= {s, c, m}, True)
        self.assertNotIn(self.uid("no_roles"), self.pick("all"))  # no Flight Academy access
        self.assertIn(c, self.pick("cfis"))
        self.assertNotIn(s, self.pick("cfis"))
        self.assertEqual([s], [i for i in self.pick("students") if i == s])
        self.assertNotIn(c, self.pick("students"))
        self.assertEqual(self.pick("cats", ["Private"]), [s])
        self.assertEqual(set(self.pick("cats", ["Instrument", "CFI"])), {s, c})
        self.assertEqual(self.pick("cats", ["Complex"]), [c])
        self.assertEqual(self.pick("cats", ["Commercial"]), [])
        self.assertEqual(self.pick("people", ids=[s, 99999]), [s])

    def test_granted_course_counts_as_enrolled(self):
        s = self.uid("flight_student")
        self.exec("INSERT INTO acs_ratings (name, slug, uploaded_at, status) VALUES ('Commercial Pilot ACS','c',?, 'parsed')",
                  (db.now_iso(),))
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (s,))["id"]
        self.exec("INSERT INTO academy_course_grants (student_id, rating_id, granted_at) VALUES (?, 1, ?)", (sid, db.now_iso()))
        self.assertIn(s, self.pick("cats", ["Commercial"]))

    def send(self, c, **data):
        data.setdefault("text", "Resources update: Rules\nWhat changed: x")
        return c.post(f"{GS}/resources/updates/{self.update_id}/send", data=data)

    def test_send_log_visibility_and_read(self):
        admin = self.login("master")
        page = admin.get(f"{GS}/resources/updates/{self.update_id}/send").get_data(as_text=True)
        self.assertIn("Added 91.109", page)
        self.assertIn("Old: 2025-01-01 | Amdt 0", page)
        self.assertIn("New: 2026-01-01 | Amdt 1", page)
        self.send(admin, audience="cats", cats=["Private"], text="Resources update: Rules\nEdited by admin")
        n = self.q1("SELECT * FROM resource_update_notices")
        self.assertEqual((n["recipient_count"], n["sent_by"], n["audience"]), (1, self.uid("master"), "cats:Private"))
        self.assertIn("Edited by admin", n["text"])
        # the student sees it with the unread dot; the CFI (not addressed) sees nothing
        stu = self.login("flight_student")
        body = stu.get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn("What's new in Resources", body)
        self.assertIn("Edited by admin", body)
        self.assertTrue(self.q1("SELECT read_at FROM resource_notice_recipients WHERE user_id = ?", (self.uid("flight_student"),))["read_at"])
        cfi = self.login("cfi")
        body = cfi.get(f"{GS}/resources").get_data(as_text=True)
        self.assertNotIn("What's new in Resources", body)
        self.assertNotIn("Edited by admin", body)
        # people cannot open the send step or the log
        for c in (stu, cfi):
            self.assertNotEqual(c.get(f"{GS}/resources/updates/{self.update_id}/send").status_code, 200)
            self.assertNotEqual(c.get(f"{GS}/resources/notices").status_code, 200)
        self.assertIn("Edited by admin", admin.get(f"{GS}/resources/notices").get_data(as_text=True))

    def test_unread_dot_until_opened(self):
        admin = self.login("master")
        self.send(admin, audience="students")
        stu = self.login("flight_student")
        row = self.q1("SELECT read_at FROM resource_notice_recipients WHERE user_id = ?", (self.uid("flight_student"),))
        self.assertIsNone(row["read_at"])
        r = stu.get(GS, follow_redirects=True)
        self.assertIn(b"New in Resources", r.data)
        stu.get(f"{GS}/resources")
        self.assertIsNotNone(self.q1("SELECT read_at FROM resource_notice_recipients WHERE user_id = ?",
                                     (self.uid("flight_student"),))["read_at"])
        self.assertNotIn(b"New in Resources", stu.get(f"{GS}/resources").data)

    def test_no_double_send(self):
        admin = self.login("master")
        self.send(admin, audience="students")
        r = self.send(admin, audience="students")
        self.assertIn("Send again?", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM resource_update_notices")["c"], 1)
        self.send(admin, audience="students", again="1")
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM resource_update_notices")["c"], 2)
        self.send(admin, audience="cfis")  # a different audience is not a repeat
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM resource_update_notices")["c"], 3)

    def test_specific_people_and_empty(self):
        admin = self.login("master")
        self.send(admin, audience="people", people=[str(self.uid("cfi"))])
        n = self.q1("SELECT * FROM resource_update_notices")
        self.assertEqual(n["recipient_count"], 1)
        self.send(admin, audience="cats", cats=["Commercial"])  # nobody matches
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM resource_update_notices")["c"], 1)

    def test_push_is_queued(self):
        admin = self.login("master")
        self.send(admin, audience="students")
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM push_pending WHERE user_id = ?", (self.uid("flight_student"),))["c"], 1)
