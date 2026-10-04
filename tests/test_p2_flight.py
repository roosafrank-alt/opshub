"""Preview phase 2, flight area: FLY-27, SCHOOL-24 and the app-wide sweeps
(FLY-12 dates, SEAM-15 titles, DESIGN-3/4/8) applied to the flying pages."""
import glob
import os
import re
from datetime import date, timedelta

from harness import OpsHubTestCase
import flight


TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates")


class P2Base(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N20267S")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]


class FinderPrefillTest(P2Base):
    def test_result_link_carries_plane_and_instructor(self):  # FLY-27
        html = self.login("master").get("/flight/schedule/find").get_data(as_text=True)
        links = re.findall(r'finder-slot-pill"[^>]*href="([^"]+)"', html)
        self.assertTrue(links, "the finder found no open slot to click")
        for href in links:
            href = href.replace("&amp;", "&")
            self.assertIn(f"asset_id={self.plane}", href)
            self.assertIn("cfi_id=", href)
            self.assertIn("date=", href)
            self.assertIn("time=", href)

    def test_form_opens_with_them_filled_in(self):  # FLY-27
        day = (date.today() + timedelta(days=2)).isoformat()
        html = self.login("master").get(
            f"/flight/schedule/new?date={day}&time=09:30&asset_id={self.plane}&cfi_id={self.cfi}").get_data(as_text=True)
        self.assertRegex(html, rf'<option value="{self.plane}"[^>]*selected')
        self.assertRegex(html, rf'<option value="{self.cfi}"[^>]*selected')


class ScheduleAndSoloSettingsTest(P2Base):
    def save(self, **data):
        c = self.login("master")
        c.post(f"/flight/planes/{self.plane}/rate", data=data)
        with c.session_transaction() as s:
            return [m for _cat, m in s.get("_flashes", [])]

    def test_button_and_heading_use_the_new_name(self):  # SCHOOL-24
        c = self.login("master")
        self.assertIn("Schedule &amp; Solo settings", c.get("/flight/planes").get_data(as_text=True))
        page = c.get(f"/flight/planes/{self.plane}/rate").get_data(as_text=True)
        self.assertIn("Schedule &amp; Solo settings: N20267S", page)
        self.assertNotIn("Edit Plane", page)

    def test_message_says_what_changed(self):  # SCHOOL-24
        self.exec("UPDATE assets SET solo_allowed = 1, schedule_color = NULL, solo_color = NULL WHERE id = ?", (self.plane,))
        msgs = self.save(solo_allowed="on", color=flight.SCHEDULE_COLORS[0])
        self.assertEqual(msgs, ["N20267S: colors updated."])
        msgs = self.save(color=flight.SCHEDULE_COLORS[0])  # solo box unticked now
        self.assertEqual(msgs, ["N20267S: solo flights no longer allowed."])
        msgs = self.save(solo_allowed="on", color=flight.SCHEDULE_COLORS[1])
        self.assertEqual(msgs, ["N20267S: colors updated and solo flights now allowed."])
        msgs = self.save(solo_allowed="on", color=flight.SCHEDULE_COLORS[1])
        self.assertEqual(msgs, ["N20267S: nothing changed."])


class DateWordsTest(OpsHubTestCase):
    def test_flight_dates_use_the_spelled_month_format(self):  # FLY-12
        self.assertEqual(flight._us_date("2026-10-02"), "Fri, Oct 2, 2026")
        self.assertEqual(flight._us_date_short("2026-10-02"), "Oct 2")
        self.assertEqual(flight._us_date("not a date"), "not a date")
        self.assertEqual(flight._booking_when("2026-10-02", "14:00"), "Fri, Oct 2, 2026 2:00 PM")
        self.assertEqual(flight._slot_label("2099-10-02", "14:00"), "Fri, Oct 2, 2099 at 2:00 PM")


class TitlesTest(P2Base):
    def title(self, client, path):
        html = client.get(path).get_data(as_text=True)
        return re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()

    def test_flying_pages_end_with_the_program_name(self):  # SEAM-15
        c = self.login("master")
        self.assertEqual(self.title(c, "/flight/planes"), "Planes · Fly with Kate!")
        self.assertEqual(self.title(c, "/flight/stats"), "Stats · Fly with Kate!")
        self.assertEqual(self.title(c, "/flight/dashboard"), "Dashboard · Fly with Kate!")
        self.assertEqual(self.title(c, "/flight/students/new"), "Add Student · Fly with Kate!")
        self.assertEqual(self.title(c, "/flight/cfis/new"), "Add CFI · Fly with Kate!")

    def test_academy_pages_name_flight_academy(self):  # SEAM-15
        c = self.login("master")
        self.assertEqual(self.title(c, "/academy"), "Leaderboard · Flight Academy")
        self.assertEqual(self.title(c, "/academy/totalizer"), "Totalizer · Flight Academy")


class DeleteWordsTest(P2Base):
    def test_no_browser_confirm_boxes_left_in_flying_templates(self):  # DESIGN-3
        paths = glob.glob(os.path.join(TEMPLATES, "flight", "*.html")) + glob.glob(os.path.join(TEMPLATES, "academy*.html"))
        self.assertGreater(len(paths), 40)
        for path in paths:
            self.assertNotRegex(open(path).read(), r'(onsubmit|onclick)="return confirm\(', path)

    def test_delete_flight_is_delete_forever_in_red(self):  # DESIGN-4
        text = open(os.path.join(TEMPLATES, "flight", "log_history.html")).read()
        self.assertIn('data-confirm-ok="Delete Forever"', text)
        self.assertIn('data-confirm-style="danger"', text)
        self.assertNotIn("Permanently delete", text)


class FormFooterTest(P2Base):
    def test_save_comes_before_cancel_and_cancel_goes_back(self):  # DESIGN-8
        c = self.login("master")
        page = c.get(f"/flight/planes/{self.plane}/rate",
                     headers={"Referer": "http://localhost/flight/planes"}).get_data(as_text=True)
        self.assertLess(page.index('type="submit" class="btn btn-primary fw-bold">Save'), page.index(">Cancel</a>"))
        self.assertIn('href="/flight/planes" class="btn btn-outline-secondary">Cancel', page)
