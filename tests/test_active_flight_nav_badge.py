"""QA finding feat-drop-active-flight-tab: the 'Active Flight' tab in the
Fly with Kate! nav (templates/flight/base_flight.html) turns yellow with a
small dark count badge whenever one or more flights are in the air
school-wide, and stays a plain outlined tab when nothing is flying. Counts
every running flight in the school (not just the viewer's own), and a
paused flight still counts (see _flight_active_nav_count in flight.py).
While on the Active Flight page itself, the tab keeps its usual solid-white
"you are here" look with the badge still added."""
from harness import OpsHubTestCase
import db


class ActiveFlightNavBadgeTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?",
                                   (self.users["flight_student"]["id"],))["id"]
        self.asset_id = self.make_asset("N123")

    def start_flight(self, cfi_id=None):
        return self.exec(
            "INSERT INTO flights (asset_id, cfi_id, student_id, started_at, paused_seconds, flight_date) "
            "VALUES (?,?,?,?,0,?)",
            (self.asset_id, cfi_id, self.student_id, db.now_iso(), db.now_iso()[:10]))

    def test_no_badge_and_plain_tab_when_nothing_flying(self):
        self.login("cfi")
        r = self.client.get("/flight/dashboard")
        html = r.data.decode()
        self.assertIn('id="tour-nav-flight-active"', html)
        nav = html[html.index('id="tour-nav-flight-active"') - 200:html.index('id="tour-nav-flight-active"') + 300]
        self.assertNotIn("flight-nav-flying", nav)
        self.assertNotIn("badge", nav)

    def test_tab_turns_yellow_with_count_when_a_flight_is_up(self):
        self.start_flight(self.cfi_id)
        self.login("cfi")
        r = self.client.get("/flight/dashboard")
        html = r.data.decode()
        nav = html[html.index('id="tour-nav-flight-active"') - 200:html.index('id="tour-nav-flight-active"') + 400]
        self.assertIn("flight-nav-flying", nav)
        self.assertIn(">1<", nav)

    def test_count_is_school_wide_and_counts_every_role(self):
        """Two flights up, flown by different CFIs - the count is the same
        for every logged-in role, not just each flight's own CFI."""
        other_cfi_id = self.exec(
            "INSERT INTO cfis (name, username, password_hash, active) VALUES ('Other Cfi','other_cfi','x',1)")
        self.start_flight(self.cfi_id)
        self.start_flight(other_cfi_id)
        for role in ("cfi", "flight_student", "master"):
            self.login(role)
            html = self.client.get("/flight/dashboard").data.decode()
            nav = html[html.index('id="tour-nav-flight-active"') - 200:html.index('id="tour-nav-flight-active"') + 400]
            self.assertIn(">2<", nav, f"expected count 2 for role {role}")

    def test_paused_flight_still_counts(self):
        fid = self.start_flight(self.cfi_id)
        self.exec("UPDATE flights SET paused_at = ? WHERE id = ?", (db.now_iso(), fid))
        self.login("flight_student")
        html = self.client.get("/flight/dashboard").data.decode()
        nav = html[html.index('id="tour-nav-flight-active"') - 200:html.index('id="tour-nav-flight-active"') + 400]
        self.assertIn(">1<", nav)

    def test_ended_flight_does_not_count(self):
        fid = self.start_flight(self.cfi_id)
        self.exec("UPDATE flights SET ended_at = ? WHERE id = ?", (db.now_iso(), fid))
        self.login("cfi")
        html = self.client.get("/flight/dashboard").data.decode()
        nav = html[html.index('id="tour-nav-flight-active"') - 200:html.index('id="tour-nav-flight-active"') + 300]
        self.assertNotIn("flight-nav-flying", nav)

    def test_on_the_active_flight_page_itself_keeps_active_style_plus_badge(self):
        """Point 3: on log/active itself, the tab keeps the ordinary
        solid-white 'you are here' look (the existing 'active' class), with
        the count badge still added on top - not the yellow flying style."""
        self.start_flight(self.cfi_id)
        self.login("cfi")
        html = self.client.get("/flight/log/active").data.decode()
        nav = html[html.index('id="tour-nav-flight-active"') - 200:html.index('id="tour-nav-flight-active"') + 400]
        self.assertIn("active", nav)
        self.assertNotIn("flight-nav-flying", nav)
        self.assertIn(">1<", nav)
