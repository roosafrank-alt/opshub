"""QA fix ux-flight-history-compact-phone: on a phone, Flight History shows
each flight as a compact two-line row (date/plane/hobbs, then
student/instructor/notes with small tags) instead of the old ten-item
table-stack box, so a lot more flights fit on one screen. Desktop still
gets the full table. These tests check the phone markup renders the right
fields, hides a student's own name on their own history, and that the
desktop table is unaffected."""
import re
from harness import OpsHubTestCase
import db


def _compact_block(html):
    """The phone-only compact list's HTML, so assertions about it don't
    accidentally match the desktop table (which repeats the same names/
    notes/tags in its own markup right below it)."""
    m = re.search(r'id="history-compact">(.*?)<div class="card d-none d-sm-block">', html, re.S)
    assert m, "compact phone list (#history-compact) not found in the page"
    return m.group(1)


class FlightHistoryCompactPhoneTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N81PA")
        conn = db.get_db()
        self.student_id = conn.execute("SELECT id FROM students WHERE user_id = ?",
                                       (self.users["flight_student"]["id"],)).fetchone()["id"]
        self.cfi_id = conn.execute("SELECT id FROM cfis WHERE user_id = ?",
                                   (self.users["cfi_billing"]["id"],)).fetchone()["id"]
        conn.close()
        self.flight_id = self.exec(
            """INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, hobbs_start, hobbs_end,
               tach_start, tach_end, oil_added_qt, notes, solo, paid, squawk, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,0,0,1,?)""",
            (self.cfi_id, self.student_id, self.asset_id, "2026-09-27", 100.0, 101.4,
             200.0, 201.2, 0.5, "Pattern work, 6 T&Gs", db.now_iso()))

    def test_cfi_billing_view_shows_compact_row_with_name_and_tags(self):
        c = self.login("cfi_billing")
        html = c.get("/flight/log").get_data(as_text=True)
        block = _compact_block(html)
        self.assertIn("flight-compact-row", block)
        self.assertIn("N81PA", block)
        self.assertIn("1.4 hr", block)
        self.assertIn("Flight Student", block)  # billing staff sees the student's name
        self.assertIn("Pattern work, 6 T&amp;Gs", block)
        self.assertIn(">Unpaid<", block)
        self.assertIn("qt oil", block)
        self.assertIn(">Squawk<", block)

    def test_student_viewing_own_history_does_not_see_own_name_in_compact_row(self):
        c = self.login("flight_student")
        html = c.get("/flight/log").get_data(as_text=True)
        block = _compact_block(html)
        self.assertIn("flight-compact-row", block)
        self.assertNotIn("Flight Student", block)
        self.assertIn("with Cfi Billing", block)
        # A student never sees billing tags, even though this flight is unpaid.
        self.assertNotIn(">Unpaid<", block)

    def test_desktop_table_still_renders_full_row(self):
        c = self.login("cfi_billing")
        html = c.get("/flight/log").get_data(as_text=True)
        self.assertIn('id="history-table"', html)
        self.assertIn('data-label="Oil Added"', html)
        self.assertIn('d-none d-sm-block', html)

    def test_tapping_a_compact_row_opens_the_flight_detail_page(self):
        c = self.login("cfi_billing")
        html = c.get("/flight/log").get_data(as_text=True)
        block = _compact_block(html)
        self.assertIn(f'/flight/log/{self.flight_id}', block)
