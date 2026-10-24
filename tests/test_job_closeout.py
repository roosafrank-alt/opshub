"""New feature feat-job-closeout-owner-ready: Mark Completed on a job opens
a "Close out this job" pop-up naming what's still loose on THIS job (an open
squawk, no logbook entry saved, nothing invoiced, someone still clocked in,
and any open Discrepancy/Sub Area) - each with a one-tap fix - plus "Tell
the owner it's ready", which reuses the same email/text machinery as Found
Items (app._notify_owner_found_items) rather than a new channel.
"""
from harness import OpsHubTestCase, SIDE_EFFECTS, seed_row
import db


class JobCloseoutTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.make_asset("N12345")
        self.project = self.make_project(name="Annual - N12345", asset_id=self.asset)
        self.exec("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (self.customer_id, self.asset))

    def detail_html(self, role="shop_admin"):
        return self.login(role).get(f"/projects/{self.project}").get_data(as_text=True)

    def test_checklist_shows_whats_still_loose(self):
        conn = db.get_db()
        laborer = seed_row(conn, "laborers", name="Tech A", code="LABOR-A", rate=85, active=1)
        # One finished session (makes the job billable) and one still
        # running (so someone's still clocked in on it).
        conn.execute("INSERT INTO labor_sessions (laborer_id, project_id, started_at, ended_at, hours, rate, cost, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (laborer, self.project, db.now_iso(), db.now_iso(), 1.0, 85, 85.0, db.now_iso()))
        conn.execute("INSERT INTO labor_sessions (laborer_id, project_id, started_at, rate, created_at) "
                     "VALUES (?, ?, ?, ?, ?)", (laborer, self.project, db.now_iso(), 85, db.now_iso()))
        conn.execute("INSERT INTO plane_squawks (asset_id, notes, reported_by, reported_at) VALUES (?, ?, ?, ?)",
                     (self.asset, "Left brake soft", "Tech", db.now_iso()))
        conn.commit()
        conn.close()
        html = self.detail_html()
        self.assertIn("Close out this job", html)
        self.assertIn("No logbook entry saved yet", html)
        self.assertIn("Draft it", html)
        self.assertIn("not invoiced yet", html)
        self.assertIn("Send invoice", html)
        self.assertIn("Squawk still open: Left brake soft", html)
        self.assertIn("Still clocked in: Tech A", html)
        self.assertIn("Tell the owner it's ready", html)
        self.assertIn("N12345 is ready for pickup", html)

    def test_only_whats_applicable_is_shown(self):
        # No open squawks, no labor at all, no billable amount, no Sub
        # Areas on this job: nothing invoiced and no Sub Area line at all,
        # since neither applies - but the plane's squawks and the clocked-in
        # check always apply, so they still show up green.
        html = self.detail_html()
        self.assertIn("Close out this job", html)
        self.assertIn("No open squawks on N12345", html)
        self.assertNotIn("Squawk still open", html)
        self.assertIn("No workers still clocked in", html)
        self.assertNotIn("invoiced", html)
        self.assertIn("No logbook entry saved yet", html)

    def test_discrepancy_check_reflects_sub_area_state(self):
        part = self.make_part()
        self.login("tech")
        self.scan(self.q1("SELECT barcode FROM parts WHERE id=?", (part,))["barcode"], "out",
                  qty=1, project_id=self.project, section="Brakes")
        html = self.detail_html()
        self.assertIn("Brakes discrepancy still open", html)
        self.assertIn("Open it", html)

        section_id = self.q1("SELECT id FROM project_sections WHERE project_id=? AND name='Brakes'",
                             (self.project,))["id"]
        self.login("shop_admin").post(f"/projects/{self.project}/sections/{section_id}/complete", data={"completed": "1"})
        self.login("inspector").post(f"/projects/{self.project}/sections/{section_id}/confirm", data={})
        html = self.detail_html()
        self.assertIn("Brakes discrepancy marked Done", html)

    def test_no_owner_linked_hides_the_notify_checkbox(self):
        other = self.make_asset("N999")
        other_project = self.make_project(name="Oil Change - N999", asset_id=other)
        html = self.login("shop_admin").get(f"/projects/{other_project}").get_data(as_text=True)
        self.assertIn("Close out this job", html)
        self.assertNotIn("Tell the owner it's ready", html)

    def test_mark_completed_without_the_checkbox_does_not_notify(self):
        before = len(SIDE_EFFECTS)
        self.login("shop_admin").post(f"/projects/{self.project}/status", data=dict(status="completed"))
        row = self.q1("SELECT * FROM projects WHERE id=?", (self.project,))
        self.assertEqual(row["status"], "completed")
        self.assertIsNone(row["ready_notified_at"])
        self.assertEqual(len(SIDE_EFFECTS), before)

    def test_mark_completed_with_the_checkbox_notifies_the_owner(self):
        # Configure email (faked in tests, but this makes notify.send_email
        # actually attempt it, like a real shop would have SMTP set up).
        self.exec("INSERT INTO app_settings (key, value) VALUES ('smtp_host', 'smtp.example.com')")
        self.exec("INSERT INTO app_settings (key, value) VALUES ('smtp_username', 'shop@example.com')")
        self.exec("INSERT INTO app_settings (key, value) VALUES ('smtp_password', 'secret')")
        before = len(SIDE_EFFECTS)
        r = self.login("shop_admin").post(f"/projects/{self.project}/status",
                                          data=dict(status="completed", notify_owner="on"))
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM projects WHERE id=?", (self.project,))
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["ready_notified_at"])
        self.assertEqual(row["ready_notified_by"], "Shop Admin")
        # Email/SMS are faked in tests (always blocked); the attempt is
        # still recorded so this proves the app tried to reach the owner.
        self.assertGreater(len(SIDE_EFFECTS), before)

    def test_double_complete_does_not_notify_again(self):
        c = self.login("shop_admin")
        c.post(f"/projects/{self.project}/status", data=dict(status="completed", notify_owner="on"))
        first = self.q1("SELECT ready_notified_at FROM projects WHERE id=?", (self.project,))["ready_notified_at"]
        c.post(f"/projects/{self.project}/status", data=dict(status="completed", notify_owner="on"))
        second = self.q1("SELECT ready_notified_at FROM projects WHERE id=?", (self.project,))["ready_notified_at"]
        self.assertEqual(first, second)

    def test_completed_or_archived_job_shows_no_closeout_modal(self):
        self.login("shop_admin").post(f"/projects/{self.project}/status", data=dict(status="completed"))
        html = self.detail_html()
        self.assertNotIn("Close out this job", html)
        self.assertNotIn("Mark Completed", html)

    def test_a_tech_cannot_reach_a_project_it_has_no_role_for(self):
        for role in ("cfi", "flight_student", "no_roles"):
            with self.subTest(role=role):
                r = self.login(role).get(f"/projects/{self.project}")
                self.assertNotEqual(r.status_code, 200)
