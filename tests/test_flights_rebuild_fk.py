"""Startup crash fix (qa-startup-old-backup-flights): the one-time flights
rebuild in db._migrate (making cfi_id nullable, for a solo flight) did
DROP TABLE flights with foreign keys on. On an older backup where a flight
already has a billing charge (student_ledger.flight_id) or a logbook entry
(pilot_logbook.flight_id) attached, that drop failed with "FOREIGN KEY
constraint failed" and the app wouldn't start. The rebuild now runs with
foreign keys off, keeps every id, and turns them back on after - same fix
as qa-project-purge-crash's found_items rebuild.
"""
from harness import OpsHubTestCase
import db


class FlightsRebuildFKTest(OpsHubTestCase):
    def make_old_shape_flights_with_billing_and_logbook(self):
        """Swap flights back to the pre-fix shape (cfi_id NOT NULL) with one
        flight that has a billing charge and a pilot logbook entry attached."""
        self.asset = self.make_asset("N54321")
        student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        conn = db.get_db()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE flights")
        conn.execute("""CREATE TABLE flights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cfi_id INTEGER NOT NULL REFERENCES cfis(id),
            student_id INTEGER NOT NULL REFERENCES students(id),
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            flight_date TEXT NOT NULL,
            hobbs_start REAL,
            hobbs_end REAL,
            tach_start REAL,
            tach_end REAL,
            oil_added_qt REAL,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("INSERT INTO flights (id, cfi_id, student_id, asset_id, flight_date, created_at) "
                     "VALUES (9, ?, ?, ?, ?, ?)", (cfi, student, self.asset, db.now_iso()[:10], db.now_iso()))
        conn.execute("INSERT INTO student_ledger (student_id, entry_type, amount, flight_id, created_at) "
                     "VALUES (?, 'flight_deduction', -50, 9, ?)", (student, db.now_iso()))
        conn.execute("INSERT INTO pilot_logbook (flight_id, student_id, cfi_id, asset_id, entry_date, total) "
                     "VALUES (9, ?, ?, ?, ?, 1.2)", (student, cfi, self.asset, db.now_iso()[:10]))
        conn.commit()
        conn.close()

    def test_rebuild_with_billing_and_logbook_on_a_flight_does_not_crash_startup(self):
        self.make_old_shape_flights_with_billing_and_logbook()
        db.init_db()  # raised sqlite3.IntegrityError before the fix

        cols = {c["name"]: c for c in self.q("PRAGMA table_info(flights)")}
        self.assertEqual(cols["cfi_id"]["notnull"], 0)

        self.assertEqual(self.q1("SELECT flight_id FROM student_ledger WHERE flight_id = 9")["flight_id"], 9)
        self.assertEqual(self.q1("SELECT flight_id FROM pilot_logbook WHERE flight_id = 9")["flight_id"], 9)

        conn = db.get_db()
        try:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()

    def test_foreign_keys_are_back_on_after_the_flights_rebuild(self):
        self.make_old_shape_flights_with_billing_and_logbook()
        conn = db.get_db()
        try:
            db._migrate(conn)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            conn.close()
