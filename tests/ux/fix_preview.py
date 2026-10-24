#!/usr/bin/env python3
"""Fix previews for Idea Queue cards: a NOW picture with the problem circled
in red, and an AFTER picture with the proposed fix mocked up and what changed
circled in green.

It runs the real app on the same throwaway sample data as ux_audit.py, opens
the page as the right account at phone or desktop size, takes the NOW shot,
then applies a temporary in-browser mockup of the fix (JavaScript that edits
the page in the browser only: no app code changes, nothing saved) and takes
the AFTER shot of the same area.

    python3 tests/ux/fix_preview.py specs.json --out /tmp/previews

specs.json is a list of specs:
  {
    "name": "sun-times",                 # file name stem
    "role": "cfi",                       # master | shop_admin | tech | cfi | flight_student | customer
    "size": "phone",                     # phone | desktop
    "path": "/flight/dashboard",         # may use {project} {asset} {part} {order} {student} ... ids
    "setup_js": "...",                   # optional: runs before both shots (open a menu, fill a field)
    "focus": "#sunline",                 # optional: crop to this element (CSS selector, plus padding);
                                         #   omit for the top screenful, "full" for the whole page,
                                         #   {"from": sel, "to": sel} for the full-width band between two
    "pad": 16,                           # optional crop padding in px
    "problem": [{"sel": "...", "note": "Times split across lines"}],   # red marks on NOW
    "mock_js": "...",                    # JS that makes the page look fixed (browser only)
    "mock_html": "<p>...</p>",           # OR: replace <body> with this HTML (for error pages, new screens)
    "after_path": "/flight/dashboard",   # optional: open this page before the mockup (e.g. a fix that
                                         #   redirects somewhere else); same {id} placeholders
    "focus_after": "#sunline",           # optional: crop for AFTER (defaults to focus)
    "changed": [{"sel": "...", "note": "One line per time, no '|'"}], # green marks on AFTER
    "now_only": false                    # true: just the marked NOW shot (no mockup)
  }
A mark's "sel" may match several elements; each gets its own box. A mark may
use "box": [x, y, w, h] in page pixels instead of "sel".

Output: <name>-now.jpg, <name>-after.jpg and manifest.json with each shot's
marks as percentages of the picture ({x,y,w,h,note}), ready to store on the
card (screenshots[i].marks / previews[j].marks). Anything that failed is
listed in manifest.json with "error".
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import ux_audit as ux  # noqa: E402  (harness, sample data, CDN cache, screen sizes)
from ux_audit import harness, flask_app, PASSWORD  # noqa: E402

RECTS_JS = r"""
(specs) => specs.map(m => {
  if (m.box) return [{x: m.box[0], y: m.box[1], w: m.box[2], h: m.box[3]}];
  let els = [];
  try { els = [...document.querySelectorAll(m.sel)]; } catch (e) { return []; }
  return els.map(e => e.getBoundingClientRect()).filter(r => r.width > 0 && r.height > 0)
    .slice(0, 4).map(r => ({x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height}));
})
"""
FOCUS_JS = r"""
(sel) => { const e = document.querySelector(sel); if (!e) return null;
  const r = e.getBoundingClientRect(); return {x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height}; }
