"""Admin Reset pages, second pass: Reset Planes / Students / Instructors with a
record in EVERY table that points at a plane, student or instructor (found from
the database itself, so a table added later is covered automatically), plus
Reset Schedule / Flights not touching shop-side data, and double clicks."""
import db
from harness import OpsHubTestCase, open_finding


def _dummy(col_type):
    t = (col_type or "").upper()
    if "INT" in t or "REAL" in t or "NUM" in t:
        return 1
    return "x"


class ResetEveryLinkTest(OpsHubTestCase):
    def _linked(self, target):
        conn = db.get_db()
        out = []
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for fk in conn.execute(f"PRAGMA foreign_key_list('{name}')").fetchall():
                if fk["table"] == target and fk["from"] and name != target:
                    out.append((name, fk["from"]))
        conn.close()
        return out

    def _link_row(self, table, col, ident):
        conn = db.get_db()
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        names, vals = [col], [ident]
        for c in cols:
            if c["name"] == col or c["pk"]:
                continue
            if c["notnull"] and c["dflt_value"] is None:
                names.append(c["name"]); vals.append(_dummy(c["type"]))
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' * len(names))})", vals)
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False
        finally:
            conn.close()

    def _reset(self, what):
        c = self.login("master")
        html = c.post(f"/admin/reset/{what}", data={"confirm_text": "RESET"},
                      follow_redirects=True).get_data(as_text=True)
        self.assertNotIn("Reset didn't run", html)
        self.assertNotIn("FOREIGN KEY constraint failed", html)

    @open_finding("qa-reset-planes-linked-records")
    def test_reset_planes_with_a_record_in_every_linked_table(self):
        aid = self.make_asset("N7001R")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (aid,))
        shop = self.make_asset("N7002S")
        for t, c in self._linked("assets"):
            self._link_row(t, c, aid)
        self._reset("planes")
        self.assertIsNone(self.q1("SELECT 1 FROM assets WHERE id = ?", (aid,)))
        self.assertIsNotNone(self.q1("SELECT 1 FROM assets WHERE id = ?", (shop,)), "shop-only aircraft must survive")

    @open_finding("qa-reset-students-linked-records")
    def test_reset_students_with_a_record_in_every_linked_table(self):
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        for t, c in self._linked("students"):
            self._link_row(t, c, sid)
        self._reset("students")
        self.assertIsNone(self.q1("SELECT 1 FROM students WHERE id = ?", (sid,)))

    @open_finding("qa-reset-instructors-linked-records")
    def test_reset_instructors_with_a_record_in_every_linked_table(self):
        cid = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        for t, c in self._linked("cfis"):
            self._link_row(t, c, cid)
        self._reset("instructors")
        self.assertIsNone(self.q1("SELECT 1 FROM cfis WHERE id = ?", (cid,)))

    def test_reset_twice_in_a_row_is_harmless(self):
        for what in ("planes", "students", "instructors", "billing", "schedule", "flights", "flight_school"):
            self._reset(what)
            self._reset(what)

    def test_reset_schedule_and_flights_keep_shop_side_data(self):
        part = self.make_part()
        asset = self.make_asset("N7003T")
        proj = self.make_project("Keep me", asset_id=asset)
        before = (self.q1("SELECT COUNT(*) c FROM parts")["c"], self.q1("SELECT COUNT(*) c FROM projects")["c"],
                  self.q1("SELECT COUNT(*) c FROM assets")["c"])
        c = self.login("master")
        c.post("/admin/reset/schedule")
        c.post("/admin/reset/flights")
        after = (self.q1("SELECT COUNT(*) c FROM parts")["c"], self.q1("SELECT COUNT(*) c FROM projects")["c"],
                 self.q1("SELECT COUNT(*) c FROM assets")["c"])
        self.assertEqual(before, after)

    def test_billing_reset_wrong_confirm_words_change_nothing(self):
        sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE students SET balance = 77 WHERE id = ?", (sid,))
        c = self.login("master")
        for word in ("", "reset it", "RESETT", " ", "no"):
            c.post("/admin/reset/billing", data={"confirm_text": word})
            self.assertEqual(self.q1("SELECT balance FROM students WHERE id = ?", (sid,))["balance"], 77, word)
