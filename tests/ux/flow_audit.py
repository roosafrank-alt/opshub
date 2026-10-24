#!/usr/bin/env python3
"""OpsHub navigation and flow review: how easy is it to get around?

Three reports, all against a throwaway database with realistic sample data:

1. APP MAP (map.md): every link between pages, for each kind of account.
   Counts the taps from the home screen to every page. Opening a dropdown or
   the phone menu costs an extra tap. Lists pages that are hard to reach (4+
   taps), pages the account can open but that nothing links to, and dead-end
   pages with no way forward except the top menu.

2. SHOULD-BE-CLICKABLE (clickable.md): places where a plane's tail number, a
   part, a project, a customer or a student is shown as plain text, with no
   link to its own page.

3. TASK WALKTHROUGHS (journeys.md + screenshots): does real daily jobs from
   tests/ux/journeys.py in a real browser, clicking like a person would, and
   counts taps, page loads, typing and scrolling. A step that can't be found
   is reported, since that's usually a flow problem.

    python3 tests/ux/flow_audit.py --out /tmp/flow
    python3 tests/ux/flow_audit.py --out /tmp/flow --only journeys --journey squawks
"""
import argparse
import heapq
import json
import os
import re
import subprocess
import sys
import threading
from collections import defaultdict
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ux_audit as UX  # noqa: E402  (also sets up the test harness + temp DB)
from ux_audit import harness, flask_app, PASSWORD  # noqa: E402
import db  # noqa: E402

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

NOT_A_PAGE = re.compile(r"^(/api/|/static/)|\.(csv|js|json|png|jpe?g|pdf)$|/logout|/export|/print|/label$|/file$|/excerpt$")
MAP_ROLES = ["master", "shop_admin", "tech", "cfi", "flight_student", "customer"]


def norm(path):
    """/assets/12/edit -> /assets/<id>/edit so the same kind of page groups together."""
    return re.sub(r"/\d+(?=/|$)", "/<id>", path.split("?")[0].split("#")[0].rstrip("/") or "/")


