#!/usr/bin/env python3
"""Run a PREVIEW copy of OpsHub next to the live one, never instead of it.

    python3 run_preview.py --tree ~/shopinv-preview --port 5051 --label preview
    python3 run_preview.py --tree ~/shopinv-before  --port 5052 --label before

--tree   the folder holding that version of the app (own instance/ with a COPY of the data)
--label  preview = new code with changes outlined; before = unchanged code, banner only

Nothing here touches the live app on port 5050, and a preview can't send
email, texts, phone alerts, Wave calls or restarts (see preview/safety.py).
Never run this against the live instance folder.
"""
import argparse
import os
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--tree", default=os.path.dirname(os.path.abspath(__file__)))
ap.add_argument("--port", type=int, default=5051)
ap.add_argument("--label", choices=("preview", "before"), default="preview")
ap.add_argument("--before-port", type=int, default=5052)
ap.add_argument("--preview-port", type=int, default=5051)
ap.add_argument("--db", help="override the database file (testing only)")
args = ap.parse_args()

HERE = os.path.dirname(os.path.abspath(__file__))
tree = os.path.abspath(args.tree)
if args.port == 5050:
    sys.exit("Refusing to run on port 5050: that is the live app's port.")
vend = os.path.join(tree, "vendor")   # the Pi keeps Flask in a vendor folder
if os.path.isdir(vend):
    sys.path.insert(0, vend)
sys.path.insert(0, HERE)            # our preview/ package
import preview  # noqa: E402
# This copy's own folder goes first on the path BEFORE neutralize(): that call imports notify (which imports db),
# and those must come from THIS tree - otherwise the "before" copy would load the new code's db.py and database.
sys.path.insert(0, tree)
os.chdir(tree)
preview.neutralize()                # before the app is imported
import db  # noqa: E402
if args.db:
    db.DB_PATH = args.db
import app as opshub  # noqa: E402
preview.neutralize()                # again: label_printer etc. now exist
opshub.init_db()                    # safe: IF NOT EXISTS, runs the version's own migrations on the COPY
preview.init(opshub.app, label=args.label, before_port=args.before_port, preview_port=args.preview_port)

cert, key = os.path.join(tree, "cert.pem"), os.path.join(tree, "key.pem")
kw = dict(host="0.0.0.0", port=args.port, debug=False, threaded=True)
if os.path.exists(cert) and os.path.exists(key):
    kw["ssl_context"] = (cert, key)
print(f"{args.label} copy on port {args.port} ({'https' if 'ssl_context' in kw else 'http'}), data: {db.DB_PATH}")
opshub.app.run(**kw)       # no background alert loop is started here, on purpose
