"""Repair: harden the labor_sessions rebuild in db._migrate against the same
crash as qa-project-purge-crash (found_items) and qa-startup-old-backup-flights
(flights). Nothing references labor_sessions today, but the rebuild used to
run its DROP TABLE labor_sessions with foreign keys on - so the day
something does reference it, an old backup that still needs this rebuild
would fail to start with "FOREIGN KEY constraint failed", exactly like the
other two. The rebuild now runs with foreign keys off, keeps every id, and
turns them back on after - and tools/predeploy_check.py now catches any
future table rebuild that skips this, before it ever reaches the Pi.
"""
from harness import OpsHubTestCase
import db


class LaborSessionsRebuildFKTest(OpsHubTestCase):
    def make_old_shape_labor_sessions(self):
        """Swap labor_sessions back to the pre-fix shape (project_id NOT
        NULL) with one session clocked against a real project."""
        self.asset = self.make_asset("N54321")
        self.project = self.make_project(name="Annual - N54321", asset_id=self.asset)
        row = self.q1("SELECT id FROM laborers LIMIT 1")
        if row is None:
            from harness import seed_row
            conn = db.get_db()
            self.laborer = seed_row(conn, "laborers", name="Joe Tech", code="L-0001")
            conn.commit()
            conn.close()
        else:
            self.laborer = row["id"]
        conn = db.get_db()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE labor_sessions")
        conn.execute("""CREATE TABLE labor_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            laborer_id INTEGER NOT NULL REFERENCES laborers(id),
            project_id INTEGER NOT NULL REFERENCES projects(id),
            section TEXT,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at TEXT,
            hours REAL,
            rate REAL,
            cost REAL,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("INSERT INTO labor_sessions (id, laborer_id, project_id, started_at, ended_at, hours, cost, created_at) "
                     "VALUES (3, ?, ?, ?, ?, 1.0, 50, ?)",
                     (self.laborer, self.project, db.now_iso(), db.now_iso(), db.now_iso()))
        conn.commit()
        conn.close()

    def test_rebuild_with_an_existing_session_does_not_crash_startup(self):
        self.make_old_shape_labor_sessions()
        db.init_db()  # would raise sqlite3.IntegrityError if a future FK pointed at labor_sessions

        cols = {c["name"]: c for c in self.q("PRAGMA table_info(labor_sessions)")}
        self.assertEqual(cols["project_id"]["notnull"], 0)

        row = self.q1("SELECT laborer_id, project_id, cost FROM labor_sessions WHERE id = 3")
        self.assertEqual(row["laborer_id"], self.laborer)
        self.assertEqual(row["project_id"], self.project)
        self.assertEqual(row["cost"], 50)

        conn = db.get_db()
        try:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()

    def test_foreign_keys_are_back_on_after_the_rebuild(self):
        self.make_old_shape_labor_sessions()
        conn = db.get_db()
        try:
            conn.executescript(open(db.SCHEMA_PATH).read())
            conn.commit()
            db._migrate(conn)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            conn.close()
