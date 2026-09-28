#!/usr/bin/env python3
"""Before/after proof for an Idea Queue idea, so Frank doesn't have to log in
as each account to see that a change works.

It walks through the change in a real browser, as each account involved,
twice: once on the code WITHOUT the change (before) and once WITH it (after),
on the same throwaway sample data as ux_audit.py (never the live database,
outside calls faked). Checks run in order on one database, so a later account
sees what an earlier one did (a student books a lesson, then the CFI sees it).

    python3 tests/ux/verify_idea.py spec.json --out /tmp/proof --before <sha> [--after <sha>]

--before is the commit without the change (usually <idea commit>^), --after the
commit with it (default HEAD). Each is checked out in a temporary git worktree
with THIS checkout's tests/ copied over it, so both runs use the same tool and
sample data. Pass --after-here to run the after side on this checkout as is.

spec.json:
  {
    "checks": [                               # run in this order, on one database
      {
        "role": "flight_student",             # master | shop_admin | tech | cfi | flight_student | customer
        "size": "phone",                      # phone | desktop
        "sql": ["UPDATE ..."],                # optional: set up sample data first
        "steps": [                            # the Walker steps from tests/ux/journeys.py
          ["start", "/flight/schedule"],      #   paths and values may use {project} {asset} {student} ... ids
          ["tap", "Book"],
          ["fill", "Notes", "Pattern work"],
          ["tap", "Save"],
          ["see", "Booked"],                  #   must be on screen (a failed "see" fails the check)
          ["shot", {"caption": "Student sees the booking",   # a picture here; optional:
                    "focus": "#booking",                      #   crop (CSS selector, "full", or omit
                    "changed": [{"sel": "#booking .badge",    #   for the top screenful)
                                 "note": "New Booked badge"}]}]  # green marks on the AFTER picture
        ]
      },
      {"role": "cfi", "size": "desktop", "steps": [["start", "/"], ["see", "Pattern work"], ["shot", {"caption": "CFI sees it"}]]}
    ]
  }
A check with no "shot" step gets one picture at its end. On the BEFORE side a
step that can't be done (the button doesn't exist yet) is expected: the
picture is taken where it stopped. On the AFTER side it fails the check.

Output in --out: before-<c>-<s>.jpg / after-<c>-<s>.jpg and result.json:
  {"ok": true|false, "summary": "...",
   "checks": [{"role", "size", "ok", "stuck_before", "stuck_after", "errors_after",
               "shots": [{"caption", "before": {"file", "marks"}|null, "after": {"file", "marks"}|null}]}]}
"ok" is true only if every AFTER step ran, every "see" was found, and no page
failed to load or threw a JavaScript error.
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


# ------------------------------------------------------------ parent: two runs
def _free_port():
    """Several helpers may run this at once; each walk-through gets its own port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _git(*a, cwd=REPO):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _run_side(label, rev, spec, out, port):
    """Check out rev in a temporary worktree, lay this checkout's tests/ over
    it, and run the walk there. rev None = this checkout as is."""
    wt = None
    cwd = REPO
    if rev:
        wt = tempfile.mkdtemp(prefix=f"verify-{label}-")
        os.rmdir(wt)
        _git("worktree", "add", "--detach", wt, rev)
        shutil.copytree(os.path.join(REPO, "tests"), os.path.join(wt, "tests"), dirs_exist_ok=True)
        cwd = wt
    try:
        env = dict(os.environ)
        vendor = os.path.join(cwd, "vendor")
        if os.path.isdir(vendor):
            env["PYTHONPATH"] = vendor + os.pathsep + env.get("PYTHONPATH", "")
        p = subprocess.run([sys.executable, os.path.join(cwd, "tests", "ux", "verify_idea.py"), spec, "--out", out,
                            "--side", label, "--port", str(port)], cwd=cwd, env=env, capture_output=True, text=True)
        man = os.path.join(out, f"{label}.json")
        if p.returncode or not os.path.exists(man):
            tail = (p.stderr or p.stdout).strip().splitlines()[-5:]
            return {"crashed": " / ".join(tail)[:400] or f"exit {p.returncode}"}
        with open(man) as f:
            return json.load(f)
    finally:
        if wt:
            subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=REPO, capture_output=True)


