"""QA fix ux-academy-log-first: the Flight Academy tab row is now one line
(Board, Logbook, Ground School, More - Resources joins the row when there's
room, else it lives under More with How points work), Logbook/Progress/Map
are sub-tabs inside Logbook instead of three separate top tabs, and Log Your
Flying sits right under the Flight Academy banner instead of below the
leaderboards. See _academy_tabs.html / _academy_logbook_subtabs.html."""
from harness import OpsHubTestCase


class AcademyTabsLayoutTest(OpsHubTestCase):
    def test_board_page_has_one_line_tab_row_with_more(self):
        c = self.login("master")
        body = c.get("/academy").get_data(as_text=True)
        self.assertIn('class="academy-tabs-row"', body)
        self.assertIn(">Board<", body)
        self.assertIn(">Ground School<", body)
        self.assertNotIn(">More<", body)
        self.assertIn(">Resources<", body)
        # Progress/Map are no longer separate top-level tabs on the Board page.
        self.assertNotIn('href="/academy/progress"', body)
        self.assertNotIn('href="/academy/map"', body)

    def test_log_your_flying_is_on_the_logbook_not_the_board(self):
        c = self.login("master")
        self.assertNotIn("Log Your Flying", c.get("/academy").get_data(as_text=True))
        self.assertIn("Log Your Flying", c.get("/academy/logbook").get_data(as_text=True))

    def test_points_board_has_an_info_icon_for_how_points_work(self):
        c = self.login("master")
        body = c.get("/academy").get_data(as_text=True)
        self.assertIn('aria-label="How points work"', body)

    def test_how_points_work_is_a_modal_not_a_bottom_list(self):
        c = self.login("master")
        body = c.get("/academy").get_data(as_text=True)
        self.assertIn('id="howPointsModal"', body)
        self.assertIn("Flight hours: 10 each", body)
        self.assertNotIn('href="#how"', body)
        self.assertNotIn('id="how"', body)

    def test_logbook_page_shows_logbook_progress_map_subtabs(self):
        c = self.login("master")
        body = c.get("/academy/logbook").get_data(as_text=True)
        self.assertIn('class="nav nav-pills academy-subtabs', body)
        self.assertIn('href="/academy/progress"', body)
        self.assertIn('href="/academy/map"', body)
        self.assertIn('id="howPointsModal"', body)

    def test_progress_and_map_pages_show_the_same_subtabs(self):
        c = self.login("master")
        for path in ("/academy/progress", "/academy/map"):
            body = c.get(path).get_data(as_text=True)
            self.assertIn('class="nav nav-pills academy-subtabs', body)

    def test_ground_school_page_has_no_logbook_subtabs(self):
        c = self.login("master")
        body = c.get("/flight/groundschool").get_data(as_text=True)
        self.assertIn('class="academy-tabs-row"', body)
        self.assertNotIn('class="nav nav-pills academy-subtabs', body)
        self.assertIn('id="howPointsModal"', body)

    def test_resources_page_has_the_tabs_and_modal_too(self):
        c = self.login("master")
        body = c.get("/flight/groundschool/resources").get_data(as_text=True)
        self.assertIn('class="academy-tabs-row"', body)
        self.assertIn('id="howPointsModal"', body)


class DenyPendingLogbookEntryTest(OpsHubTestCase):
    def test_deny_removes_entry_from_totals_and_can_be_reopened(self):
        sid = self.q1("SELECT id FROM students LIMIT 1")["id"]
        self.exec("""INSERT INTO pilot_logbook (student_id, entry_date, tail, total, status, created_at, updated_at)
                     VALUES (?, '2026-10-01', 'N1', 1.5, 'pending', 'x', 'x')""", (sid,))
        eid = self.q1("SELECT id FROM pilot_logbook WHERE student_id = ?", (sid,))["id"]
        c = self.login("master")
        self.assertIn('value="deny"', c.get(f"/academy/logbook/{eid}").get_data(as_text=True))
        c.post(f"/academy/logbook/{eid}", data={"action": "deny"})
        self.assertEqual(self.q1("SELECT status FROM pilot_logbook WHERE id = ?", (eid,))["status"], "denied")
        body = c.get(f"/academy/logbook?student_id={sid}").get_data(as_text=True)
        self.assertIn("denied (not counted)", body)
        c.post(f"/academy/logbook/{eid}", data={"action": "reopen"})
        self.assertEqual(self.q1("SELECT status FROM pilot_logbook WHERE id = ?", (eid,))["status"], "pending")


class AdminAsOwnStudentTest(OpsHubTestCase):
    def test_admin_can_take_courses_as_themselves_and_progress_saves(self):
        c = self.login("master")
        self.assertIn("Use it as a student myself", c.get("/view-as/flight/student").get_data(as_text=True))
        r = c.post("/view-as/flight/student/me")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get("/flight/groundschool").status_code, 200)
        # not look-only: a JSON POST is not refused
        r = c.post("/flight/groundschool/task/1/lesson-time", json={"seconds": 5})
        self.assertNotEqual(r.status_code, 403)


class SwitchRoleStaysOnPageTest(OpsHubTestCase):
    def test_switching_role_keeps_you_on_the_same_page_when_allowed(self):
        c = self.login("master")
        r = c.post("/view-as/shop/tech", headers={"Referer": "http://localhost/admin/users"})
        # Admin pages are closed to a Shop Tech, so it falls back to the dashboard...
        self.assertNotIn("/admin/users", r.headers["Location"])
        c.post("/view-as/exit")
        # ...but a page the new role may open is kept.
        r = c.post("/view-as/shop/tech", headers={"Referer": "http://localhost/shop"})
        self.assertTrue(r.headers["Location"].endswith("/shop"), r.headers["Location"])


class CfiCannotEditStudentLogbookTest(OpsHubTestCase):
    def test_cfi_reads_but_cannot_change_and_student_can(self):
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("""INSERT INTO pilot_logbook (student_id, entry_date, tail, total, status, created_at, updated_at)
                     VALUES (?, '2026-10-01', 'N1', 1.5, 'pending', 'x', 'x')""", (sid,))
        eid = self.q1("SELECT id FROM pilot_logbook WHERE student_id = ?", (sid,))["id"]
        self.exec("UPDATE users SET academy_access = 1 WHERE username IN ('flight_student', 'cfi')")
        cfi = self.login("cfi")
        self.assertEqual(cfi.get(f"/academy/logbook/{eid}").status_code, 200)
        cfi.post(f"/academy/logbook/{eid}", data={"action": "approve"})
        self.assertEqual(self.q1("SELECT status FROM pilot_logbook WHERE id = ?", (eid,))["status"], "pending")
        cfi.post("/academy/entry", data={"student_id": sid, "kind": "landings", "value": "2"})
        self.assertIsNone(self.q1("SELECT id FROM academy_entries WHERE student_id = ?", (sid,)))
        stu = self.login("flight_student")
        stu.post(f"/academy/logbook/{eid}", data={"action": "approve"})
        self.assertEqual(self.q1("SELECT status FROM pilot_logbook WHERE id = ?", (eid,))["status"], "complete")
