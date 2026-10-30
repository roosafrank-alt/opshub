"""Aircraft (assets) part 2: odd numbers on the New Aircraft form, look-alike
tail numbers, and what a trashed aircraft still allows.

Tests marked @open_finding("qa-...") describe the CORRECT behavior for a
problem reported on the Idea Queue page but not fixed yet.
"""
import math

from harness import OpsHubTestCase, open_finding


class NewAircraftNumbersTest(OpsHubTestCase):
    def post_new(self, **kw):
        self.login("shop_admin")
        data = dict(tag="N900"); data.update(kw)
        return self.client.post("/assets/new", data=data)

    def row(self):
        return self.q1("SELECT * FROM assets WHERE tag='N900'")

    def test_text_hours_are_ignored_not_a_crash(self):
        r = self.post_new(hobbs_hours="abc", tach_hours="xyz")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.row()["hobbs_hours"])

    @open_finding("qa-new-aircraft-bad-hours-saved")
    def test_nan_infinite_or_negative_hours_are_not_saved(self):
        for bad in ("nan", "inf", "-5"):
            self.q1("SELECT 1")
            self.exec("DELETE FROM assets WHERE tag='N900'")
            self.post_new(hobbs_hours=bad, tach_hours=bad)
            r = self.row()
            if r is None:
                continue  # refused outright is fine
            for col in ("hobbs_hours", "tach_hours"):
                v = r[col]
                self.assertTrue(v is None or (math.isfinite(v) and v >= 0), (bad, col, v))

    @open_finding("qa-new-aircraft-bad-hours-saved")
    def test_nan_or_negative_rental_rate_is_not_saved(self):
        for bad in ("nan", "inf", "-100"):
            self.exec("DELETE FROM assets WHERE tag='N900'")
            self.post_new(rental_rate=bad)
            r = self.row()
            if r is None:
                continue
            v = r["rental_rate"]
            self.assertTrue(v is None or (math.isfinite(v) and v >= 0), (bad, v))

    def test_tail_number_is_trimmed(self):
        self.post_new(tag="  N901  ")
        self.assertIsNotNone(self.q1("SELECT 1 FROM assets WHERE tag='N901'"))

    @open_finding("qa-aircraft-tail-case-duplicate")
    def test_same_tail_in_different_case_is_a_duplicate(self):
        self.make_asset("N777")
        self.login("shop_admin")
        self.client.post("/assets/new", data=dict(tag="n777"))
        n = self.q1("SELECT COUNT(*) n FROM assets WHERE UPPER(tag)='N777'")["n"]
        self.assertEqual(n, 1)

    def test_trashed_aircraft_still_blocks_its_tail_number(self):
        aid = self.make_asset("N555")
        self.exec("UPDATE assets SET deleted_at='2026-01-01T00:00:00' WHERE id=?", (aid,))
        self.post_new(tag="N555")
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM assets WHERE tag='N555'")["n"], 1)


class HoursUpdateTest(OpsHubTestCase):
    def test_hours_update_stamps_who_and_when(self):
        aid = self.make_asset("N12345")
        self.login("tech")
        self.client.post(f"/assets/{aid}/update_hours", data=dict(hobbs_hours="100.5"))
        r = self.q1("SELECT * FROM assets WHERE id=?", (aid,))
        self.assertEqual(r["hobbs_hours"], 100.5)
        self.assertTrue(r["hobbs_updated_at"])
        self.assertIn("Shop", r["hobbs_updated_by"] or "")

    def test_updating_only_tach_leaves_hobbs_alone(self):
        aid = self.make_asset("N12345")
        self.exec("UPDATE assets SET hobbs_hours=50 WHERE id=?", (aid,))
        self.login("tech")
        self.client.post(f"/assets/{aid}/update_hours", data=dict(tach_hours="60"))
        r = self.q1("SELECT * FROM assets WHERE id=?", (aid,))
        self.assertEqual(r["hobbs_hours"], 50)
        self.assertEqual(r["tach_hours"], 60)

    def test_one_bad_reading_saves_neither(self):
        aid = self.make_asset("N12345")
        self.exec("UPDATE assets SET hobbs_hours=50, tach_hours=40 WHERE id=?", (aid,))
        self.login("tech")
        self.client.post(f"/assets/{aid}/update_hours", data=dict(hobbs_hours="70", tach_hours="-1"))
        r = self.q1("SELECT * FROM assets WHERE id=?", (aid,))
        self.assertEqual((r["hobbs_hours"], r["tach_hours"]), (50, 40))


class TrashedAircraftTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.aid = self.make_asset("N321")
        self.exec("UPDATE assets SET deleted_at='2026-01-01T00:00:00' WHERE id=?", (self.aid,))

    def test_trashed_aircraft_not_in_fleet_list(self):
        self.login("shop_admin")
        self.assertNotIn(b"N321", self.client.get("/assets").data)

    def test_trashed_aircraft_listed_in_recently_deleted(self):
        self.login("shop_admin")
        self.assertIn(b"N321", self.client.get("/trash").data)

    def test_trashing_twice_and_restoring_a_live_one_do_not_crash(self):
        self.login("shop_admin")
        for _ in range(2):
            self.assertLess(self.client.post(f"/assets/{self.aid}/trash").status_code, 500)
        live = self.make_asset("N322")
        self.assertLess(self.client.post(f"/assets/{live}/restore").status_code, 500)
        self.assertIsNone(self.q1("SELECT deleted_at d FROM assets WHERE id=?", (live,))["d"])
