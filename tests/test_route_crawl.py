"""Crawl EVERY GET page in the app as EVERY kind of account and fail on:

  * any 500 (unhandled exception) - a page crashing for someone
  * any 500 on a record that doesn't exist (should be a clean 404)

This is the broad net: it needs no knowledge of what a page does, so it
automatically covers new pages the moment they're added. A failure message
lists every (role, url) that crashed plus the exception, so one run shows
the whole picture instead of stopping at the first problem.
"""
import re
import traceback

from harness import OpsHubTestCase, ROLES, seed_everything, flask_app, app_module, SIDE_EFFECTS

# Pages that are intentionally not crawled, with the reason.
SKIP_ENDPOINTS = {
    "static": "static files",
    "logout": "clears the session mid-crawl",
    "customer.customer_logout": "clears the session mid-crawl",
    "push_service_worker": "static JS",
}

# Param name -> key into seed_everything()'s ids. Anything not listed gets 1.
PARAM_IDS = {
    "asset_id": "asset", "part_id": "part", "project_id": "project", "order_id": "order",
    "laborer_id": "laborer", "item_id": "maint", "student_id": "student", "cfi_id": "cfi",
    "flight_id": "flight", "scheduled_id": "sched", "section_id": "section",
}
ALL_ROLES = list(ROLES) + ["customer", None]


def _get_rules():
    out = []
    for rule in flask_app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.endpoint in SKIP_ENDPOINTS:
            continue
        out.append(rule)
    return sorted(out, key=lambda r: r.rule)


def _build_url(rule, ids, missing=False):
    def sub(m):
        conv, name = (m.group(1) or ""), m.group(2)
        if conv.startswith("int"):
            return "999999" if missing else str(ids.get(PARAM_IDS.get(name, ""), 1))
        if name == "barcode":
            return "NOPE-404" if missing else "PART-001"
        if name == "code":
            return "99-999" if missing else "26-001"
        if name == "field_key":
            return "name"
        if name == "filename":
            return "x"
        return "x"
    return re.sub(r"<(?:([a-z]+(?:\([^)]*\))?):)?([a-z_]+)>", sub, rule.rule)


def _last_error(app_log_capture):
    return app_log_capture[-1] if app_log_capture else ""


class RouteCrawlTest(OpsHubTestCase):
    def _crawl(self, missing):
        ids = seed_everything(self)
        errors = []
        captured = []

        def on_exc(sender, exception, **extra):
            captured.append("".join(traceback.format_exception(type(exception), exception,
                                                               exception.__traceback__)[-3:]).strip())
        from flask import got_request_exception
        got_request_exception.connect(on_exc, flask_app)
        try:
            rules = _get_rules()
            for role in ALL_ROLES:
                for rule in rules:
                    if missing and "<" not in rule.rule:
                        continue
                    # Fresh login per page so one page that signs out / switches
                    # "view as" can't silently turn the rest of the crawl into
                    # logged-out redirects.
                    client = self.login(role)
                    url = _build_url(rule, ids, missing=missing)
                    captured.clear()
                    resp = client.get(url)
                    if resp.status_code >= 500:
                        errors.append(f"[{role}] GET {url} -> {resp.status_code}\n      {_last_error(captured)}")
        finally:
            got_request_exception.disconnect(on_exc, flask_app)
        return errors

    def test_every_page_renders_for_every_role(self):
        errors = self._crawl(missing=False)
        self.assertEqual(errors, [], f"{len(errors)} page crash(es):\n" + "\n".join(errors))

    def test_missing_records_give_404_not_crash(self):
        errors = self._crawl(missing=True)
        self.assertEqual(errors, [], f"{len(errors)} crash(es) on missing records:\n" + "\n".join(errors))

    def test_flight_only_accounts_never_get_a_shop_redirect_loop(self):
        """QA finding ux-shop-link-redirect-loop: a CFI or flight student
        (no shop_role at all) opening a shop page used to be sent to the
        Shop dashboard, which ALSO turned them away and sent them right
        back - an infinite redirect the browser gave up on with
        "too many redirects" instead of a real page. shop_role_required now
        sends an account with no shop_role to their own Flight School
        dashboard instead (which only needs a login, so there's nowhere
        left to bounce from). This follows redirects for real, so a
        regression here shows up as too many redirects or a non-200 - not
        just a raw 302 that _crawl()'s status>=500 check would miss."""
        ids = seed_everything(self)
        shop_pages = ["/shop", "/squawks", "/assets/%s" % ids["asset"], "/parts",
                      "/parts/%s" % ids["part"], "/projects", "/projects/%s" % ids["project"], "/orders"]
        for role in ("cfi", "flight_student"):
            client = self.login(role)
            for url in shop_pages:
                resp = client.get(url, follow_redirects=True)
                self.assertEqual(resp.status_code, 200, f"[{role}] GET {url} did not land on a real page")
                self.assertEqual(resp.request.path, "/flight/dashboard",
                                  f"[{role}] GET {url} landed on {resp.request.path}, not the Flight dashboard")

    def test_crawl_made_no_outside_calls_that_escaped(self):
        # GET pages should never reboot the Pi / restart the service.
        seed_everything(self)
        for role in ("master", "shop_admin"):
            for rule in _get_rules():
                if "<" not in rule.rule:
                    self.login(role).get(rule.rule)
        bad = [s for s in SIDE_EFFECTS if s[0] == "subprocess" and not _read_only_command(s[1])]
        self.assertEqual(bad, [], "A GET page ran a shell command: %r" % bad)


# Commands a page may run just to READ the Pi's status (the System page shows
# the CPU temperature). Anything else from a GET page - reboot, restart, lp,
# git - is still a failure.
_READ_ONLY_COMMANDS = {("vcgencmd", "measure_temp"), ("vcgencmd", "get_throttled")}


def _read_only_command(args):
    return isinstance(args, (list, tuple)) and tuple(args[:2]) in _READ_ONLY_COMMANDS


class CrawlerSanityTest(OpsHubTestCase):
    """Guards the crawler itself: if logins silently stop working, every
    page just redirects and the crawl 'passes' while testing nothing."""

    def test_master_admin_actually_reaches_pages(self):
        ids = seed_everything(self)
        ok = 0
        rules = _get_rules()
        for rule in rules:
            if self.login("master").get(_build_url(rule, ids)).status_code == 200:
                ok += 1
        self.assertGreater(ok, len(rules) * 0.7,
                           f"master admin only got 200 on {ok}/{len(rules)} pages - login in tests is broken")