def _combine(spec, before, after):
    checks, all_ok, lines = [], True, []
    for i, c in enumerate(spec["checks"]):
        b = (before.get("checks") or [{}] * (i + 1))[i] if "checks" in before else {}
        a = (after.get("checks") or [{}] * (i + 1))[i] if "checks" in after else {}
        n = max(len(b.get("shots", [])), len(a.get("shots", [])))
        shots = []
        for k in range(n):
            bs = b["shots"][k] if k < len(b.get("shots", [])) else None
            as_ = a["shots"][k] if k < len(a.get("shots", [])) else None
            cap = (as_ or bs or {}).get("caption", "")
            shots.append({"caption": cap,
                          "before": bs and {"file": bs["file"], "marks": bs.get("marks", [])},
                          "after": as_ and {"file": as_["file"], "marks": as_.get("marks", [])}})
        errs = a.get("errors", [])
        ok = bool(a) and not a.get("stuck") and not errs
        all_ok &= ok
        checks.append({"role": c.get("role", "master"), "size": c.get("size", "phone"), "ok": ok,
                       "stuck_before": b.get("stuck"), "stuck_after": a.get("stuck"), "errors_after": errs, "shots": shots})
        who = f"{c.get('role', 'master')} ({c.get('size', 'phone')})"
        lines.append(f"{'OK  ' if ok else 'FAIL'} check {i + 1} as {who}"
                     + (f": {a.get('stuck')}" if a.get("stuck") else "") + (f": {'; '.join(errs)}" if errs else ""))
    if after.get("crashed"):
        all_ok = False
        lines.append("FAIL the AFTER run crashed: " + after["crashed"])
    if before.get("crashed"):
        lines.append("note: the BEFORE run crashed (no before pictures): " + before["crashed"])
    return {"ok": all_ok, "summary": "\n".join(lines), "checks": checks}


def parent(args):
    spec_path = os.path.abspath(args.spec)
    with open(spec_path) as f:
        spec = json.load(f)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    before = _run_side("before", _git("rev-parse", args.before), spec_path, out, _free_port()) if args.before else {}
    after_rev = None if args.after_here else _git("rev-parse", args.after or "HEAD")
    after = _run_side("after", after_rev, spec_path, out, _free_port())
    res = _combine(spec, before, after)
    with open(os.path.join(out, "result.json"), "w") as f:
        json.dump(res, f, indent=1)
    print(res["summary"])
    print(("PASSED" if res["ok"] else "FAILED") + f" - pictures and result.json in {out}")
    return 0 if res["ok"] else 1


