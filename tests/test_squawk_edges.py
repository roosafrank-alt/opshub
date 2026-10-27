"""Squawk edge cases: steps on records that don't exist, reopen rules, and
estimates with odd numbers (NaN, infinity, negative)."""
from test_squawks import SquawkBase
import db

STEPS = ("acknowledge", "assign", "estimate", "worker_ack", "repair", "repair_confirm", "reopen")


class MissingRecordTest(SquawkBase):
    def test_every_step_on_a_missing_squawk_is_not_a_server_error(self):
        self.login("shop_admin")
        for kind in ("quick", "flight"):
            for step in STEPS:
                with self.subTest(kind=kind, step=step):
                    r = self.client.post(f"/squawks/{kind}/99999/{step}",
                                         data=dict(assigned_to="1", hours="1", parts="0"))
                    self.assertLess(r.status_code, 500)

    def test_unknown_kind_is_404_on_every_step(self):
        self.login("shop_admin")
        for step in STEPS:
            with self.subTest(step=step):
                r = self.client.post(f"/squawks/bogus/{self.quick}/{step}")
                self.assertEqual(r.status_code, 404)


class ReopenTest(SquawkBase):
    def test_open_squawk_cannot_be_reopened(self):
        self.login("shop_admin")
        self.post("quick", "reopen")
        self.assertIsNone(self.sq("quick")["reopened_at"])

    def test_signed_off_squawk_reopens_cleanly_once(self):
        self.login("shop_admin")
        conn = db.get_db()
        conn.execute("UPDATE plane_squawks SET acknowledged_at=?, repaired_at=?, repaired_by='Tech' WHERE id=?",
                     (db.now_iso(), db.now_iso(), self.quick))
        conn.commit()
        conn.close()
        self.post("quick", "reopen")
        s = self.sq("quick")
        self.assertIsNone(s["repaired_at"])
        self.assertIsNone(s["acknowledged_at"])
        self.assertTrue(s["reopened_at"])

    def test_student_cannot_reopen(self):
        conn = db.get_db()
        conn.execute("UPDATE plane_squawks SET repaired_at=? WHERE id=?", (db.now_iso(), self.quick))
        conn.commit()
        conn.close()
        self.login("flight_student")
        self.post("quick", "reopen")
        self.assertTrue(self.sq("quick")["repaired_at"])


class EstimateNumbersTest(SquawkBase):
    def test_odd_numbers_are_never_saved(self):
        self.login("shop_admin")
        for bad in ("nan", "inf", "-inf", "-5", "1e999", "abc"):
            with self.subTest(hours=bad):
                self.post("quick", "estimate", hours=bad, parts="10")
                h = self.sq("quick")["est_hours"]
                self.assertTrue(h is None or (h == h and 0 <= h < 1e6), h)