"""


def crop_box(page, focus, pad, vw):
    size = page.evaluate("() => ({w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight, vh: innerHeight})")
    if focus == "full":
        return (0, 0, size["w"], size["h"])
    if isinstance(focus, dict):  # {"from": sel, "to": sel}: the band from one element down to another
        a, b = page.evaluate(FOCUS_JS, focus["from"]), page.evaluate(FOCUS_JS, focus["to"])
        if a and b:
            y0, y1 = min(a["y"], b["y"]), max(a["y"] + a["h"], b["y"] + b["h"])
            return (0, int(max(0, y0 - pad)), size["w"], int(min(size["h"], y1 + pad)))
        focus = None
    if focus:
        r = page.evaluate(FOCUS_JS, focus)
        if r:
            x0 = 0 if r["w"] > vw * 0.6 else max(0, r["x"] - pad)
            x1 = size["w"] if r["w"] > vw * 0.6 else min(size["w"], r["x"] + r["w"] + pad)
            return (int(x0), int(max(0, r["y"] - pad)), int(x1), int(min(size["h"], r["y"] + r["h"] + pad)))
    return (0, 0, size["w"], min(size["h"], size["vh"]))


def shoot(page, spec, marks, focus, out_png):
    from PIL import Image
    vw = page.viewport_size["width"]
    box = crop_box(page, focus, spec.get("pad", 16), vw)
    rects = page.evaluate(RECTS_JS, marks or [])
    page.screenshot(path=out_png, full_page=True)
    im = Image.open(out_png).convert("RGB")
    scale = im.width / max(1, page.evaluate("() => document.documentElement.scrollWidth"))
    x0, y0, x1, y1 = box
    im = im.crop((int(x0 * scale), int(y0 * scale), int(x1 * scale), int(y1 * scale)))
    if im.width > 1200:
        im = im.resize((1200, int(im.height * 1200 / im.width)))
    jpg = out_png[:-4] + ".jpg"
    im.save(jpg, "JPEG", quality=85, optimize=True)
    os.remove(out_png)
    W, H = (x1 - x0) or 1, (y1 - y0) or 1
    out = []
    for m, rs in zip(marks or [], rects):
        for r in rs:
            x, y = r["x"] - x0 - 4, r["y"] - y0 - 4
            w, h = r["w"] + 8, r["h"] + 8
            if x + w < 0 or y + h < 0 or x > W or y > H:
                continue
            out.append({k: round(v, 1) for k, v in dict(x=max(0, x) * 100 / W, y=max(0, y) * 100 / H,
                        w=min(w, W - max(0, x)) * 100 / W, h=min(h, H - max(0, y)) * 100 / H).items()}
                       | ({"note": m["note"]} if m.get("note") else {}))
    return os.path.basename(jpg), out, (not rects or any(rects)) if marks else True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("specs")
    ap.add_argument("--out", default="fix-previews")
    ap.add_argument("--port", type=int, default=5098)
    args = ap.parse_args()
    specs = json.load(open(args.specs))
    os.makedirs(args.out, exist_ok=True)

    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server

    tc = ux._Seeder()
    tc.setUp()
    harness.GC_AFTER_REQUEST[0] = False
    ids = ux.seed_realistic(tc)
    tc.exec("UPDATE users SET tour_seen_shop = 1, tour_seen_flight = 1")
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    srv = make_server("127.0.0.1", args.port, flask_app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{args.port}"

    fakes = {k: getattr(subprocess, k) for k in harness.REAL_SUBPROCESS}
    for k, v in harness.REAL_SUBPROCESS.items():
        setattr(subprocess, k, v)
    pw = sync_playwright().start()
    for k, v in fakes.items():
        setattr(subprocess, k, v)
    manifest = []
    with ux._Closing(pw) as p:
        launch = dict(args=["--no-sandbox"])
        exe = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
        if os.path.exists(exe):
            launch["executable_path"] = exe
        browser = p.chromium.launch(**launch)
        for spec in specs:
            rec = {"name": spec["name"]}
            try:
                ctx = browser.new_context(**(ux.PHONE if spec.get("size", "phone") == "phone" else ux.DESKTOP))
                ctx.route(re.compile(r"^https?://(?!127\.0\.0\.1)"), ux._serve_cdn)
                role = spec.get("role", "master")
                user = "owner@example.com" if role == "customer" else role
                ctx.request.post(base + "/", form={"username": user, "password": PASSWORD, "remember": "on"})
                page = ctx.new_page()
                path = spec["path"].format(**{k: v for k, v in ids.items()})
                try:
                    page.goto(base + path, wait_until="load", timeout=20000)
                except Exception as e:  # noqa: BLE001  (e.g. a redirect loop: the NOW shot is the error page)
                    rec["load_error"] = str(e)[:160]
                time.sleep(0.3)
                if spec.get("dump"):
                    open(os.path.join(args.out, spec["name"] + ".html"), "w").write(page.content())
                if spec.get("setup_js"):
                    page.evaluate(spec["setup_js"]); time.sleep(0.2)
                name, marks, ok = shoot(page, spec, spec.get("problem"), spec.get("focus"), os.path.join(args.out, spec["name"] + "-now.png"))
                rec["now"] = {"file": name, "marks": marks}
                if not ok:
                    rec["warning"] = "a problem mark matched nothing"
                if not spec.get("now_only"):
                    if spec.get("after_path"):  # the fix lands the person on a different page
                        page.goto(base + spec["after_path"].format(**ids), wait_until="load", timeout=20000)
                        time.sleep(0.3)
                    if spec.get("mock_html") is not None:
                        page.evaluate("(h) => { document.body.innerHTML = h; window.scrollTo(0,0); }", spec["mock_html"])
                    if spec.get("mock_js"):
                        page.evaluate(spec["mock_js"])
                    time.sleep(0.3)
                    name, marks, ok = shoot(page, spec, spec.get("changed"), spec.get("focus_after", spec.get("focus")),
                                            os.path.join(args.out, spec["name"] + "-after.png"))
                    rec["after"] = {"file": name, "marks": marks}
                    if not ok:
                        rec["warning"] = (rec.get("warning", "") + "; a changed mark matched nothing").strip("; ")
                ctx.close()
            except Exception as e:  # noqa: BLE001
                rec["error"] = str(e)[:300]
            manifest.append(rec)
            print(json.dumps(rec))
        browser.close()
    srv.shutdown()
    tc.tearDown()
    with open(os.path.join(args.out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)


if __name__ == "__main__":
    main()
