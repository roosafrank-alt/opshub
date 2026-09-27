#!/usr/bin/env python3
"""Pre-deploy sanity check for the shopinv/OpsHub staging folder.

Run this against ~/Desktop/shopinv_v10 (the folder that gets rsynced to
the Pi) right before every deploy - especially useful when more than one
Claude session is editing that same folder independently (see the broken
"Alerts" nav link from 2026-09-23: a template referenced a route that was
never actually built, and it broke every Flight School page site-wide).
Nothing here is specific to who made the change; it just checks that the
folder, as it currently sits, is internally consistent.

Catches, cheaply and in well under a second:
  1. Python syntax errors in any .py file.
  2. Any url_for('some.endpoint') in a template that doesn't match a real
     Flask route (exactly tonight's bug).
  3. Any {% extends "x" %} / {% include "x" %} template path that doesn't
     exist under templates/.
  4. A db.py one-time table rebuild (CREATE ..._new / DROP TABLE <real
     table> / RENAME ..._new) that doesn't turn foreign keys off for the
     swap. On a database old enough to still need the rebuild, DROP TABLE
     is an implicit delete of every row, so it fails with "FOREIGN KEY
     constraint failed" - and the app won't start - the moment anything
     else in the database references that table (qa-project-purge-crash's
     found_items rebuild, then qa-startup-old-backup-flights's flights
     rebuild, both hit this the same way, on live Pi backups). This isn't
     about who references the table today; it's about every rebuild
     guarding itself so the *next* one doesn't have to be found by a
     crash on the Pi.

Usage:
    cd ~/Desktop/shopinv_v10   (or wherever this checkout lives)
    python3 tools/predeploy_check.py

Exits 0 and prints "OK" if everything checks out; exits 1 and prints every
problem found otherwise. Safe to run any time - read-only, touches no
files, doesn't need the app's actual database (instance/ isn't required).
"""
import glob
import os
import py_compile
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
problems = []


def _skip(path):
    return "/vendor/" in path or "/instance/" in path or "/.git/" in path


# --- 1) Python syntax check on every .py file (app code, not vendor/) ------
py_files = [p for p in glob.glob(os.path.join(ROOT, "**/*.py"), recursive=True) if not _skip(p)]
for pyfile in py_files:
    try:
        py_compile.compile(pyfile, doraise=True, quiet=2)
    except py_compile.PyCompileError as e:
        problems.append(f"SYNTAX ERROR: {os.path.relpath(pyfile, ROOT)}: {e.msg}")

if problems:
    print(f"{len(problems)} problem(s) found - stopping before the app-import check "
          f"(a syntax error would just cascade into a fake import failure):\n")
    print("\n".join(problems))
    sys.exit(1)

# --- 2) Load the real app to get its actual endpoint list -----------------
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "vendor"))
os.chdir(ROOT)
try:
    import app as appmod
    endpoints = {r.endpoint for r in appmod.app.url_map.iter_rules()}
except Exception as e:
    print(f"APP IMPORT FAILED - can't check url_for() references without a loadable app:\n{e}")
    sys.exit(1)

# --- 3) Every literal url_for('...') in every template must be real -------
url_for_re = re.compile(r"url_for\(\s*['\"]([\w.]+)['\"]")
template_files = glob.glob(os.path.join(ROOT, "templates/**/*.html"), recursive=True)
for tpl in template_files:
    text = open(tpl, encoding="utf-8").read()
    for m in url_for_re.finditer(text):
        endpoint = m.group(1)
        if endpoint not in endpoints:
            line_no = text.count("\n", 0, m.start()) + 1
            problems.append(f"BAD url_for: {os.path.relpath(tpl, ROOT)}:{line_no}: "
                             f"references endpoint '{endpoint}' which doesn't exist")

# --- 4) Every {% extends %} / {% include %} path must exist ---------------
tpl_ref_re = re.compile(r"{%-?\s*(?:extends|include)\s+['\"]([^'\"]+)['\"]")
for tpl in template_files:
    text = open(tpl, encoding="utf-8").read()
    for m in tpl_ref_re.finditer(text):
        ref = m.group(1)
        if not os.path.exists(os.path.join(ROOT, "templates", ref)):
            line_no = text.count("\n", 0, m.start()) + 1
            problems.append(f"MISSING TEMPLATE: {os.path.relpath(tpl, ROOT)}:{line_no}: "
                             f"references '{ref}' which doesn't exist under templates/")

# --- 5) Every db.py table-rebuild's DROP TABLE runs with foreign keys off -
# A rebuild looks like: CREATE TABLE x_new (...), INSERT INTO x_new SELECT
# ... FROM x, DROP TABLE x, ALTER TABLE x_new RENAME TO x. Only the DROP of
# the REAL table (never "IF EXISTS", never a "_new" scratch table) can hit
# "FOREIGN KEY constraint failed" on an old database where something else
# already references it - so every such DROP TABLE must have foreign keys
# already off (PRAGMA foreign_keys = OFF, not yet turned back ON) at that
# point in db.py, read top to bottom.
db_py = os.path.join(ROOT, "db.py")
if os.path.exists(db_py):
    text = open(db_py, encoding="utf-8").read()
    fk_off = False
    token_re = re.compile(
        r"PRAGMA\s+foreign_keys\s*=\s*(OFF|ON)"
        r"|DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)"
        r"|RENAME\s+TO\s+(\w+)",
        re.IGNORECASE,
    )
    for m in token_re.finditer(text):
        if m.group(1):
            fk_off = m.group(1).upper() == "OFF"
            continue
        table = m.group(2)
        if table is None:
            continue  # a RENAME TO, not relevant on its own
        if "IF EXISTS" in m.group(0).upper() or table.endswith("_new"):
            continue  # scratch table or a defensive drop, not a real rebuild
        # Confirm this is a rename-based rebuild (a real DROP TABLE outside
        # that pattern would be unusual, but isn't what this check is for).
        tail = text[m.end():m.end() + 400]
        if not re.search(r"RENAME\s+TO\s+" + re.escape(table) + r"\b", tail, re.IGNORECASE):
            continue
        if not fk_off:
            line_no = text.count("\n", 0, m.start()) + 1
            problems.append(f"UNGUARDED TABLE REBUILD: db.py:{line_no}: DROP TABLE {table} "
                             f"runs with foreign keys on - an old database where anything "
                             f"references {table} will fail to start with 'FOREIGN KEY "
                             f"constraint failed'. Wrap the rebuild in "
                             f"PRAGMA foreign_keys = OFF / try / finally: PRAGMA foreign_keys = ON, "
                             f"same as the flights and found_items rebuilds.")

if problems:
    print(f"{len(problems)} problem(s) found:\n")
    print("\n".join(problems))
    sys.exit(1)

print(f"OK - {len(py_files)} .py files, {len(template_files)} templates, {len(endpoints)} routes. "
      f"Every url_for()/extends/include reference resolves.")
sys.exit(0)
