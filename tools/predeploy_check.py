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

if problems:
    print(f"{len(problems)} problem(s) found:\n")
    print("\n".join(problems))
    sys.exit(1)

print(f"OK - {len(py_files)} .py files, {len(template_files)} templates, {len(endpoints)} routes. "
      f"Every url_for()/extends/include reference resolves.")
sys.exit(0)
