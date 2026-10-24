"""General safeguard for the bug class behind qa-project-purge-crash and
qa-startup-old-backup-flights: a one-time table rebuild inside db._migrate
(the "make a column nullable" recipe - create a *_new table, copy the data,
DROP the old table, rename) that runs with foreign keys still on. SQLite's
DROP TABLE is an implicit delete of every row in it, so the drop fails with
"FOREIGN KEY constraint failed" - and the app won't start - the moment any
other table has a row that references the table being rebuilt. That only
shows up on a database old enough, or lived-in enough, to have such a row,
so a fresh dev database or an empty test fixture never catches it. It hit
found_items, then flights, each time only after it actually crashed the app
on someone's real data.

Rather than adding one more per-table regression test after the next crash,
this scans db.py itself: every unconditional `DROP TABLE <name>` of a real
table (not `DROP TABLE IF EXISTS <name>_new`, which is scratch-table cleanup
before a CREATE, not a data-carrying drop) must be preceded, earlier in the
same function, by `PRAGMA foreign_keys = OFF` - the only way SQLite allows
a rebuilt table's rows to keep the ids other tables' foreign keys point at
without the drop itself failing first.

This is a source-level check, not a behavioral one: it can't tell you
whether a *specific* rebuild is safe (test_flights_rebuild_fk.py and
test_found_items_rebuild_fk.py do that, with real FK-referencing rows), but
it makes sure every rebuild at least takes the precaution that makes such a
test possible to pass - so the next one-off table rebuild someone adds is
caught here, on day one, instead of on someone's old backup in production.
"""
import ast
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_ROOT, "db.py")


def _string_literal(node):
    """Best-effort literal string value of an ast node, else None (covers
    plain string constants and simple f-strings with no interpolation)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and all(
        isinstance(part, ast.Constant) for part in node.values
    ):
        return "".join(part.value for part in node.values)
    return None


def _sql_calls_in_order(func_node):
    """Every conn.execute(...)/conn.executescript(...) call in a function,
    in source order, as (lineno, sql-text-or-None) pairs."""
    calls = []
    for node in ast.walk(func_node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("execute", "executescript")
            and node.args
        ):
            calls.append((node.lineno, _string_literal(node.args[0])))
    calls.sort(key=lambda pair: pair[0])
    return calls


class MigrateRebuildForeignKeyGuardTest(unittest.TestCase):
    def test_every_drop_table_in_migrate_is_guarded_by_foreign_keys_off(self):
        tree = ast.parse(open(DB_PATH, encoding="utf-8").read(), filename=DB_PATH)
        migrate_fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "_migrate"),
            None,
        )
        self.assertIsNotNone(migrate_fn, "db.py has no _migrate function to check")

        foreign_keys_off_seen_by = None  # lineno of the *last* seen FK-off PRAGMA
        unguarded_drops = []

        for lineno, sql in _sql_calls_in_order(migrate_fn):
            if sql is None:
                continue
            normalized = " ".join(sql.split()).upper()

            if "PRAGMA FOREIGN_KEYS" in normalized and "OFF" in normalized:
                foreign_keys_off_seen_by = lineno
            elif "PRAGMA FOREIGN_KEYS" in normalized and "ON" in normalized:
                # Rebuild block closed out; a later DROP TABLE belongs to a
                # different piece of migration logic and needs its own guard.
                foreign_keys_off_seen_by = None

            if normalized.startswith("DROP TABLE"):
                is_scratch_table_cleanup = "IF EXISTS" in normalized and normalized.rstrip(
                    ";"
                ).endswith("_NEW")
                if is_scratch_table_cleanup:
                    continue
                if foreign_keys_off_seen_by is None:
                    unguarded_drops.append((lineno, sql.strip()))

        self.assertEqual(
            unguarded_drops, [],
            "db.py:_migrate has a DROP TABLE that runs without a preceding "
            "'PRAGMA foreign_keys = OFF' in the same function - this is the "
            "exact shape of qa-project-purge-crash and "
            "qa-startup-old-backup-flights: it will raise "
            "'FOREIGN KEY constraint failed' and stop the app from starting "
            "on any database old enough to have a row referencing the table "
            "being dropped. Wrap the rebuild the same way flights and "
            "found_items are wrapped: commit(), PRAGMA foreign_keys = OFF, "
            "the create/copy/drop/rename, then PRAGMA foreign_keys = ON in a "
            f"finally block. Offending line(s): {unguarded_drops}"
        )


if __name__ == "__main__":
    unittest.main()
