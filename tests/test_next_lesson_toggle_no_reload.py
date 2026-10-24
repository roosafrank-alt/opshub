"""QA "your next lesson toggling": the Next Lesson card's Personal/School
toggle (master admins only) used to be plain <a href> links to a fresh
/flight/dashboard page load, which reset the browser's scroll position to
the top. They're now buttons that the dashboard's live-refresh JS fetches
from flight.dashboard_live and swaps into #dashboard-live-region in place -
see the next-scope-btn click handler in dashboard.html - so this exercises
the same endpoint the button now calls."""
from harness import OpsHubTestCase


class NextLessonToggleNoReloadTest(OpsHubTestCase):
    def test_dashboard_live_accepts_next_and_remembers_it_in_session(self):
        c = self.login("master")
        # Flip to "school" via the same endpoint the button fetches (not a
        # full /flight/dashboard navigation).
        body = c.get("/flight/dashboard/live?next=school").get_data(as_text=True)
        self.assertIn("Next Lesson - Whole School", body)
        with c.session_transaction() as s:
            self.assertEqual(s.get("next_scope"), "school")
        # It's remembered for the next dashboard_live poll too, with no
        # ?next= needed (same as the session-remembered behavior of the
        # full /flight/dashboard page).
        body = c.get("/flight/dashboard/live").get_data(as_text=True)
        self.assertIn("Next Lesson - Whole School", body)
        # Flip back.
        body = c.get("/flight/dashboard/live?next=mine").get_data(as_text=True)
        self.assertNotIn("Next Lesson - Whole School", body)
        with c.session_transaction() as s:
            self.assertEqual(s.get("next_scope"), "mine")

    def test_toggle_is_a_button_not_a_link_so_it_never_navigates(self):
        c = self.login("master")
        body = c.get("/flight/dashboard").get_data(as_text=True)
        self.assertIn('class="btn next-scope-btn', body)
        self.assertNotIn('href="/flight/dashboard?next=', body)

    def test_a_non_master_admin_cannot_flip_the_scope(self):
        c = self.login("cfi")
        body = c.get("/flight/dashboard/live?next=school").get_data(as_text=True)
        with c.session_transaction() as s:
            self.assertIsNone(s.get("next_scope"))
        self.assertNotIn('next-scope-btn', body)
