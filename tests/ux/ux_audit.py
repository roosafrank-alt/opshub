#!/usr/bin/env python3
"""OpsHub design review: opens every page in a real browser at phone and
desktop size, as each kind of account, and records layout problems plus
screenshots.

Runs the real app against a THROWAWAY database filled with realistic sample
data (never the live one), with all outside calls faked, same as the unit
tests. Safe to run anywhere.

    python3 tests/ux/ux_audit.py                     # everything
    python3 tests/ux/ux_audit.py --pages /scan /shop # just these pages
    python3 tests/ux/ux_audit.py --roles tech cfi    # just these accounts
    python3 tests/ux/ux_audit.py --no-shots          # checks only, no screenshots

Output (default ux-report/): report.json, summary.md, and screenshots named
<role>/<page>-<phone|desktop>.png.

Automatic checks, per page and screen size:
  sideways_scroll  the whole page scrolls sideways (something is too wide)
  offscreen        elements poking past the right edge of the screen
  small_targets    buttons, links and fields under 32px tall or wide (phone)
  ios_zoom_inputs  text fields under 16px font; iPhone zooms in when tapped (phone)
  tiny_text        visible text under 12px (phone)
  clipped_text     text cut off inside its box
  unlabeled_inputs form fields with no label or placeholder
  js_errors        JavaScript errors on the page
  broken_images    images that didn't load
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
TESTS = os.path.dirname(HERE)
sys.path.insert(0, TESTS)

import harness  # noqa: E402  (points the app at a temp DB and fakes outside calls)
from harness import OpsHubTestCase, seed_everything, seed_row, flask_app, PASSWORD  # noqa: E402
import db  # noqa: E402
import test_route_crawl as crawl  # noqa: E402

PHONE = dict(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
             user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
DESKTOP = dict(viewport={"width": 1366, "height": 850})

# Who looks at what, on which screen. Techs and students mostly use phones on
# the shop floor / flight line; admins use both.
DEFAULT_PLAN = {
    "master": ["phone", "desktop"],
    "shop_admin": ["desktop"],
    "tech": ["phone"],
    "cfi": ["phone", "desktop"],
    "flight_student": ["phone"],
    "customer": ["phone", "desktop"],
}

SKIP_PATTERNS = [r"^/api/", r"\.csv$", r"/export", r"/logout", r"^/static/", r"push-sw\.js", r"/file$",
                 r"/excerpt$"]


class _Seeder(OpsHubTestCase):
    def runTest(self):  # pragma: no cover
        pass


def seed_realistic(tc):
    """seed_everything() plus enough realistic records that lists, tables and
    dashboards look the way they do in daily use (long names, many rows)."""
    ids = seed_everything(tc)
    names = [
        ("Champion REM38E Spark Plug", "Ignition", 24, 18.95), ("Oil Filter CH48110-1", "Engine", 12, 21.40),
        ("Aeroshell W100 Plus 15W-50 (qt)", "Oil", 40, 11.25), ("Cleveland Brake Lining 066-10500", "Brakes", 8, 14.60),
        ("Tire 6.00-6 6-ply Flight Custom III", "Wheels & Tires", 4, 189.00), ("Inner Tube 6.00-6", "Wheels & Tires", 6, 42.10),
        ("Safety Wire .032 (1 lb spool)", "Hardware", 3, 27.80), ("AN960-10L Washer (bag of 100)", "Hardware", 1, 9.50),
        ("Vacuum Pump 215CC Dry Air", "Instruments", 1, 612.00), ("ELT Battery Pack Artex 452-0133", "Avionics", 2, 145.00),
        ("Fuel Sump Drain Valve", "Fuel", 5, 33.70), ("Nav Light Bulb A7512-12", "Electrical", 10, 6.90),
    ]
    conn = db.get_db()
    for i, (n, cat, q, cost) in enumerate(names):
        seed_row(conn, "parts", barcode=f"SHOP-UX{i:04d}", name=n, category=cat, location=f"Bin {chr(65 + i % 6)}{i}",
                 unit="ea", qty_on_hand=q, reorder_point=2 if i % 4 else q + 1, unit_cost=cost, sell_price=round(cost * 1.3, 2),
                 supplier="Aircraft Spruce" if i % 2 else "Chief Aircraft", created_at=db.now_iso(), updated_at=db.now_iso())
    for tag, name in (("N4729K", "Cessna 172S Skyhawk"), ("N81PA", "Piper PA-28-181 Archer III"), ("N2231Q", "Cessna 152")):
        # The two rental trainers are Flight School planes (they show on Schedule / Log a Flight);
        # N4729K is a customer's plane in for maintenance.
        flight = dict(is_flight_asset=1, hobbs_hours=1000.0, tach_hours=850.0) if tag != "N4729K" else {}
        aid = seed_row(conn, "assets", tag=tag, name=name, current_hours=2310.4, created_at=db.now_iso(), updated_at=db.now_iso(), **flight)
        if tag == "N4729K":  # the sample customer owns one plane
            conn.execute("INSERT INTO customer_assets (customer_id, asset_id) VALUES (?, ?)", (tc.customer_id, aid))
        seed_row(conn, "maintenance_items", asset_id=aid, name="100-hour inspection")
        seed_row(conn, "plane_squawks", asset_id=aid, notes="Left main tire showing cord on outboard side, needs replacement before next flight")
    conn.commit()
    conn.close()
    for n in ("Annual Inspection - N4729K", "Prop strike teardown and IRAN - N81PA", "Avionics upgrade (GTN 650Xi)"):
        tc.make_project(name=n)
    return ids


class _Closing:
    def __init__(self, pw): self.pw = pw
    def __enter__(self): return self.pw
    def __exit__(self, *a): self.pw.stop()


CDN_CACHE = os.path.join(HERE, "cdn-cache")
_TYPES = {".css": "text/css", ".js": "application/javascript", ".woff2": "font/woff2", ".woff": "font/woff",
          ".png": "image/png"}
MISSING_CDN = set()


def _serve_cdn(route):
    """Answer requests for the app's CDN libraries from the local cache."""
    from urllib.parse import urlparse
    u = urlparse(route.request.url)
    local = os.path.join(CDN_CACHE, u.netloc, u.path.lstrip("/"))
    if os.path.isfile(local):
        with open(local, "rb") as f:
            body = f.read()
        return route.fulfill(status=200, body=body, headers={
            "content-type": _TYPES.get(os.path.splitext(local)[1], "application/octet-stream"),
            "access-control-allow-origin": "*"})
    if u.netloc.startswith("cdn"):
        MISSING_CDN.add(u.netloc + u.path)
    return route.abort()


