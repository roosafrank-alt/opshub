"""QA fix ux-header-width-rules: every header (Winds Aloft, Fly with Kate!,
Admin, My Account) follows the same rule - words next to the icons whenever
the whole row fits on one line, icon-only otherwise, and the same ~992px
hamburger breakpoint everywhere. The actual fit check runs in the browser
(static/js/nav-fit.js, which compares each button's own height with and
without its label), so this test checks the markup/CSS contract it depends
on: every header loads the script, starts icon-only by default (never a
label caught mid-wrap before the script runs), and now shares one hamburger
breakpoint. Before this fix, Winds Aloft alone collapsed at
navbar-expand-xl (1200px) while the other three used navbar-expand-lg
(992px)."""
import os

from harness import OpsHubTestCase, REPO_ROOT

with open(os.path.join(REPO_ROOT, "static", "css", "style.css")) as _f:
    STYLE_CSS = _f.read()


class HeaderWidthRulesTest(OpsHubTestCase):
    def _assert_header_contract(self, body, bg_marker):
        self.assertIn(bg_marker, body)
        # Every header now collapses to the hamburger at the same breakpoint.
        self.assertIn("navbar-expand-lg", body)
        self.assertNotIn("navbar-expand-xl", body)
        # The header-fit script is loaded (it toggles .hdr-words on <nav>).
        self.assertIn("js/nav-fit.js", body)
        # At least one icon+word button exists for the script to measure.
        self.assertIn('class="nav-label"', body)

    def test_shop_header_follows_the_shared_rule(self):
        c = self.login("master")
        body = c.get("/shop").get_data(as_text=True)
        self._assert_header_contract(body, "bg-dark")

    def test_flight_header_follows_the_shared_rule(self):
        c = self.login("cfi")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        self._assert_header_contract(body, "bg-primary")

    def test_admin_header_follows_the_shared_rule(self):
        c = self.login("master")
        body = c.get("/admin").get_data(as_text=True)
        self._assert_header_contract(body, "admin-nav")
        # "Program Picker" must never wrap onto a second line.
        self.assertIn("admin-program-picker", body)
        # The account button sits in the same boxed scope as the other
        # buttons, so it's the same height as them (not the plain, shorter
        # Bootstrap .nav-link default).
        self.assertIn('class="dropdown admin-nav-boxed"', body)

    def test_account_header_follows_the_shared_rule(self):
        c = self.login("master")
        body = c.get("/account").get_data(as_text=True)
        self._assert_header_contract(body, "account-nav")
        # The account button matches the .btn-program buttons' sizing
        # (same btn/btn-sm classes) instead of the shorter plain .nav-link.
        self.assertIn('class="btn btn-outline-light btn-sm dropdown-toggle"', body)
        # The program buttons no longer wrap onto a second row.
        self.assertNotIn("d-flex flex-wrap align-items-center gap-2", body)

    def test_style_css_hides_labels_by_default_until_the_fit_check_runs(self):
        # Icon-only is the safe default (never a label caught mid-wrap
        # before nav-fit.js has run); .hdr-words (added by the script once
        # the row fits with words) is what shows them again.
        self.assertRegex(STYLE_CSS, r"\.navbar\s+\.nav-label\s*\{[^}]*display:\s*none")
        self.assertIn(".navbar.hdr-words .nav-label", STYLE_CSS)
        # Below the hamburger breakpoint the collapsed menu is a single
        # column, so labels always show there regardless of the JS check.
        self.assertRegex(STYLE_CSS, r"max-width:\s*991\.98px\)\s*\{\s*\.navbar\s+\.nav-label\s*\{[^}]*display:\s*inline")

    def test_nav_fit_script_compares_button_height_with_and_without_label(self):
        # The actual fit test has to be per-button height, not a fixed
        # pixel width, so it adapts to however many buttons an account
        # sees (a CFI's Fly with Kate! header needs far less room than an
        # admin's) instead of guessing one width that fits every account.
        with open(os.path.join(REPO_ROOT, "static", "js", "nav-fit.js")) as f:
            js = f.read()
        self.assertIn("nav-label", js)
        self.assertIn("getBoundingClientRect", js)
        self.assertIn("hdr-words", js)
