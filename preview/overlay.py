"""Flask side of the preview: banner/highlight injection, the change list,
the side-by-side page and the 'what would have been sent' page."""
import glob
import json
import os
import re

from flask import Response, jsonify, request

from . import safety

HERE = os.path.dirname(os.path.abspath(__file__))
_NO_INJECT = ("/__preview/",)


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def load_changes():
    """spec.json (what Frank approved: id -> title/before/after/note) merged
    with every preview/changes/*.json registry file the builders wrote
    (id, status, pages, where, see_as). A change with no registry entry shows
    as 'not marked yet' so nothing silently goes missing."""
    try:
        spec = json.loads(_read("spec.json"))
    except Exception:
        spec = {}
    reg = {}
    for path in sorted(glob.glob(os.path.join(HERE, "changes", "*.json"))):
        try:
            for e in json.load(open(path, encoding="utf-8")):
                reg[e["id"]] = e
        except Exception:
            continue
    out = []
    for cid, s in spec.items():
        e = reg.get(cid, {})
        out.append(dict(id=cid, title=s.get("title", cid), before=s.get("before", ""), after=s.get("after", ""),
                        group=s.get("group", ""), note=s.get("note", ""), decision=s.get("decision", ""),
                        status=e.get("status", "pending"), pages=e.get("pages", []),
                        where=e.get("where", ""), see_as=e.get("see_as", ""), detail=e.get("detail", "")))
    return out


def init(app, label="preview", before_port=5052, preview_port=5051):
    """Attach the preview tooling to a normal Flask app. label is 'preview'
    (new code, highlights on) or 'before' (unchanged code, banner only)."""
    cfg = dict(label=label, before_port=before_port, preview_port=preview_port)

    @app.after_request
    def _inject(resp):
        try:
            if resp.mimetype != "text/html" or resp.direct_passthrough or resp.status_code >= 400 and False:
                return resp
            if request.path.startswith(_NO_INJECT) or request.args.get("embedded") == "1":
                return resp
            body = resp.get_data(as_text=True)
            tag = '<script src="/__preview/overlay.js" data-pv-label="%s" defer></script>' % cfg["label"]
            i = body.lower().rfind("</body>")
            if i == -1 or "/__preview/overlay.js" in body:
                return resp
            resp.set_data(body[:i] + tag + body[i:])
        except Exception:
            pass
        return resp

    @app.route("/__preview/overlay.js")
    def pv_js():
        return Response(_read("static/overlay.js"), mimetype="application/javascript",
                        headers={"Cache-Control": "no-store"})

    @app.route("/__preview/changes.json")
    def pv_changes():
        return jsonify(label=cfg["label"], before_port=cfg["before_port"], preview_port=cfg["preview_port"],
                       changes=load_changes(),
                       outbox=[dict(kind=k, detail=d) for k, d in safety.OUTBOX[-50:]])

    @app.route("/__preview/compare")
    def pv_compare():
        return Response(_read("static/compare.html"), mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.route("/__preview/all")
    def pv_all():
        return Response(_read("static/all.html"), mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.route("/__preview/outbox")
    def pv_outbox():
        rows = "".join("<tr><td>%s</td><td><code>%s</code></td></tr>" % (re.sub("<", "&lt;", k), re.sub("<", "&lt;", d))
                       for k, d in reversed(safety.OUTBOX)) or "<tr><td colspan=2>Nothing has been blocked yet.</td></tr>"
        return Response("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                        "<title>Blocked sends</title><body style='font:15px system-ui;margin:16px'>"
                        "<h3>Things the preview did NOT send</h3><p>Emails, texts, phone alerts, Wave calls, label prints and "
                        "restarts are switched off in the preview. This is what would have happened.</p>"
                        "<table border=1 cellpadding=6 style='border-collapse:collapse'>" + rows + "</table>"
                        "<p><a href='javascript:history.back()'>Back</a></p>", mimetype="text/html")
    return app