def _skip(path):
    return any(re.search(p, path) for p in SKIP_PATTERNS)


def list_pages(ids, only=None):
    pages = []
    for rule in crawl._get_rules():
        url = crawl._build_url(rule, ids)
        if _skip(url):
            continue
        if only and not any(url == o or url.startswith(o.rstrip("/") + "/") for o in only):
            continue
        pages.append(url)
    return sorted(set(pages))


CHECK_JS = r"""
(isPhone) => {
  const W = window.innerWidth, out = {};
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const scrollsX = el => { for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX; if (o === 'auto' || o === 'scroll' || o === 'hidden') return true; } return false; };
  const label = el => {
    const t = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || el.id || '').trim();
    const cls = (el.className && typeof el.className === 'string') ? '.' + el.className.trim().split(/\s+/).slice(0,2).join('.') : '';
    return el.tagName.toLowerCase() + cls + (t ? ' "' + t.slice(0, 40).replace(/\s+/g, ' ') + '"' : '');
  };
  out.sideways_scroll = document.documentElement.scrollWidth > W + 2 ? document.documentElement.scrollWidth - W : 0;
  const all = [...document.body.querySelectorAll('*')];
  out.offscreen = all.filter(el => vis(el) && el.getBoundingClientRect().right > W + 2 && !scrollsX(el)
      && !(el.parentElement && el.parentElement.getBoundingClientRect().right > W + 2)).slice(0, 8).map(label);
  const inter = [...document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, [role=button], summary')].filter(vis);
  out.small_targets = isPhone ? inter.filter(el => { const r = el.getBoundingClientRect();
      if (el.tagName === 'A' && getComputedStyle(el).display === 'inline' && el.closest('p, li, td')) return false; // links inside text
      return r.height < 32 || r.width < 32; }).map(label) : [];
  out.ios_zoom_inputs = isPhone ? [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]), select, textarea')]
      .filter(el => vis(el) && parseFloat(getComputedStyle(el).fontSize) < 16).map(label) : [];
  const texts = new Set();
  if (isPhone) { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) { const n = w.currentNode, p = n.parentElement;
      if (p && n.textContent.trim().length > 2 && vis(p) && parseFloat(getComputedStyle(p).fontSize) < 12) texts.add(label(p)); } }
  out.tiny_text = [...texts].slice(0, 10);
  out.clipped_text = all.filter(el => vis(el) && el.children.length === 0 && (el.innerText || '').trim()
      && el.scrollWidth > el.clientWidth + 2 && getComputedStyle(el).overflowX === 'hidden'
      && getComputedStyle(el).textOverflow !== 'ellipsis').slice(0, 8).map(label);
  out.unlabeled_inputs = [...document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea')]
      .filter(el => vis(el) && !el.labels?.length && !el.getAttribute('aria-label') && !el.getAttribute('placeholder') && !el.closest('label'))
      .slice(0, 8).map(label);
  out.broken_images = [...document.images].filter(i => i.complete && i.naturalWidth === 0 && vis(i)).map(i => i.getAttribute('src')).slice(0, 5);
  out.page_height = document.documentElement.scrollHeight;
  out.title = document.title;
  return out;
}
"""

