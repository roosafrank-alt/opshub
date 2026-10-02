"""Aircraft (assets): new, quick new, edit, hours update, trash / restore / purge.

Tests marked @open_finding("qa-...") describe the CORRECT behavior for a
problem reported on the Idea Queue page but not fixed yet.
"""
import math

from harness import OpsHubTestCase, seed_row, open_finding
import db


class NewAircraftTest(OpsHubTestCase):
    def test_admin_can_add_an_aircraft(self):
        self.login("shop_admin")
        r = self.client.post("/assets/new", data=dict(tag="N777", make="Cessna", hobbs_hours="10.5"))
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM assets WHERE tag='N777'")
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "N777")
        self.assertEqual(row["hobbs_hours"], 10.5)

    def test_blank_tag_rejected(self):
        self.login("shop_admin")
        before = self.q1("SELECT COUNT(*) n FROM assets")["n"]
        r = self.client.post("/assets/new", data=dict(tag="   "))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM assets")["n"], before)

    def test_duplicate_tag_rejected(self):
        self.make_asset("N777")
        self.login("shop_admin")
        r = self.client.post("/assets/new", data=dict(tag="N777"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM assets WHERE tag='N777'")["n"], 1)

    def test_only_admin_can_add(self):
        for role in ("tech", "inspector", "shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            self.client.post("/assets/new", data=dict(tag="N555"))
            self.assertIsNone(self.q1("SELECT id FROM assets WHERE tag='N555'"), role)


class QuickNewTest(OpsHubTestCase):
    def test_tech_quick_adds_incomplete_profile(self):
        self.login("tech")
        r = self.client.post("/assets/quick_new", data=dict(tag=" N321 "))
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        row = self.q1("SELECT * FROM assets WHERE id=?", (body["id"],))
        self.assertEqual(row["tag"], "N321")
        self.assertEqual(row["profile_incomplete"], 1)

    def test_blank_and_duplicate_rejected_with_400(self):
        self.make_asset("N321")
        self.login("tech")
        self.assertEqual(self.client.post("/assets/quick_new", data=dict(tag="")).status_code, 400)
        self.assertEqual(self.client.post("/assets/quick_new", data=dict(tag="N321")).status_code, 400)
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM assets")["n"], 1)

    def test_other_roles_cannot(self):
        for role in ("inspector", "shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            self.client.post("/assets/quick_new", data=dict(tag="N999"))
            self.assertIsNone(self.q1("SELECT id FROM assets WHERE tag='N999'"), role)

    def test_edit_clears_incomplete_flag(self):
        self.login("shop_admin")
        aid = self.client.post("/assets/quick_new", data=dict(tag="N321")).get_json()["id"]
        self.client.post(f"/assets/{aid}/edit", data=dict(tag="N321", make="Piper"))
        row = self.q1("SELECT * FROM assets WHERE id=?", (aid,))
        self.assertEqual(row["profile_incomplete"], 0)
        self.assertEqual(row["make"], "Piper")


class EditAircraftTest(OpsHubTestCase):
    def test_rename_to_existing_tag_or_blank_rejected(self):
        a = self.make_asset("N1")
        self.make_asset("N2")
        self.login("shop_admin")
        self.client.post(f"/assets/{a}/edit", data=dict(tag="N2"))
        self.client.post(f"/assets/{a}/edit", data=dict(tag=" "))
        self.assertEqual(self.q1("SELECT tag FROM assets WHERE id=?", (a,))["tag"], "N1")

    def test_edit_keeps_rental_rate_and_hours(self):
        a = self.make_asset("N1")
        self.exec("UPDATE assets SET rental_rate=125, hobbs_hours=42 WHERE id=?", (a,))
        self.login("shop_admin")
        self.client.post(f"/assets/{a}/edit", data=dict(tag="N1", make="Cessna"))
        row = self.q1("SELECT * FROM assets WHERE id=?", (a,))
        self.assertEqual(row["rental_rate"], 125)
        self.assertEqual(row["hobbs_hours"], 42)

    def test_missing_aircraft_is_404(self):
        self.login("shop_admin")
        self.assertEqual(self.client.get("/assets/999/edit").status_code, 404)
        self.assertEqual(self.client.post("/assets/999/edit", data=dict(tag="X")).status_code, 404)
        self.assertEqual(self.client.get("/assets/999").status_code, 404)

    def test_tech_cannot_edit(self):
        a = self.make_asset("N1")
        self.login("tech")
        self.client.post(f"/assets/{a}/edit", data=dict(tag="N1", make="Hacked"))
        self.assertNotEqual(self.q1("SELECT make FROM assets WHERE id=?", (a,))["make"], "Hacked")


class HoursTest(OpsHubTestCase):
    def hours(self, a):
        return self.q1("SELECT hobbs_hours h, tach_hours t FROM assets WHERE id=?", (a,))

    def test_tech_updates_hobbs_and_tach(self):
        a = self.make_asset("N1")
        self.login("tech")
        r = self.client.post(f"/assets/{a}/update_hours", data=dict(hobbs_hours="100.5", tach_hours="90"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual((self.hours(a)["h"], self.hours(a)["t"]), (100.5, 90))

    def test_blank_or_text_changes_nothing(self):
        a = self.make_asset("N1")
        self.exec("UPDATE assets SET hobbs_hours=10 WHERE id=?", (a,))
        self.login("tech")
        self.client.post(f"/assets/{a}/update_hours", data=dict(hobbs_hours="", tach_hours=""))
        self.client.post(f"/assets/{a}/update_hours", data=dict(hobbs_hours="abc"))
        self.assertEqual(self.hours(a)["h"], 10)

    def test_missing_aircraft_is_404_and_other_roles_blocked(self):
        a = self.make_asset("N1")
        self.login("tech")
        self.assertEqual(self.client.post("/assets/999/update_hours", data=dict(hobbs_hours="5")).status_code, 404)
        for role in ("inspector", "shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            self.client.post(f"/assets/{a}/update_hours", data=dict(hobbs_hours="5"))
            self.assertIsNone(self.hours(a)["h"], role)

    def test_negative_and_infinite_readings_rejected(self):
        a = self.make_asset("N1")
        self.exec("UPDATE assets SET hobbs_hours=10, tach_hours=10 WHERE id=?", (a,))
        self.login("tech")
        for bad in ("-5", "inf", "nan", "1e999"):
            self.client.post(f"/assets/{a}/update_hours", data=dict(hobbs_hours=bad, tach_hours=bad))
            h = self.hours(a)
            for v in (h["h"], h["t"]):
                self.assertTrue(v is not None and math.isfinite(v) and v >= 0, f"{bad!r} stored as {v!r}")


class TrashRestorePurgeTest(OpsHubTestCase):
    def test_trash_restore_roundtrip(self):
        a = self.make_asset("N1")
        self.login("shop_admin")
        self.client.post(f"/assets/{a}/trash")
        self.assertIsNotNone(self.q1("SELECT deleted_at FROM assets WHERE id=?", (a,))["deleted_at"])
        self.client.post(f"/assets/{a}/restore")
        self.assertIsNone(self.q1("SELECT deleted_at FROM assets WHERE id=?", (a,))["deleted_at"])

    def test_missing_is_404(self):
        self.login("shop_admin")
        for step in ("trash", "restore", "purge"):
            self.assertEqual(self.client.post(f"/assets/999/{step}").status_code, 404, step)

    def test_only_admin(self):
        a = self.make_asset("N1")
        for role in ("tech", "inspector", "shop_student", "cfi", "no_roles"):
            self.login(role)
            self.client.post(f"/assets/{a}/trash")
            self.client.post(f"/assets/{a}/purge")
            row = self.q1("SELECT deleted_at FROM assets WHERE id=?", (a,))
            self.assertIsNotNone(row, role)
            self.assertIsNone(row["deleted_at"], role)

    def test_purge_keeps_projects_unlinked(self):
        a = self.make_asset("N1")
        p = self.make_project(asset_id=a)
        self.login("shop_admin")
        self.client.post(f"/assets/{a}/purge")
        self.assertIsNone(self.q1("SELECT id FROM assets WHERE id=?", (a,)))
        self.assertIsNone(self.q1("SELECT asset_id FROM projects WHERE id=?", (p,))["asset_id"])

    def test_purge_aircraft_with_squawk_or_flight_history(self):
        for kind in ("squawk", "flight"):
            a = self.make_asset("N-" + kind)
            conn = db.get_db()
            if kind == "squawk":
                seed_row(conn, "plane_squawks", asset_id=a, notes="Soft brake", reported_by="F",
                         reported_at=db.now_iso())
            else:
                stu = conn.execute("SELECT id FROM students WHERE user_id=?",
                                   (self.users["flight_student"]["id"],)).fetchone()["id"]
                seed_row(conn, "flights", asset_id=a, student_id=stu, flight_date=db.now_iso()[:10])
            conn.commit()
            conn.close()
            self.login("shop_admin")
            r = self.client.post(f"/assets/{a}/purge")
            self.assertLess(r.status_code, 400, kind)
            self.assertIsNone(self.q1("SELECT id FROM assets WHERE id=?", (a,)), kind)
