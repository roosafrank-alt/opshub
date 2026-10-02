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


    # ---- Side by side without a second address ------------------------------------------------
    # The "before" copy runs on the same Pi, so this server fetches it itself (over localhost) and
    # shows it under /__preview/before/... That way the split view is one page on one address: no
    # second certificate warning, no cross-site cookie problems. Only the preview copy has this.
    if label == "preview":
        import http.client, ssl
        PFX = "/__preview/before"
        _attr = re.compile(r"""((?:href|src|action|data-href|data-url|data-live-url|formaction|poster)\s*=\s*)(["'])/(?!/)""", re.I)
        _css = re.compile(r"""url\(\s*(["']?)/(?!/)""", re.I)
        _shim = ("<script>(function(){var P='%s';function f(u){return (typeof u==='string'&&u.charAt(0)==='/'&&u.charAt(1)!=='/'&&u.indexOf(P)!==0)?P+u:u;}"
                 "var F=window.fetch;if(F)window.fetch=function(u,o){return F.call(this,(u&&u.url)?u:f(u),o);};"
                 "var X=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){arguments[1]=f(u);return X.apply(this,arguments);};"
                 "if(window.EventSource){var E=window.EventSource;window.EventSource=function(u,c){return new E(f(u),c);};}})();</script>") % PFX
        _hop = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "server", "date", "te", "upgrade"}

        def _fix(text, kind):
            if kind == "html":
                text = _attr.sub(lambda m: m.group(1) + m.group(2) + PFX + "/", text)
                text = _css.sub(lambda m: "url(" + m.group(1) + PFX + "/", text)
                i = text.lower().find("<head")
                if i != -1:
                    j = text.find(">", i)
                    text = text[:j + 1] + _shim + text[j + 1:]
                return text
            return _css.sub(lambda m: "url(" + m.group(1) + PFX + "/", text)

        @app.route(PFX, defaults={"rest": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
        @app.route(PFX + "/", defaults={"rest": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
        @app.route(PFX + "/<path:rest>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
        def pv_before(rest):
            target = "/" + rest + (("?" + request.query_string.decode()) if request.query_string else "")
            hdrs = {"Host": "127.0.0.1:%s" % cfg["before_port"], "Accept-Encoding": "identity"}
            for k in ("Cookie", "Content-Type", "Accept", "User-Agent", "X-Requested-With", "Accept-Language"):
                if request.headers.get(k):
                    hdrs[k] = request.headers[k]
            body = request.get_data() if request.method in ("POST", "PUT", "PATCH", "DELETE") else None
            up = conn = err = None
            for scheme in ("https", "http"):   # the Pi's copies use https; a copy run without certificates uses http
                try:
                    conn = (http.client.HTTPSConnection("127.0.0.1", cfg["before_port"], timeout=25, context=ssl._create_unverified_context())
                            if scheme == "https" else http.client.HTTPConnection("127.0.0.1", cfg["before_port"], timeout=25))
                    conn.request(request.method, target, body=body, headers=hdrs)
                    up = conn.getresponse()
                    data = up.read()
                    break
                except (ssl.SSLError, http.client.BadStatusLine, ConnectionResetError, OSError) as e:
                    err = e
                    up = None
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
            if up is None:
                return Response("The before copy is not answering (is the opshub-before service running?): %s" % err,
                                status=502, mimetype="text/plain")
            ctype = up.getheader("Content-Type", "") or ""
            if "text/html" in ctype:
                data = _fix(data.decode("utf-8", "replace"), "html").encode("utf-8")
            elif "text/css" in ctype:
                data = _fix(data.decode("utf-8", "replace"), "css").encode("utf-8")
            resp = Response(data, status=up.status)
            for k, v in up.getheaders():
                kl = k.lower()
                if kl in _hop or kl == "set-cookie":
                    continue
                if kl == "location":
                    v = re.sub(r"^https?://[^/]+", "", v)
                    if v.startswith("/") and not v.startswith("//"):
                        v = PFX + v
                resp.headers[k] = v
            for v in up.msg.get_all("Set-Cookie") or []:
                resp.headers.add("Set-Cookie", v)
            conn.close()
            return resp

    @app.route("/__preview/all")
    def pv_all():
        return Response(_read("static/all.html"), mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.route("/__preview/outbox")
    def pv_outbox():
        def _show(text):   # escape, then make web links tappable (so a reset link can be opened)
            text = re.sub("<", "&lt;", text).replace(">", "&gt;")
            return re.sub(r"(https?://[^\s&]+)", r"<a href='\1'>\1</a>", text).replace("\n", "<br>")
        rows = "".join("<tr><td>%s</td><td><code style='white-space:normal'>%s</code></td></tr>" % (re.sub("<", "&lt;", k), _show(d))
                       for k, d in reversed(safety.OUTBOX)) or "<tr><td colspan=2>Nothing has been blocked yet.</td></tr>"
        return Response("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                        "<title>Blocked sends</title><body style='font:15px system-ui;margin:16px'>"
                        "<h3>Things the preview did NOT send</h3><p>Emails, texts, phone alerts, Wave calls, label prints and "
                        "restarts are switched off in the preview. This is what would have happened.</p>"
                        "<table border=1 cellpadding=6 style='border-collapse:collapse'>" + rows + "</table>"
                        "<p><a href='javascript:history.back()'>Back</a></p>", mimetype="text/html")
    return app