SEVERE = ("sideways_scroll", "offscreen", "js_errors", "broken_images")


def slug(path):
    return re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_") or "home"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="ux-report")
    ap.add_argument("--pages", nargs="*")
    ap.add_argument("--roles", nargs="*")
    ap.add_argument("--no-shots", action="store_true")
    ap.add_argument("--port", type=int, default=5099)
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server

    tc = _Seeder()
    tc.setUp()
    harness.GC_AFTER_REQUEST[0] = False
    ids = seed_realistic(tc)
    tc.exec("UPDATE users SET tour_seen_shop = 1, tour_seen_flight = 1")  # no guided-tour popup over every page
    pages = list_pages(ids, args.pages)
    plan = {r: v for r, v in DEFAULT_PLAN.items() if not args.roles or r in args.roles}

    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    srv = make_server("127.0.0.1", args.port, flask_app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{args.port}"
    os.makedirs(args.out, exist_ok=True)

    results = []
    exe = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
    fakes = {k: getattr(subprocess, k) for k in harness.REAL_SUBPROCESS}
    for k, v in harness.REAL_SUBPROCESS.items():  # real process launcher, only to start the browser
        setattr(subprocess, k, v)
    pw = sync_playwright().start()
    for k, v in fakes.items():  # back to fakes: the app can never run commands
        setattr(subprocess, k, v)
    with _Closing(pw) as p:
        launch = dict(args=["--no-sandbox"])
        if os.path.exists(exe):
            launch["executable_path"] = exe
        browser = p.chromium.launch(**launch)
        for role, sizes in plan.items():
            for size in sizes:
                ctx = browser.new_context(**(PHONE if size == "phone" else DESKTOP))
                # The app loads Bootstrap etc. from CDNs, which a sandbox may block.
                # Serve the exact same versions from tests/ux/cdn-cache so pages look
                # the way they do for real users; anything else external (map tiles,
                # YouTube) fails fast.
                ctx.route(re.compile(r"^https?://(?!127\.0\.0\.1)"), _serve_cdn)
                user = "owner@example.com" if role == "customer" else role
                ctx.request.post(base + "/", form={"username": user, "password": PASSWORD, "remember": "on"})
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)[:200]))
                seen_final = set()
                for path in pages:
                    errors.clear()
                    try:
                        resp = page.goto(base + path, wait_until="load", timeout=20000)
                    except Exception as e:  # noqa: BLE001
                        results.append(dict(role=role, size=size, path=path, error=str(e)[:200]))
                        continue
                    final = page.url.replace(base, "")
                    if final.split("?")[0] != path.split("?")[0] or (resp and resp.status >= 400):
                        continue  # redirected (no access / login) or error page - not this role's page
                    if final in seen_final:
                        continue
                    seen_final.add(final)
                    time.sleep(0.15)
                    try:
                        r = page.evaluate(CHECK_JS, size == "phone")
                    except Exception as e:  # noqa: BLE001
                        r = {"error": str(e)[:200]}
                    r["js_errors"] = [e for e in errors if "Failed to load" not in e][:5]
                    r.update(role=role, size=size, path=path)
                    if not args.no_shots:
                        shot = os.path.join(args.out, role, f"{slug(path)}-{size}.png")
                        os.makedirs(os.path.dirname(shot), exist_ok=True)
                        try:
                            page.screenshot(path=shot, full_page=True)
                            r["screenshot"] = os.path.relpath(shot, args.out)
                        except Exception:  # noqa: BLE001
                            pass
                    results.append(r)
                ctx.close()
        browser.close()
    srv.shutdown()
    tc.tearDown()

    with open(os.path.join(args.out, "report.json"), "w") as f:
        json.dump(results, f, indent=1)

    # Human summary: worst first.
    def score(r):
        return (bool(r.get("error")) * 100 + sum(bool(r.get(k)) for k in SEVERE) * 10
                + len(r.get("small_targets", [])) + len(r.get("ios_zoom_inputs", [])))
    lines = [f"# OpsHub design review - {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"{len(results)} page views checked ({len(pages)} pages x roles x screen sizes).", ""]
    for r in sorted(results, key=score, reverse=True):
        probs = []
        if r.get("error"):
            loop = "TOO_MANY_REDIRECTS" in r["error"]
            probs.append("REDIRECT LOOP (browser error page)" if loop else f"didn't load: {r['error'].splitlines()[0]}")
        if r.get("sideways_scroll"): probs.append(f"page scrolls sideways by {r['sideways_scroll']}px")
        for k, name in (("offscreen", "off the right edge"), ("js_errors", "JavaScript errors"),
                        ("broken_images", "broken images"), ("small_targets", "tap targets under 32px"),
                        ("ios_zoom_inputs", "fields that make iPhone zoom in"), ("tiny_text", "text under 12px"),
                        ("clipped_text", "clipped text"), ("unlabeled_inputs", "unlabeled fields")):
            v = r.get(k)
            if v:
                probs.append(f"{len(v)} {name}: " + "; ".join(map(str, v[:4])))
        if probs:
            lines.append(f"## {r['path']} ({r['role']}, {r['size']})" + (f" - {r['screenshot']}" if r.get("screenshot") else ""))
            lines += [f"- {p}" for p in probs] + [""]
    with open(os.path.join(args.out, "summary.md"), "w") as f:
        f.write("\n".join(lines))
    bad = sum(1 for r in results if any(r.get(k) for k in SEVERE) or r.get("error"))
    if MISSING_CDN:
        print("Note: these CDN files aren't in tests/ux/cdn-cache, so those parts rendered without them:",
              ", ".join(sorted(MISSING_CDN)))
    print(f"Checked {len(results)} page views; {bad} with serious layout problems. Report: {args.out}/summary.md")


if __name__ == "__main__":
    main()