# ------------------------------------------------------- child: one walk-through
def child(args):
    sys.path.insert(0, HERE)
    import ux_audit as ux  # noqa: E402  (harness, temp DB, sample data, CDN cache, screen sizes)
    from ux_audit import harness, flask_app, PASSWORD  # noqa: E402
    from flow_audit import Walker  # noqa: E402
    from fix_preview import shoot  # noqa: E402
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server

    with open(args.spec) as f:
        spec = json.load(f)
    tc = ux._Seeder()
    tc.setUp()
    harness.GC_AFTER_REQUEST[0] = False
    ids = ux.seed_realistic(tc)
    tc.exec("UPDATE users SET tour_seen_shop = 1, tour_seen_flight = 1")
    fmt = lambda s: s.format(**ids) if isinstance(s, str) else s  # noqa: E731

    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    srv = make_server("127.0.0.1", args.port, flask_app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{args.port}"

    fakes = {k: getattr(subprocess, k) for k in harness.REAL_SUBPROCESS}
    for k, v in harness.REAL_SUBPROCESS.items():  # the real launcher, only to start the browser
        setattr(subprocess, k, v)
    pw = sync_playwright().start()
    for k, v in fakes.items():
        setattr(subprocess, k, v)

    class QuietWalker(Walker):
        def _shot(self, label):  # no picture per step; just remember where it was
            self.log.append(dict(step=label, url=self.page.url.replace(self.base, "")))

    side, results = args.side, []
    with ux._Closing(pw) as p:
        launch = dict(args=["--no-sandbox"])
        exe = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
        if os.path.exists(exe):
            launch["executable_path"] = exe
        browser = p.chromium.launch(**launch)
        for ci, c in enumerate(spec["checks"]):
            rec = {"shots": [], "errors": [], "stuck": None}
            for q in c.get("sql", []):
                tc.exec(fmt(q))
            role = c.get("role", "master")
            ctx = browser.new_context(**(ux.PHONE if c.get("size", "phone") == "phone" else ux.DESKTOP))
            ctx.route(re.compile(r"^https?://(?!127\.0\.0\.1)"), ux._serve_cdn)
            user = "owner@example.com" if role == "customer" else role
            ctx.request.post(base + "/", form={"username": user, "password": PASSWORD, "remember": "on"})
            page = ctx.new_page()
            page.on("pageerror", lambda e: rec["errors"].append("JavaScript error: " + str(e)[:150]))
            page.on("response", lambda r: r.request.is_navigation_request() and r.status >= 500
                    and rec["errors"].append(f"server error {r.status} on {r.url.replace(base, '')}"))
            w = QuietWalker(page, base, args.out, f"{side}-{ci}")
            steps = [s for s in c.get("steps", [])]
            if not any(s[0] == "shot" for s in steps):
                steps.append(["shot", {"caption": c.get("caption", "")}])
            pending = [s[1] for s in steps if s[0] == "shot"]

            def take(opts, note=None):
                k = len(rec["shots"])
                name, marks, _ = shoot(page, opts, opts.get("changed") if side == "after" and not note else None,
                                       opts.get("focus") if not note else None,
                                       os.path.join(args.out, f"{side}-{ci}-{k}.png"))
                cap = opts.get("caption", "")
                rec["shots"].append({"file": name, "marks": marks, "caption": cap + (f" ({note})" if note else "")})

            for s in steps:
                verb, rest = s[0], [fmt(x) for x in s[1:]]
                try:
                    if verb == "shot":
                        time.sleep(0.2)
                        take(s[1]); pending.pop(0)
                    elif verb in ("start", "tap", "fill", "see", "scan"):
                        getattr(w, verb)(*rest)
                    else:
                        raise ValueError(f"unknown step '{verb}'")
                except Exception as e:  # noqa: BLE001
                    rec["stuck"] = f"step {steps.index(s) + 1} ({verb} {' '.join(map(str, rest))[:60]}): " + str(e).split("\n")[0][:160]
                    note = "stopped here: couldn't " + verb + (" " + str(rest[0])[:40] if rest else "")
                    for opts in pending:  # the pictures it didn't get to show where it stopped
                        try:
                            take(opts, note)
                        except Exception:  # noqa: BLE001
                            pass
                    break
            rec["errors"] = sorted(set(rec["errors"]))[:5]
            results.append(rec)
            ctx.close()
        browser.close()
    srv.shutdown()
    tc.tearDown()
    with open(os.path.join(args.out, f"{side}.json"), "w") as f:
        json.dump({"checks": results}, f, indent=1)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--out", default="verify-proof")
    ap.add_argument("--before", help="commit without the change (e.g. <idea commit>^)")
    ap.add_argument("--after", help="commit with the change (default HEAD)")
    ap.add_argument("--after-here", action="store_true", help="run the after side on this checkout as is")
    ap.add_argument("--side", choices=["before", "after"], help=argparse.SUPPRESS)  # internal: one walk-through
    ap.add_argument("--port", type=int, default=5096, help=argparse.SUPPRESS)  # internal: set per walk-through
    args = ap.parse_args()
    return child(args) if args.side else parent(args)


if __name__ == "__main__":
    sys.exit(main())
