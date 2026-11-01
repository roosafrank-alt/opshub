"""Recently Deleted, part 2: Delete Forever on an aircraft that has a record in
EVERY table that points at aircraft, and restore edge cases.

The first test discovers the linked tables from the database itself, so a table
added later is covered automatically.
"""
import db
from harness import OpsHubTestCase


def _dummy(col_type):
    t = (col_type or "").upper()
    if "INT" in t or "REAL" in t or "NUM" in t:
        return 1
    return "x"


class PurgeEveryLinkTest(OpsHubTestCase):
    def _linked_tables(self):
        conn = db.get_db()
        out = []
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for fk in conn.execute(f"PRAGMA foreign_key_list({name})").fetchall():
                if fk["table"] == "assets" and fk["from"]:
                    out.append((name, fk["from"]))
        conn.close()
        return out

    def _link_row(self, table, col, asset_id):
        conn = db.get_db()
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        names, vals = [col], [asset_id]
        for c in cols:
            if c["name"] == col or c["pk"]:
                continue
            if c["notnull"] and c["dflt_value"] is None:
                names.append(c["name"]); vals.append(_dummy(c["type"]))
        try:
            conn.execute("PRAGMA foreign_keys=OFF")  # only the link to the aircraft matters here
            conn.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' * len(names))})", vals)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_delete_forever_leaves_nothing_pointing_at_the_gone_aircraft(self):
        aid = self.make_asset("N4242")
        linked = [(t, c) for t, c in self._linked_tables() if t != "assets"]
        self.assertTrue(linked)
        added = []
        for t, c in linked:
            try:
                self._link_row(t, c, aid)
                added.append((t, c))
            except Exception:
                pass  # a table we can't fill with dummy values is skipped
        self.exec("UPDATE assets SET deleted_at='2026-01-01T00:00:00' WHERE id=?", (aid,))
        self.login("shop_admin")
        r = self.client.post(f"/assets/{aid}/purge")
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        self.assertIsNone(self.q1("SELECT 1 FROM assets WHERE id=?", (aid,)))
        dangling = [(t, c) for t, c in added
                    if self.q1(f"SELECT 1 FROM {t} WHERE {c}=?", (aid,))]
        self.assertEqual(dangling, [], "records still pointing at the deleted aircraft")

    def test_empty_trash_with_linked_aircraft_does_not_crash(self):
        aid = self.make_asset("N4343")
        for t, c in self._linked_tables():
            if t != "assets":
                try:
                    self._link_row(t, c, aid)
                except Exception:
                    pass
        self.exec("UPDATE assets SET deleted_at='2026-01-01T00:00:00' WHERE id=?", (aid,))
        self.login("shop_admin")
        r = self.client.post("/trash/empty")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.q1("SELECT 1 FROM assets WHERE id=?", (aid,)))


class RestoreEdgesTest(OpsHubTestCase):
    def test_restoring_a_job_that_was_never_deleted_changes_nothing(self):
        pid = self.make_project("Live")
        before = self.q1("SELECT code, name FROM projects WHERE id=?", (pid,))
        self.login("shop_admin")
        self.assertEqual(self.client.post(f"/projects/{pid}/restore").status_code, 302)
        after = self.q1("SELECT code, name FROM projects WHERE id=?", (pid,))
        self.assertEqual(tuple(before), tuple(after))

    def test_restored_job_never_shares_a_code_with_another_job(self):
        a = self.make_project("A")
        self.login("shop_admin")
        self.client.post(f"/projects/{a}/trash")
        b = self.make_project("B")
        self.client.post(f"/projects/{a}/restore")
        codes = [r["code"] for r in self.q("SELECT code FROM projects WHERE deleted_at IS NULL")]
        self.assertEqual(len(codes), len(set(codes)), codes)

    def test_job_on_a_trashed_aircraft_still_opens(self):
        aid = self.make_asset("N5050")
        pid = self.make_project("On trashed", asset_id=aid)
        self.login("shop_admin")
        self.client.post(f"/assets/{aid}/trash")
        self.assertEqual(self.client.get(f"/projects/{pid}").status_code, 200)

    def test_trash_page_with_many_items_loads(self):
        self.login("shop_admin")
        for i in range(15):
            pid = self.make_project(f"J{i}")
            self.client.post(f"/projects/{pid}/trash")
            aid = self.make_asset(f"N9{i:02d}")
            self.client.post(f"/assets/{aid}/trash")
        self.assertEqual(self.client.get("/trash").status_code, 200)
