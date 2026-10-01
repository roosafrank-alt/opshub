"""Maintenance items: new, edit, complete, delete, and the due / overdue math.

Tests marked @open_finding("qa-...") describe the CORRECT behavior for a
problem reported on the Idea Queue page but not fixed yet.
"""
import math
from datetime import datetime, timedelta

from harness import OpsHubTestCase, seed_row, open_finding
import db


def item(**kw):
    base = dict(type="hours", interval_hours=100, interval_days=None, last_done_hours=None,
                last_done_date=None, remind_lead=None)
    base.update(kw)
    return base


def days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


class DueMathTest(OpsHubTestCase):
    def test_hours_ok_due_soon_overdue(self):
        self.assertEqual(db.maintenance_status(item(last_done_hours=100), 120)["urgency"], "ok")
        s = db.maintenance_status(item(last_done_hours=100), 195)
        self.assertEqual((s["urgency"], s["remaining"], s["next_due"]), ("due_soon", 5, 200))
        s = db.maintenance_status(item(last_done_hours=100), 200)
        self.assertEqual((s["urgency"], s["remaining"]), ("overdue", 0))
        s = db.maintenance_status(item(last_done_hours=100), 230.5)
        self.assertEqual(s["urgency"], "overdue")
        self.assertIn("30.5", s["label"])

    def test_remind_lead_overrides_ten_percent(self):
        self.assertEqual(db.maintenance_status(item(last_done_hours=0, remind_lead=25), 80)["urgency"], "due_soon")
        self.assertEqual(db.maintenance_status(item(last_done_hours=0, remind_lead=0), 99)["urgency"], "ok")

    def test_never_done_counts_from_zero(self):
        self.assertEqual(db.maintenance_status(item(), 150)["urgency"], "overdue")

    def test_unknown_when_nothing_to_compute_from(self):
        self.assertEqual(db.maintenance_status(item(interval_hours=None), 50)["urgency"], "unknown")
        self.assertEqual(db.maintenance_status(item(interval_hours=0), 50)["urgency"], "unknown")
        self.assertEqual(db.maintenance_status(item(), None)["urgency"], "unknown")
        self.assertEqual(db.maintenance_status(item(type="calendar", interval_days=None), None)["urgency"], "unknown")

    def test_calendar_ok_due_soon_overdue(self):
        cal = dict(type="calendar", interval_days=365)
        self.assertEqual(db.maintenance_status(item(last_done_date=days_ago(10), **cal), None)["urgency"], "ok")
        self.assertEqual(db.maintenance_status(item(last_done_date=days_ago(340), **cal), None)["urgency"], "due_soon")
        s = db.maintenance_status(item(last_done_date=days_ago(366), **cal), None)
        self.assertEqual((s["urgency"], s["remaining"]), ("overdue", -1))
        self.assertEqual(s["label"], "1 day overdue")
        s = db.maintenance_status(item(last_done_date=days_ago(365), **cal), None)
        self.assertEqual((s["urgency"], s["remaining"]), ("overdue", 0))

    def test_calendar_bad_date_does_not_crash(self):
        s = db.maintenance_status(item(type="calendar", interval_days=30, last_done_date="garbage"), None)
        self.assertIn(s["urgency"], ("ok", "due_soon", "unknown"))


class NewItemTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.make_asset("N1")
        self.exec("UPDATE assets SET tach_hours=200, hobbs_hours=230 WHERE id=?", (self.a,))

    def post_new(self, **data):
        data.setdefault("name", "100 hour")
        return self.client.post(f"/assets/{self.a}/maintenance/new", data=data)

    def last(self):
        return self.q1("SELECT * FROM maintenance_items WHERE asset_id=? ORDER BY id DESC", (self.a,))

    def test_hours_item_defaults_to_current_reading(self):
        self.login("shop_admin")
        r = self.post_new(type="hours", interval_hours="100", hour_type="tach", category="100hour")
        self.assertEqual(r.status_code, 302)
        row = self.last()
        self.assertEqual((row["last_done_hours"], row["interval_hours"], row["category"]), (200, 100, "100hour"))
        self.post_new(name="Hobbs one", type="hours", interval_hours="50", hour_type="hobbs")
        self.assertEqual(self.last()["last_done_hours"], 230)

    def test_calendar_item_defaults_last_done_to_today(self):
        self.login("shop_admin")
        self.post_new(name="Annual", type="calendar", interval_days="365", category="annual")
        self.assertEqual(self.last()["last_done_date"], db.now_iso()[:10])

    def test_blank_name_rejected(self):
        self.login("shop_admin")
        r = self.post_new(name="   ")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.last())

    def test_unknown_type_and_category_fall_back(self):
        self.login("shop_admin")
        self.post_new(type="bogus", category="bogus", hour_type="bogus", interval_hours="10")
        row = self.last()
        self.assertEqual((row["type"], row["category"], row["hour_type"]), ("hours", "scheduled_maint", "tach"))

    def test_missing_aircraft_404(self):
        self.login("shop_admin")
        self.assertEqual(self.client.post("/assets/999/maintenance/new", data=dict(name="x")).status_code, 404)
        self.assertEqual(self.client.get("/assets/999/maintenance/new").status_code, 404)

    def test_only_admin_can_add_edit_delete(self):
        self.login("shop_admin")
        self.post_new()
        iid = self.last()["id"]
        for role in ("tech", "inspector", "shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            self.post_new(name="Sneaky " + role, interval_hours="10")
            self.client.post(f"/maintenance/{iid}/edit", data=dict(name="Hacked"))
            self.client.post(f"/maintenance/{iid}/delete")
            self.assertEqual(self.q1("SELECT COUNT(*) n FROM maintenance_items")["n"], 1, role)
            row = self.q1("SELECT name, active FROM maintenance_items WHERE id=?", (iid,))
            self.assertEqual((row["name"], row["active"]), ("100 hour", 1), role)

    def test_nan_negative_and_infinite_intervals_rejected(self):
        self.login("shop_admin")
        for bad in ("nan", "inf", "-100", "1e999"):
            self.post_new(name="Bad " + bad, type="hours", interval_hours=bad, last_done_hours=bad)
            row = self.q1("SELECT * FROM maintenance_items WHERE name=?", ("Bad " + bad,))
            if row is None:
                continue
            for col in ("interval_hours", "last_done_hours"):
                v = row[col]
                self.assertTrue(v is None or (math.isfinite(v) and v >= 0), f"{bad!r} stored in {col} as {v!r}")

    def test_zero_or_negative_day_interval_rejected(self):
        self.login("shop_admin")
        for bad in ("-30", "0"):
            self.post_new(name="Days " + bad, type="calendar", interval_days=bad)
            row = self.q1("SELECT * FROM maintenance_items WHERE name=?", ("Days " + bad,))
            if row is None:
                continue
            self.assertFalse(row["interval_days"] is not None and row["interval_days"] <= 0,
                             f"interval_days {bad!r} stored as {row['interval_days']!r}")


class EditDeleteTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.make_asset("N1")
        conn = db.get_db()
        self.iid = seed_row(conn, "maintenance_items", asset_id=self.a, name="Oil", type="hours", category="oil_change",
                            hour_type="tach", interval_hours=50, last_done_hours=10, active=1)
        conn.commit()
        conn.close()

    def test_edit_keeps_last_done_and_history(self):
        self.login("shop_admin")
        r = self.client.post(f"/maintenance/{self.iid}/edit",
                             data=dict(name="Oil + filter", type="hours", interval_hours="60", hour_type="tach"))
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM maintenance_items WHERE id=?", (self.iid,))
        self.assertEqual((row["name"], row["interval_hours"], row["last_done_hours"]), ("Oil + filter", 60, 10))

    def test_edit_blank_name_rejected(self):
        self.login("shop_admin")
        self.client.post(f"/maintenance/{self.iid}/edit", data=dict(name=" ", type="hours"))
        self.assertEqual(self.q1("SELECT name FROM maintenance_items WHERE id=?", (self.iid,))["name"], "Oil")

    def test_missing_item_404(self):
        self.login("shop_admin")
        self.assertEqual(self.client.get("/maintenance/999/edit").status_code, 404)
        self.assertEqual(self.client.post("/maintenance/999/edit", data=dict(name="x")).status_code, 404)
        self.assertEqual(self.client.post("/maintenance/999/delete").status_code, 404)
        self.assertEqual(self.client.post("/maintenance/999/complete", data=dict(performed_by="F")).status_code, 404)

    def test_delete_hides_item_but_keeps_row(self):
        self.login("shop_admin")
        self.client.post(f"/maintenance/{self.iid}/delete")
        self.assertEqual(self.q1("SELECT active FROM maintenance_items WHERE id=?", (self.iid,))["active"], 0)
        body = self.client.get(f"/assets/{self.a}").get_data(as_text=True)
        self.assertNotIn(">Oil<", body)

    @open_finding("qa-maint-deleted-item-still-editable")
    def test_removed_item_cannot_be_completed_or_edited(self):
        self.login("shop_admin")
        self.client.post(f"/maintenance/{self.iid}/delete")
        self.client.post(f"/maintenance/{self.iid}/complete", data=dict(performed_by="Frank"))
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM maintenance_log WHERE item_id=?", (self.iid,))["n"], 0)


class CompleteTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.make_asset("N1")
        self.exec("UPDATE assets SET tach_hours=120, hobbs_hours=140 WHERE id=?", (self.a,))
        conn = db.get_db()
        self.hrs = seed_row(conn, "maintenance_items", asset_id=self.a, name="100hr", type="hours", category="100hour",
                            hour_type="tach", interval_hours=100, last_done_hours=0, active=1)
        self.cal = seed_row(conn, "maintenance_items", asset_id=self.a, name="Annual", type="calendar",
                            category="annual", interval_days=365, last_done_date=days_ago(400), active=1)
        conn.commit()
        conn.close()

    def complete(self, iid, **data):
        return self.client.post(f"/maintenance/{iid}/complete", data=data)

    def row(self, iid):
        return self.q1("SELECT * FROM maintenance_items WHERE id=?", (iid,))

    def test_hours_complete_uses_current_reading_and_logs(self):
        self.login("tech")
        self.assertEqual(db.maintenance_status(self.row(self.hrs), 120)["urgency"], "overdue")
        r = self.complete(self.hrs, performed_by="Frank", note="done")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.row(self.hrs)["last_done_hours"], 120)
        log = self.q1("SELECT * FROM maintenance_log WHERE item_id=?", (self.hrs,))
        self.assertEqual((log["performed_by"], log["completed_hours"], log["note"]), ("Frank", 120, "done"))
        self.assertEqual(db.maintenance_status(self.row(self.hrs), 120)["urgency"], "ok")

    def test_completing_at_a_higher_reading_raises_the_aircraft_meter(self):
        self.login("tech")
        self.complete(self.hrs, performed_by="Frank", completed_hours="130.5")
        self.assertEqual(self.q1("SELECT tach_hours t FROM assets WHERE id=?", (self.a,))["t"], 130.5)
        self.assertEqual(self.row(self.hrs)["last_done_hours"], 130.5)

    def test_completing_at_a_lower_reading_leaves_the_meter_alone(self):
        self.login("tech")
        self.complete(self.hrs, performed_by="Frank", completed_hours="110")
        self.assertEqual(self.q1("SELECT tach_hours t FROM assets WHERE id=?", (self.a,))["t"], 120)

    def test_calendar_complete_resets_to_today(self):
        self.login("tech")
        self.assertEqual(db.maintenance_status(self.row(self.cal), None)["urgency"], "overdue")
        self.complete(self.cal, performed_by="Frank")
        self.assertEqual(self.row(self.cal)["last_done_date"], db.now_iso()[:10])
        self.assertEqual(db.maintenance_status(self.row(self.cal), None)["urgency"], "ok")

    def test_must_say_who_did_it(self):
        self.login("tech")
        self.complete(self.hrs, performed_by="  ")
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM maintenance_log")["n"], 0)
        self.assertEqual(self.row(self.hrs)["last_done_hours"], 0)

    def test_only_admin_and_tech(self):
        for role in ("inspector", "shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            self.complete(self.hrs, performed_by="X")
            self.assertEqual(self.q1("SELECT COUNT(*) n FROM maintenance_log")["n"], 0, role)

    def test_double_submit_logs_twice_but_state_is_stable(self):
        # Two quick taps are two real log lines today; the due state must still be sane.
        self.login("tech")
        self.complete(self.hrs, performed_by="Frank")
        self.complete(self.hrs, performed_by="Frank")
        self.assertEqual(self.row(self.hrs)["last_done_hours"], 120)

    def test_nan_or_negative_completion_reading_rejected(self):
        self.login("tech")
        for bad in ("nan", "inf", "-5"):
            self.complete(self.hrs, performed_by="Frank", completed_hours=bad)
            v = self.row(self.hrs)["last_done_hours"]
            self.assertTrue(v is not None and math.isfinite(v) and v >= 0, f"{bad!r} stored as {v!r}")


class FleetRemindersTest(OpsHubTestCase):
    def test_dashboard_shows_overdue_item_and_not_deleted_one(self):
        a = self.make_asset("N1")
        self.exec("UPDATE assets SET tach_hours=500 WHERE id=?", (a,))
        conn = db.get_db()
        seed_row(conn, "maintenance_items", asset_id=a, name="Overdue thing", type="hours", category="100hour",
                 hour_type="tach", interval_hours=100, last_done_hours=0, active=1)
        seed_row(conn, "maintenance_items", asset_id=a, name="Removed thing", type="hours", category="100hour",
                 hour_type="tach", interval_hours=100, last_done_hours=0, active=0)
        conn.commit()
        conn.close()
        import app as app_module
        conn = db.get_db()
        rem = app_module._fleet_maintenance_reminders(conn)
        conn.close()
        names = [r["item"]["name"] for r in rem]
        self.assertIn("Overdue thing", names)
        self.assertNotIn("Removed thing", names)

    def test_trashed_aircraft_items_not_in_reminders(self):
        a = self.make_asset("N1")
        self.exec("UPDATE assets SET tach_hours=500, deleted_at=? WHERE id=?", (db.now_iso(), a))
        conn = db.get_db()
        seed_row(conn, "maintenance_items", asset_id=a, name="Ghost item", type="hours", category="100hour",
                 hour_type="tach", interval_hours=100, last_done_hours=0, active=1)
        conn.commit()
        import app as app_module
        rem = app_module._fleet_maintenance_reminders(conn)
        conn.close()
        self.assertNotIn("Ghost item", [r["item"]["name"] for r in rem])
