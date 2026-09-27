"""New feature feat-tool-calibration: a Calibrated Tools list tracks torque
wrenches, gauges and testers with a due date derived from the last
calibration plus the tool's own interval, overdue/due-soon badges, a
certificate upload, and a Maintenance dashboard reminder for anything
overdue or due within 30 days. A tool with no interval set is "not
required" and never shows a due date or a dashboard reminder.
"""
from datetime import date, timedelta
from harness import OpsHubTestCase
import db


class ToolCalibrationTest(OpsHubTestCase):
    def make_tool(self, **kw):
        conn = db.get_db()
        cur = conn.execute("""INSERT INTO shop_tools (name, serial, location, calibration_interval_days,
                              last_calibrated_date, next_due_date, created_at, updated_at)
                              VALUES (?,?,?,?,?,?,?,?)""",
                           (kw.get("name", "Torque Wrench"), kw.get("serial"), kw.get("location"),
                            kw.get("calibration_interval_days"), kw.get("last_calibrated_date"),
                            kw.get("next_due_date"), db.now_iso(), db.now_iso()))
        conn.commit()
        tid = cur.lastrowid
        conn.close()
        return tid

    def test_list_page_shows_add_tool_for_admin_not_for_tech(self):
        admin_html = self.login("shop_admin").get("/shop/tools").get_data(as_text=True)
        self.assertIn("Add Tool", admin_html)
        tech_html = self.login("tech").get("/shop/tools").get_data(as_text=True)
        self.assertNotIn("Add Tool", tech_html)

    def test_a_flight_only_account_cannot_reach_the_tools_page(self):
        r = self.login("cfi").get("/shop/tools", follow_redirects=True)
        self.assertNotIn("Calibrated Tools", r.get_data(as_text=True))

    def test_add_tool_creates_it_not_required_by_default(self):
        c = self.login("shop_admin")
        r = c.post("/shop/tools/new", data=dict(name="Timing Light", serial="MTL-02", location="Bay 1"),
                   follow_redirects=True)
        row = self.q1("SELECT * FROM shop_tools WHERE name = 'Timing Light'")
        self.assertIsNotNone(row)
        self.assertIsNone(row["calibration_interval_days"])
        self.assertIn("not required", r.get_data(as_text=True))

    def test_add_tool_with_interval_shows_not_yet_calibrated(self):
        c = self.login("shop_admin")
        c.post("/shop/tools/new", data=dict(name="New Gauge", calibration_interval_days="365"))
        html = c.get("/shop/tools").get_data(as_text=True)
        self.assertIn("not yet calibrated", html)

    def test_mark_calibrated_sets_last_and_next_due(self):
        tid = self.make_tool(name="Torque Wrench A", calibration_interval_days=365)
        c = self.login("shop_admin")
        c.post(f"/shop/tools/{tid}/calibrate", data=dict(calibrated_at="2026-01-15"), follow_redirects=True)
        row = self.q1("SELECT * FROM shop_tools WHERE id = ?", (tid,))
        self.assertEqual(row["last_calibrated_date"], "2026-01-15")
        self.assertEqual(row["next_due_date"], "2027-01-15")
        hist = self.q("SELECT * FROM tool_calibrations WHERE tool_id = ?", (tid,))
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["performed_by"], "Shop Admin")

    def test_mark_calibrated_refused_without_an_interval(self):
        tid = self.make_tool(name="Timing Light", calibration_interval_days=None)
        c = self.login("shop_admin")
        c.post(f"/shop/tools/{tid}/calibrate", data=dict(calibrated_at="2026-01-15"), follow_redirects=True)
        row = self.q1("SELECT * FROM shop_tools WHERE id = ?", (tid,))
        self.assertIsNone(row["last_calibrated_date"])

    def test_mark_calibrated_rejects_a_bad_cert_extension(self):
        import io
        tid = self.make_tool(name="Torque Wrench B", calibration_interval_days=365)
        c = self.login("shop_admin")
        c.post(f"/shop/tools/{tid}/calibrate",
              data=dict(calibrated_at="2026-01-15", cert=(io.BytesIO(b"bad"), "cert.exe")),
              content_type="multipart/form-data", follow_redirects=True)
        row = self.q1("SELECT * FROM shop_tools WHERE id = ?", (tid,))
        self.assertIsNone(row["last_calibrated_date"])

    def test_a_tech_can_mark_calibrated_but_not_edit_or_delete(self):
        tid = self.make_tool(name="Torque Wrench C", calibration_interval_days=365)
        c = self.login("tech")
        c.post(f"/shop/tools/{tid}/calibrate", data=dict(calibrated_at="2026-01-15"), follow_redirects=True)
        row = self.q1("SELECT * FROM shop_tools WHERE id = ?", (tid,))
        self.assertEqual(row["last_calibrated_date"], "2026-01-15")
        c.post(f"/shop/tools/{tid}/delete", follow_redirects=True)
        self.assertIsNone(self.q1("SELECT deleted_at FROM shop_tools WHERE id = ?", (tid,))["deleted_at"])

    def test_overdue_and_due_soon_badges(self):
        today = date.today()
        overdue_id = self.make_tool(name="Overdue Tool", calibration_interval_days=365,
                                    last_calibrated_date=(today - timedelta(days=380)).isoformat(),
                                    next_due_date=(today - timedelta(days=15)).isoformat())
        soon_id = self.make_tool(name="Due Soon Tool", calibration_interval_days=365,
                                 last_calibrated_date=(today - timedelta(days=342)).isoformat(),
                                 next_due_date=(today + timedelta(days=23)).isoformat())
        ok_id = self.make_tool(name="OK Tool", calibration_interval_days=365,
                               last_calibrated_date=(today - timedelta(days=30)).isoformat(),
                               next_due_date=(today + timedelta(days=335)).isoformat())
        html = self.login("shop_admin").get("/shop/tools").get_data(as_text=True)
        self.assertIn("Overdue 15 days", html)
        self.assertIn("Due in 23 days", html)
        self.assertIn("1 tool overdue, 1 due within 30 days", html)

    def test_not_required_tool_has_no_badge_and_no_dashboard_reminder(self):
        self.make_tool(name="Timing Light", calibration_interval_days=None)
        html = self.login("shop_admin").get("/shop/tools").get_data(as_text=True)
        self.assertIn("not required", html)
        dash = self.login("shop_admin").get("/shop").get_data(as_text=True)
        self.assertNotIn("Tools Due for Calibration", dash)

    def test_dashboard_reminder_shows_overdue_and_due_soon_only(self):
        today = date.today()
        self.make_tool(name="Overdue Tool", calibration_interval_days=365,
                       next_due_date=(today - timedelta(days=15)).isoformat())
        self.make_tool(name="OK Tool", calibration_interval_days=365,
                       next_due_date=(today + timedelta(days=335)).isoformat())
        dash = self.login("shop_admin").get("/shop").get_data(as_text=True)
        self.assertIn("Tools Due for Calibration", dash)
        self.assertIn("Overdue Tool", dash)
        self.assertNotIn("OK Tool", dash)

    def test_edit_tool_recomputes_next_due_from_new_interval(self):
        tid = self.make_tool(name="Torque Wrench D", calibration_interval_days=365,
                             last_calibrated_date="2026-01-01", next_due_date="2027-01-01")
        c = self.login("shop_admin")
        c.post(f"/shop/tools/{tid}/edit", data=dict(name="Torque Wrench D", calibration_interval_days="180"),
              follow_redirects=True)
        row = self.q1("SELECT * FROM shop_tools WHERE id = ?", (tid,))
        self.assertEqual(row["calibration_interval_days"], 180)
        self.assertEqual(row["next_due_date"], "2026-06-30")

    def test_delete_soft_deletes_and_hides_from_list(self):
        tid = self.make_tool(name="Old Gauge")
        c = self.login("shop_admin")
        c.post(f"/shop/tools/{tid}/delete", follow_redirects=True)
        self.assertIsNotNone(self.q1("SELECT deleted_at FROM shop_tools WHERE id = ?", (tid,))["deleted_at"])
        html = c.get("/shop/tools").get_data(as_text=True)
        self.assertNotIn("Old Gauge", html)
