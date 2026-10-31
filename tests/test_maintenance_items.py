"""Maintenance items on an aircraft: add, edit, complete, remove, and the
due / overdue maths for hours and calendar items.

Tests marked @open_finding("qa-...") describe the CORRECT behavior for a
problem reported on the Idea Queue page but not fixed yet.
"""
from datetime import datetime, timedelta

import db
from harness import OpsHubTestCase, open_finding


def _status(**kw):
    item = dict(type="hours", interval_hours=None, interval_days=None, last_done_hours=None,
                last_done_date=None, remind_lead=None)
    cur = kw.pop("current", None)
    item.update(kw)
    return db.maintenance_status(item, cur)


class DueMathTest(OpsHubTestCase):
    def test_hours_ok_due_soon_overdue(self):
        self.assertEqual(_status(interval_hours=100, last_done_hours=0, current=50)["urgency"], "ok")
        self.assertEqual(_status(interval_hours=100, last_done_hours=0, current=95)["urgency"], "due_soon")
        self.assertEqual(_status(interval_hours=100, last_done_hours=0, current=100)["urgency"], "overdue")
        self.assertEqual(_status(interval_hours=100, last_done_hours=0, current=130)["label"], "30 hrs overdue")

    def test_hours_next_due_is_last_plus_interval(self):
        s = _status(interval_hours=50, last_done_hours=1200.5, current=1210)
        self.assertEqual(s["next_due"], 1250.5)
        self.assertAlmostEqual(s["remaining"], 40.5)

    def test_reminder_lead_overrides_the_ten_percent_default(self):
        self.assertEqual(_status(interval_hours=100, last_done_hours=0, current=80, remind_lead=25)["urgency"], "due_soon")
        self.assertEqual(_status(interval_hours=100, last_done_hours=0, current=80, remind_lead=5)["urgency"], "ok")

    def test_no_reading_or_no_interval_is_unknown_not_a_crash(self):
        self.assertEqual(_status(interval_hours=100, last_done_hours=0, current=None)["urgency"], "unknown")
        self.assertEqual(_status(interval_hours=None, current=10)["urgency"], "unknown")
        self.assertEqual(_status(type="calendar", interval_days=None)["urgency"], "unknown")

    def test_calendar_items(self):
        today = datetime.now().date()
        done = lambda d: (today - timedelta(days=d)).isoformat()
        self.assertEqual(_status(type="calendar", interval_days=365, last_done_date=done(10))["urgency"], "ok")
        self.assertEqual(_status(type="calendar", interval_days=365, last_done_date=done(340))["urgency"], "due_soon")
        s = _status(type="calendar", interval_days=365, last_done_date=done(370))
        self.assertEqual(s["urgency"], "overdue")
        self.assertEqual(s["label"], "5 days overdue")
        self.assertEqual(_status(type="calendar", interval_days=365, last_done_date=done(364))["label"], "1 day remaining")

    def test_calendar_junk_last_done_date_does_not_crash(self):
        self.assertIn(_status(type="calendar", interval_days=30, last_done_date="garbage")["urgency"],
                      ("ok", "due_soon", "overdue"))


class MaintenanceFormsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.aid = self.make_asset("N555")
        self.exec("UPDATE assets SET tach_hours=100, hobbs_hours=120 WHERE id=?", (self.aid,))

    def new(self, role="shop_admin", **kw):
        self.login(role)
        data = dict(name="Oil change", type="hours", category="oil_change", hour_type="tach", interval_hours="50")
        data.update(kw)
        return self.client.post(f"/assets/{self.aid}/maintenance/new", data=data)

    def items(self):
        return self.q("SELECT * FROM maintenance_items WHERE asset_id=? AND name != 'Annual inspection'", (self.aid,))

    # --- add ---------------------------------------------------------
    def test_add_hours_item_defaults_last_done_to_current_reading(self):
        r = self.new()
        self.assertEqual(r.status_code, 302)
        it = self.items()[0]
        self.assertEqual(it["last_done_hours"], 100)
        self.assertEqual(it["active"], 1)

    def test_add_uses_hobbs_reading_when_hobbs_item(self):
        self.new(hour_type="hobbs")
        self.assertEqual(self.items()[0]["last_done_hours"], 120)

    def test_add_calendar_item_defaults_last_done_to_today(self):
        self.new(type="calendar", category="annual", name="Annual", interval_hours="", interval_days="365")
        it = self.items()[0]
        self.assertEqual(it["last_done_date"], db.now_iso()[:10])

    def test_blank_name_is_refused(self):
        self.new(name="   ")
        self.assertEqual(len(self.items()), 0)

    def test_bad_numbers_are_refused(self):
        for bad in ("-5", "0", "nan", "inf", "abc"):
            self.new(interval_hours=bad)
        self.assertEqual(len(self.items()), 0)

    def test_negative_or_zero_days_are_refused(self):
        for bad in ("-30", "0", "nan"):
            self.new(type="calendar", name="Cal", interval_hours="", interval_days=bad)
        self.assertEqual(len(self.items()), 0)

    def test_odd_type_and_category_fall_back_to_safe_values(self):
        self.new(type="bogus", category="bogus", hour_type="bogus")
        it = self.items()[0]
        self.assertEqual((it["type"], it["category"], it["hour_type"]), ("hours", "scheduled_maint", "tach"))

    def test_missing_aircraft_is_404(self):
        self.login("shop_admin")
        self.assertEqual(self.client.post("/assets/99999/maintenance/new", data=dict(name="x")).status_code, 404)
        self.assertEqual(self.client.get("/assets/99999/maintenance/new").status_code, 404)

    def test_roles(self):
        for role in ("tech", "shop_admin"):
            self.assertEqual(self.new(role, name="by " + role).status_code, 302)
        before = len(self.items())
        for role in ("inspector", "flight_student", "customer", None):
            try:
                r = self.new(role, name="nope " + str(role))
            except KeyError:
                continue  # role not in this harness
            self.assertIn(r.status_code, (302, 401, 403), role)
        names = [i["name"] for i in self.items()]
        self.assertNotIn("nope flight_student", names)
        self.assertNotIn("nope customer", names)
        self.assertNotIn("nope None", names)
        self.assertGreaterEqual(len(names), before)

    # --- edit --------------------------------------------------------
    def test_edit_changes_fields_but_not_last_done(self):
        self.new()
        it = self.items()[0]
        self.login("shop_admin")
        r = self.client.post(f"/maintenance/{it['id']}/edit",
                             data=dict(name="Oil + filter", type="hours", category="oil_change",
                                       hour_type="tach", interval_hours="25", last_done_hours="9999"))
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM maintenance_items WHERE id=?", (it["id"],))
        self.assertEqual((row["name"], row["interval_hours"], row["last_done_hours"]), ("Oil + filter", 25, 100))

    def test_edit_rejects_blank_name_and_bad_interval(self):
        self.new()
        it = self.items()[0]
        self.login("shop_admin")
        self.client.post(f"/maintenance/{it['id']}/edit", data=dict(name="", type="hours", interval_hours="50"))
        self.client.post(f"/maintenance/{it['id']}/edit", data=dict(name="Ok", type="hours", interval_hours="-1"))
        row = self.q1("SELECT * FROM maintenance_items WHERE id=?", (it["id"],))
        self.assertEqual((row["name"], row["interval_hours"]), ("Oil change", 50))

    def test_edit_missing_item_is_404_and_removed_item_redirects(self):
        self.login("shop_admin")
        self.assertEqual(self.client.get("/maintenance/99999/edit").status_code, 404)
        self.assertEqual(self.client.post("/maintenance/99999/edit", data=dict(name="x")).status_code, 404)
        self.new()
        it = self.items()[0]
        self.exec("UPDATE maintenance_items SET active=0 WHERE id=?", (it["id"],))
        r = self.client.post(f"/maintenance/{it['id']}/edit", data=dict(name="Zombie", type="hours", interval_hours="5"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.q1("SELECT name FROM maintenance_items WHERE id=?", (it["id"],))["name"], "Oil change")

    # --- complete ----------------------------------------------------
    def complete(self, item_id, **kw):
        self.login("tech")
        data = dict(performed_by="Frank"); data.update(kw)
        return self.client.post(f"/maintenance/{item_id}/complete", data=data)

    def test_complete_needs_a_name(self):
        self.new()
        it = self.items()[0]
        self.complete(it["id"], performed_by="")
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM maintenance_log WHERE item_id=?", (it["id"],))["c"], 0)

    def test_complete_hours_item_records_hours_and_raises_the_meter(self):
        self.new()
        it = self.items()[0]
        self.complete(it["id"], completed_hours="130")
        row = self.q1("SELECT * FROM maintenance_items WHERE id=?", (it["id"],))
        self.assertEqual(row["last_done_hours"], 130)
        self.assertEqual(self.q1("SELECT tach_hours FROM assets WHERE id=?", (self.aid,))["tach_hours"], 130)
        log = self.q1("SELECT * FROM maintenance_log WHERE item_id=?", (it["id"],))
        self.assertEqual((log["completed_hours"], log["performed_by"]), (130, "Frank"))

    def test_complete_with_lower_reading_does_not_lower_the_meter(self):
        self.new()
        it = self.items()[0]
        self.complete(it["id"], completed_hours="40")
        self.assertEqual(self.q1("SELECT tach_hours FROM assets WHERE id=?", (self.aid,))["tach_hours"], 100)

    def test_complete_with_junk_hours_uses_current_reading(self):
        self.new()
        it = self.items()[0]
        for bad in ("nan", "inf", "-7", "abc", ""):
            self.complete(it["id"], completed_hours=bad)
        rows = self.q("SELECT completed_hours FROM maintenance_log WHERE item_id=?", (it["id"],))
        self.assertEqual({r["completed_hours"] for r in rows}, {100})
        self.assertEqual(self.q1("SELECT last_done_hours FROM maintenance_items WHERE id=?", (it["id"],))["last_done_hours"], 100)

    def test_complete_calendar_item_sets_today(self):
        self.new(type="calendar", category="annual", name="Annual", interval_hours="", interval_days="365")
        it = self.items()[0]
        self.exec("UPDATE maintenance_items SET last_done_date='2000-01-01' WHERE id=?", (it["id"],))
        self.complete(it["id"])
        self.assertEqual(self.q1("SELECT last_done_date FROM maintenance_items WHERE id=?", (it["id"],))["last_done_date"],
                         db.now_iso()[:10])

    def test_complete_missing_or_removed_item(self):
        self.assertEqual(self.complete(99999).status_code, 404)
        self.new()
        it = self.items()[0]
        self.exec("UPDATE maintenance_items SET active=0 WHERE id=?", (it["id"],))
        self.complete(it["id"], completed_hours="500")
        self.assertEqual(self.q1("SELECT COUNT(*) c FROM maintenance_log WHERE item_id=?", (it["id"],))["c"], 0)
        self.assertEqual(self.q1("SELECT tach_hours FROM assets WHERE id=?", (self.aid,))["tach_hours"], 100)

    def test_completing_clears_overdue(self):
        self.new(interval_hours="10")
        it = self.items()[0]
        self.exec("UPDATE assets SET tach_hours=125 WHERE id=?", (self.aid,))
        row = self.q1("SELECT * FROM maintenance_items WHERE id=?", (it["id"],))
        self.assertEqual(db.maintenance_status(row, 125)["urgency"], "overdue")
        self.complete(it["id"])
        row = self.q1("SELECT * FROM maintenance_items WHERE id=?", (it["id"],))
        self.assertEqual(db.maintenance_status(row, 125)["urgency"], "ok")

    # --- delete ------------------------------------------------------
    def test_delete_is_admin_only_and_soft(self):
        self.new()
        it = self.items()[0]
        self.login("tech")
        self.assertIn(self.client.post(f"/maintenance/{it['id']}/delete").status_code, (302, 403))
        self.assertEqual(self.q1("SELECT active FROM maintenance_items WHERE id=?", (it["id"],))["active"], 1)
        self.login("shop_admin")
        self.client.post(f"/maintenance/{it['id']}/delete")
        self.assertEqual(self.q1("SELECT active FROM maintenance_items WHERE id=?", (it["id"],))["active"], 0)
        self.assertEqual(self.client.post("/maintenance/99999/delete").status_code, 404)

    def test_aircraft_page_loads_with_every_kind_of_item(self):
        self.new(name="A", interval_hours="1")
        self.new(name="B", type="calendar", interval_hours="", interval_days="30")
        self.exec("UPDATE maintenance_items SET last_done_date='not-a-date' WHERE name='B'")
        self.login("shop_admin")
        self.assertEqual(self.client.get(f"/assets/{self.aid}").status_code, 200)
