"""An existing database (made before the ForeFlight import columns) must still start:
schema.sql must not build an index on columns the migration adds afterwards."""
import os
import sqlite3
import tempfile
import unittest

import harness  # noqa: F401  (puts the repo on sys.path)
import db


class LogbookImportUpgradeTest(unittest.TestCase):
    def test_old_database_upgrades_on_startup(self):
        old_path = db.DB_PATH
        d = tempfile.mkdtemp(prefix="opshub-upgrade-")
        try:
            db.DB_PATH = os.path.join(d, "t.db")
            db.init_db()
            c = sqlite3.connect(db.DB_PATH)
            c.execute("DROP INDEX idx_pilot_logbook_import")
            for col in ("sic", "dual_given", "ifr", "sim_flight", "ground", "source", "import_key"):
                c.execute(f"ALTER TABLE pilot_logbook DROP COLUMN {col}")
            c.commit(); c.close()
            db.init_db()          # used to raise: no such column: import_key
            c = sqlite3.connect(db.DB_PATH)
            cols = [r[1] for r in c.execute("PRAGMA table_info(pilot_logbook)")]
            c.close()
            self.assertIn("import_key", cols)
        finally:
            db.DB_PATH = old_path


if __name__ == "__main__":
    unittest.main()
