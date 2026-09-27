"""Startup crash fix: the one-time found_items rebuild in db._migrate (from
qa-project-purge-crash) did DROP TABLE found_items with foreign keys on.
On a database where a photo was already attached to a found item
(photos.found_item_id), that drop failed with "FOREIGN KEY constraint
failed" and the app wouldn't start. The rebuild now runs with foreign keys
off, keeps every id, and turns them back on after.
"""
from harness import OpsHubTestCase
import db


class FoundItemsRebuildFKTest(OpsHubTestCase):
    def make_old_shape_found_items(self):
        """Swap found_items back to the pre-fix shape (project_id NOT NULL,
        no asset_id) with one item and a photo attached to it."""
        self.asset = self.make_asset("N54321")
        self.project = self.make_project(name="Annual - N54321", asset_id=self.asset)
        conn = db.get_db()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE found_items")
        conn.execute("""CREATE TABLE found_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            description TEXT NOT NULL,
            est_parts REAL NOT NULL DEFAULT 0,
            est_labor_hours REAL NOT NULL DEFAULT 0,
            est_labor_rate REAL NOT NULL DEFAULT 0,
            est_total REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'waiting',
            created_by TEXT,
            created_at TEXT NOT NULL,
            notified_at TEXT,
            decided_at TEXT,
            decided_by TEXT,
            decision_note TEXT,
            section_name TEXT
        )""")
        conn.execute("INSERT INTO found_items (id, project_id, description, created_at) "
                     "VALUES (7, ?, 'Cracked exhaust stack', ?)", (self.project, db.now_iso()))
        conn.execute("INSERT INTO photos (filename, found_item_id) VALUES ('stack.jpg', 7)")
        conn.commit()
        conn.close()

    def test_rebuild_with_a_photo_on_a_found_item_does_not_crash_startup(self):
        self.make_old_shape_found_items()
        db.init_db()  # raised sqlite3.IntegrityError before the fix

        cols = {c["name"]: c for c in self.q("PRAGMA table_info(found_items)")}
        self.assertIn("asset_id", cols)
        self.assertEqual(cols["project_id"]["notnull"], 0)

        item = self.q1("SELECT project_id, asset_id, description FROM found_items WHERE id = 7")
        self.assertEqual(item["project_id"], self.project)
        self.assertEqual(item["asset_id"], self.asset)
        self.assertEqual(self.q1("SELECT found_item_id FROM photos WHERE filename = 'stack.jpg'")["found_item_id"], 7)

        conn = db.get_db()
        try:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()

    def test_foreign_keys_are_back_on_after_the_rebuild(self):
        self.make_old_shape_found_items()
        conn = db.get_db()
        try:
            conn.executescript(open(db.SCHEMA_PATH).read())
            conn.commit()
            db._migrate(conn)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            conn.close()