# ---------------------------------------------------------------- 1. app map
def links_on(html):
    """[(path, tap_cost, link_text)] for every same-site link on a page. A link
    inside a dropdown menu or the collapsible top menu costs 2 taps."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")) or href.startswith("http") and "127.0.0.1" not in href:
            continue
        path = urlparse(href).path or "/"
        if NOT_A_PAGE.search(path):
            continue
        in_dropdown = a.find_parent(class_="dropdown-menu") is not None
        in_nav = a.find_parent("nav") is not None or a.find_parent(class_=re.compile(r"\bnavbar\b")) is not None
        cost = 2 if in_dropdown else 1
        text = " ".join(a.get_text(" ", strip=True).split())[:50] or a.get("title", "") or a.get("aria-label", "")
        out.append((path, cost, text, in_nav))
    return out


def app_map(tc, roles, out_dir):
    lines = ["# OpsHub app map: taps from the home screen", "",
             "Taps counted from the home screen after logging in. A dropdown or top-menu item costs 2 taps "
             "(open the menu, then tap). On a phone, add 1 tap for any top-menu item (the ☰ button). "
             "Links are counted from what each account actually sees.", ""]
    data = {}
    all_routes = {norm(UX.crawl._build_url(r, tc._ids)) for r in UX.crawl._get_rules()}
    for role in roles:
        client = tc.login(role)
        dist, via, inbound, outlinks, reachable = {"/": 0}, {"/": None}, defaultdict(set), {}, set()
        heap = [(0, "/")]
        while heap:
            d, path = heapq.heappop(heap)
            if d > dist.get(path, 99):
                continue
            resp = client.get(path)
            if resp.status_code != 200 or "text/html" not in resp.content_type:
                continue
            reachable.add(path)
            links = links_on(resp.get_data(as_text=True))
            outlinks[path] = links
            for target, cost, text, _nav in links:
                inbound[norm(target)].add(norm(path))
                nd = d + cost
                if nd < dist.get(target, 99):
                    dist[target] = nd
                    via[target] = (path, text)
                    heapq.heappush(heap, (nd, target))
        # group by page type, keep the easiest example of each
        best = {}
        for p in reachable:
            k = norm(p)
            if k not in best or dist[p] < dist[best[k]]:
                best[k] = p
        # pages this role CAN open (200) that no link leads to
        openable = set()
        for k in all_routes:
            if NOT_A_PAGE.search(k):
                continue
            real = UX.crawl._build_url(next(r for r in UX.crawl._get_rules() if norm(UX.crawl._build_url(r, tc._ids)) == k), tc._ids)
            r = client.get(real)
            if r.status_code == 200 and "text/html" in r.content_type:
                openable.add(k)
        orphans = sorted(openable - set(best))

        def route_to(p):
            steps, cur = [], p
            while via.get(cur):
                prev, text = via[cur]
                steps.append(text or norm(cur))
                cur = prev
            return " → ".join(reversed(steps))

        rows = sorted(((dist[p], k, route_to(p)) for k, p in best.items()), key=lambda x: (x[0], x[1]))
        hard = [r for r in rows if r[0] >= 4]
        dead = sorted(k for k, p in best.items()
                      if not [t for t, _, _, nav in outlinks.get(p, []) if not nav and norm(t) != k])
        data[role] = dict(pages=[dict(taps=t, page=k, route=r) for t, k, r in rows], hard_to_reach=[r[1] for r in hard],
                          not_linked=orphans, dead_ends=dead)
        lines += [f"## {role}", "", f"{len(rows)} kinds of page reachable by tapping. "
                  f"{len(hard)} take 4+ taps. {len(orphans)} can only be opened by typing the address.", ""]
        if hard:
            lines += ["**Hard to reach (4+ taps):**"] + [f"- {k}: {t} taps ({r})" for t, k, r in hard] + [""]
        if orphans:
            lines += ["**Nothing links here (typed address only):**"] + [f"- {k}" for k in orphans] + [""]
        if dead:
            lines += ["**Dead ends (no links forward except the top menu):**"] + [f"- {k}" for k in dead[:25]] + [""]
        lines += ["<details><summary>Every page, easiest route</summary>", ""] + \
                 [f"- {t} taps: {k}  ({r})" for t, k, r in rows] + ["", "</details>", ""]
    with open(os.path.join(out_dir, "map.md"), "w") as f:
        f.write("\n".join(lines))
    with open(os.path.join(out_dir, "map.json"), "w") as f:
        json.dump(data, f, indent=1)
    print(f"App map: {out_dir}/map.md")


# ------------------------------------------------------- 2. should be clickable
def entity_links(tc):
    """{label shown on screen: its own page} for records people navigate by."""
    q = tc.q
    ents = {}
    for r in q("SELECT id, tag FROM assets WHERE deleted_at IS NULL" if _has_col(tc, "assets", "deleted_at") else "SELECT id, tag FROM assets"):
        ents[r["tag"]] = f"/assets/{r['id']}"
    for r in q("SELECT id, code, name FROM projects"):
        if r["code"]:
            ents[r["code"]] = f"/projects/{r['id']}"
        ents[r["name"]] = f"/projects/{r['id']}"
    for r in q("SELECT id, name FROM parts"):
        ents[r["name"]] = f"/parts/{r['id']}"
    return {k: v for k, v in ents.items() if k and len(k) >= 4}


def _has_col(tc, table, col):
    return any(r["name"] == col for r in tc.q(f"PRAGMA table_info({table})"))


CLICKABLE_JS = r"""
([ents, here]) => {
  const out = [];
  const interactive = 'a, button, [onclick], [role=button], [data-bs-toggle], [data-href], label, select, option, input, textarea, summary, script, style, title';
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  while (w.nextNode()) {
    const n = w.currentNode, p = n.parentElement, txt = n.textContent;
    if (!p || !txt.trim() || p.closest(interactive)) continue;
    const r = p.getBoundingClientRect(); if (!r.width || !r.height) continue;
    // A row or card that's clickable as a whole counts as clickable.
    const row = p.closest('tr, .card, .list-group-item');
    if (row && (row.getAttribute('onclick') || row.dataset.href || row.querySelector('a.stretched-link'))) continue;
    for (const [label, url] of ents) {
      if (url === here || seen.has(label)) continue;
      const re = new RegExp('(^|[^A-Za-z0-9])' + label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '($|[^A-Za-z0-9])');
      if (re.test(txt)) {
        seen.add(label);
        const heading = /^H[1-6]$/.test(p.tagName) ? ' (in a heading)' : '';
        out.push({label, url, where: p.tagName.toLowerCase() + heading, context: txt.trim().replace(/\s+/g,' ').slice(0, 80)});
      }
    }
  }
  return out;
}
"""


# ------------------------------------------------------- 3. task walkthroughs
class Walker:
    """Clicks through a task like a person: finds things by their visible
    text, opens the ☰ menu or a dropdown when needed (each counts as a
    tap), and records taps, page loads, typing and scrolling."""

    def __init__(self, page, base, shots_dir, name):
        self.page, self.base, self.dir, self.name = page, base, shots_dir, name
        self.taps = self.loads = self.typed = self.scroll_px = 0
        self.log, self.n = [], 0
        page.on("framenavigated", lambda fr: fr == page.main_frame and self._load())

    def _load(self):
        self.loads += 1

    def _shot(self, label):
        self.n += 1
        path = os.path.join(self.dir, f"{self.name}-{self.n:02d}.png")
        try:
            self.page.screenshot(path=path)
        except Exception:  # noqa: BLE001
            path = None
        self.log.append(dict(step=label, url=self.page.url.replace(self.base, ""), taps=self.taps, shot=path and os.path.basename(path)))

    def _visible(self, loc):
        try:
            return loc.first.is_visible()
        except Exception:  # noqa: BLE001
            return False

    def _find(self, text, role=None):
        p = self.page
        if text.startswith(("#", ".", "[")):  # a CSS selector, for controls with no visible label
            loc = p.locator(text)
            return loc.first if loc.count() else None
        cands = []
        exact = re.compile(r"^\s*" + re.escape(text) + r"\s*$", re.I)
        loose = re.compile(re.escape(text), re.I)
        # Plain CSS locators (not accessibility roles) so links hidden in the
        # collapsed phone menu are found too; the walker then opens the menu.
        clickable = p.locator("a[href], button, [role=button], summary")
        for pat in (exact, loose):  # a link labelled exactly this wins over one that merely contains it
            if role:
                cands.append(p.get_by_role(role, name=pat))
            cands += [clickable.filter(has_text=pat)]
        cands += [p.get_by_text(text, exact=True), p.get_by_text(text, exact=False)]
        n_exact = (1 if role else 0) + 1
        # An exact label match wins even if it's tucked in the phone menu (we open
        # the menu for it); only then fall back to anything containing the text.
        for group in (cands[:n_exact], cands[n_exact:]):
            for c in group:
                if c.count() and self._visible(c):
                    return c.first
            for c in group:
                if c.count():
                    return c.first
        return None

    def start(self, path):
        self.page.goto(self.base + path, wait_until="load")
        self.loads = 0
        self._shot(f"start at {path}")

    def tap(self, text, role=None):
        loc = self._find(text, role)
        if loc is None:
            raise LookupError(f"couldn't find anything labelled '{text}' on {self.page.url.replace(self.base, '')}")
        if not self._visible(loc):
            # Hidden in the collapsed phone menu or a dropdown: open it like a person would.
            tog = self.page.locator(".navbar-toggler:visible")
            if tog.count() and not self._visible(loc):
                tog.first.click(); self.taps += 1; self.page.wait_for_timeout(350)
            if not self._visible(loc):
                dd = loc.locator("xpath=ancestor::*[contains(@class,'dropdown')][1]//*[@data-bs-toggle='dropdown']")
                if dd.count():
                    dd.first.click(); self.taps += 1; self.page.wait_for_timeout(250)
        y0 = self.page.evaluate("scrollY")
        loc.scroll_into_view_if_needed()
        self.scroll_px += abs(self.page.evaluate("scrollY") - y0)
        loc.click(); self.taps += 1
        self.page.wait_for_load_state("load")
        self.page.wait_for_timeout(200)
        self._shot(f"tap '{text}'")

    def fill(self, label, value):
        if label.startswith(("#", ".", "[")):
            loc = self.page.locator(label)
        else:
            loc = self.page.get_by_label(re.compile(re.escape(label), re.I))
        if not loc.count() and not label.startswith(("#", ".", "[")):
            loc = self.page.get_by_placeholder(re.compile(re.escape(label), re.I))
        if not loc.count():
            loc = self.page.locator(f"[name='{label}']")
        if not loc.count():
            raise LookupError(f"couldn't find a field for '{label}'")
        y0 = self.page.evaluate("scrollY")
        if loc.first.is_visible():
            loc.first.scroll_into_view_if_needed()
        tag = loc.first.evaluate("e => e.tagName")
        combo = loc.first.locator("xpath=ancestor::div[contains(concat(' ',@class,' '),' ohc ')][1]//input[not(@type='hidden')]")
        if tag == "SELECT" and combo.count():
            # OpsHub's search picker (static/js/combo.js): the real <select> is hidden;
            # a person taps the box, types a few letters and picks the top match.
            box = combo.first
            box.scroll_into_view_if_needed(); box.click(); self.taps += 1  # scroll counted below
            box.fill(value[:12]); self.typed += 1
            self.page.keyboard.press("Enter"); self.taps += 1
            self.page.wait_for_timeout(150)
        elif tag == "SELECT":
            try:
                loc.first.select_option(label=re.compile(re.escape(value), re.I))
            except Exception:  # noqa: BLE001
                loc.first.select_option(value)
            self.taps += 2
        else:
            loc.first.fill(value); self.taps += 1; self.typed += 1
        self.page.wait_for_timeout(100)
        self.scroll_px += abs(self.page.evaluate("scrollY") - y0)
        self._shot(f"fill '{label}'")

    def scan(self, code, box="#scan-input"):
        """A USB/Bluetooth barcode scanner: types the code into the focused scan
        box and presses Enter. Counts as 1 tap (one trigger pull), not typing."""
        loc = self.page.locator(box)
        if not loc.count():
            raise LookupError(f"no scan box '{box}' on {self.page.url.replace(self.base, '')}")
        loc.first.fill(code)
        loc.first.press("Enter"); self.taps += 1
        self.page.wait_for_timeout(700)
        self._shot(f"scan '{code}'")

    def see(self, text):
        loc = self.page.get_by_text(text, exact=False)
        ok = loc.count() and self._visible(loc)
        if ok:
            y0 = self.page.evaluate("scrollY")
            loc.first.scroll_into_view_if_needed()
            self.scroll_px += abs(self.page.evaluate("scrollY") - y0)
        self._shot(f"look for '{text}'" + ("" if ok else " (NOT FOUND)"))
        if not ok:
            raise LookupError(f"'{text}' isn't visible on {self.page.url.replace(self.base, '')}")


def run_journeys(tc, ids, browser, base, out_dir, only=None):
    import journeys
    shots = os.path.join(out_dir, "journeys"); os.makedirs(shots, exist_ok=True)
    results, lines = [], ["# Task walkthroughs", "",
                          "Each daily job done in a real browser, clicking like a person. Taps include opening the ☰ "
                          "menu and dropdowns. Page loads are full page changes. Scroll is how far the person had to scroll.", ""]
    for j in journeys.JOURNEYS:
        if only and j["id"] not in only:
            continue
        ctx = browser.new_context(**(UX.PHONE if j["screen"] == "phone" else UX.DESKTOP))
        ctx.route(re.compile(r"^https?://(?!127\.0\.0\.1)"), UX._serve_cdn)
        user = "owner@example.com" if j["role"] == "customer" else j["role"]
        ctx.request.post(base + "/", form={"username": user, "password": PASSWORD, "remember": "on"})
        page = ctx.new_page()
        w = Walker(page, base, shots, j["id"])
        err = None
        try:
            j["steps"](w, ids)
        except Exception as e:  # noqa: BLE001
            err = str(e).split("\n")[0][:200]
        ctx.close()
        r = dict(id=j["id"], task=j["task"], role=j["role"], screen=j["screen"], taps=w.taps, page_loads=w.loads,
                 fields_typed=w.typed, scroll_px=w.scroll_px, target_taps=j.get("target_taps"), stuck=err, steps=w.log)
        results.append(r)
        verdict = "STUCK: " + err if err else (f"{w.taps} taps" + (f" (target {j['target_taps']})" if j.get("target_taps") else ""))
        lines += [f"## {j['task']} ({j['role']}, {j['screen']})", f"**{verdict}** · {w.loads} page loads · "
                  f"{w.typed} fields typed · scrolled {w.scroll_px}px", ""]
        lines += [f"{i+1}. {s['step']} → {s['url']} ({s['taps']} taps so far)" + (f" [{s['shot']}]" if s['shot'] else "")
                  for i, s in enumerate(w.log)] + [""]
    with open(os.path.join(out_dir, "journeys.md"), "w") as f:
        f.write("\n".join(lines))
    with open(os.path.join(out_dir, "journeys.json"), "w") as f:
        json.dump(results, f, indent=1)
    print(f"Walkthroughs: {out_dir}/journeys.md ({sum(1 for r in results if r['stuck'])} got stuck)")


MIXED_JS = r"""
() => {
  // Rows of look-alike boxes (cards, tiles, stat boxes) where some can be
  // tapped and others can't: people tap the dead ones expecting them to work.
  const out = [];
  for (const row of document.querySelectorAll('.row, .d-flex, .list-group, .card-group')) {
    const kids = [...row.children].filter(k => k.getBoundingClientRect().width > 0);
    if (kids.length < 2) continue;
    const box = k => k.querySelector('.card, .list-group-item, .stat-card') || (k.matches('.card, .list-group-item, .stat-card') ? k : null);
    const tappable = k => !!(k.closest('a[href]') || k.querySelector('a[href].stretched-link') || k.matches('a[href]')
                           || (box(k) && (box(k).closest('a[href]') || box(k).querySelector(':scope > a[href]'))));
    const boxes = kids.filter(box);
    if (boxes.length < 2) continue;
    const t = boxes.filter(tappable), dead = boxes.filter(k => !tappable(k));
    if (t.length && dead.length) out.push({
      tappable: t.map(k => k.innerText.trim().split('\n')[0].slice(0, 40)),
      not_tappable: dead.map(k => k.innerText.trim().split('\n')[0].slice(0, 40))});
  }
  return out.slice(0, 6);
}
"""


def clickable_report(tc, ids, browser, base, out_dir, roles):
    ents = sorted(entity_links(tc).items(), key=lambda kv: -len(kv[0]))
    pages = UX.list_pages(ids)
    lines = ["# Shown as plain text but could be a link", "",
             "Each line is a record (plane, project, part) shown on a page with no link to its own page. "
             "Not every one needs a link: judge by whether a person on that page would want to go there next.", ""]
    found, mixed = defaultdict(list), defaultdict(list)
    for role in roles:
        ctx = browser.new_context(**UX.DESKTOP)
        ctx.route(re.compile(r"^https?://(?!127\.0\.0\.1)"), UX._serve_cdn)
        ctx.request.post(base + "/", form={"username": role, "password": PASSWORD, "remember": "on"})
        page = ctx.new_page()
        for path in pages:
            try:
                resp = page.goto(base + path, wait_until="load", timeout=15000)
            except Exception:  # noqa: BLE001
                continue
            if page.url.replace(base, "").split("?")[0] != path.split("?")[0] or (resp and resp.status >= 400):
                continue
            for hit in page.evaluate(CLICKABLE_JS, [ents, path]):
                found[path].append(dict(role=role, **hit))
            for m in page.evaluate(MIXED_JS):
                mixed[path].append(m)
        ctx.close()
    for path in sorted(found):
        uniq = {(h["label"], h["url"]): h for h in found[path]}
        lines.append(f"## {path}")
        lines += [f"- '{h['label']}' in {h['where']}, could go to {h['url']}: \"{h['context']}\"" for h in uniq.values()]
        lines.append("")
    if mixed:
        lines += ["# Look-alike boxes where only some can be tapped", ""]
        for path in sorted(mixed):
            seen = set()
            for m in mixed[path]:
                key = (tuple(m["tappable"]), tuple(m["not_tappable"]))
                if key in seen:
                    continue
                seen.add(key)
                lines.append(f"- {path}: tappable {m['tappable']}, NOT tappable {m['not_tappable']}")
        lines.append("")
    with open(os.path.join(out_dir, "clickable.md"), "w") as f:
        f.write("\n".join(lines))
    print(f"Should-be-clickable: {out_dir}/clickable.md ({sum(len(v) for v in found.values())} spots)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="flow-report")
    ap.add_argument("--only", nargs="*", choices=["map", "clickable", "journeys"])
    ap.add_argument("--journey", nargs="*", help="journey ids from journeys.py")
    ap.add_argument("--port", type=int, default=5098)
    args = ap.parse_args()
    parts = set(args.only or ["map", "clickable", "journeys"])
    os.makedirs(args.out, exist_ok=True)

    tc = UX._Seeder(); tc.setUp()
    harness.GC_AFTER_REQUEST[0] = False
    ids = UX.seed_realistic(tc)
    tc.exec("UPDATE users SET tour_seen_shop = 1, tour_seen_flight = 1")
    tc._ids = ids

    if "map" in parts:
        app_map(tc, MAP_ROLES, args.out)
    if parts & {"clickable", "journeys"}:
        from playwright.sync_api import sync_playwright
        from werkzeug.serving import make_server
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
        exe = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
        browser = pw.chromium.launch(args=["--no-sandbox"], **({"executable_path": exe} if os.path.exists(exe) else {}))
        try:
            if "clickable" in parts:
                clickable_report(tc, ids, browser, base, args.out, ["master", "cfi"])
            if "journeys" in parts:
                run_journeys(tc, ids, browser, base, args.out, args.journey)
        finally:
            browser.close(); pw.stop(); srv.shutdown()
    tc.tearDown()


if __name__ == "__main__":
    main()
