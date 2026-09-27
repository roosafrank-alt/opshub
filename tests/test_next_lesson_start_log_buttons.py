"""QA finding ux-next-lesson-start-log: starting or logging the next lesson
meant opening the "Details & changes" pop-up first. The Next Lesson card
(and today's flight chips) now show Start Flight/Log Flight directly once
the booking is within 30 minutes of its slot - before that, still just the
one Details & changes button, so there's nothing to accidentally tap on a
lesson still hours out. See _ready_for_quick_actions in flight.py."""
from datetime import datetime, timedelta
from harness import OpsHubTestCase, seed_row
import db


class NextLessonStartLogButtonsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N999TT")
        conn = db.get_db()
        self.student_id = conn.execute("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],)).fetchone()["id"]
        self.cfi_id = conn.execute("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],)).fetchone()["id"]
        conn.close()

    def _book(self, minutes_from_now, **extra):
        when = datetime.now() + timedelta(minutes=minutes_from_now)
        cols = dict(asset_id=self.asset_id, student_id=self.student_id, cfi_id=self.cfi_id,
                    scheduled_date=when.strftime("%Y-%m-%d"), scheduled_time=when.strftime("%H:%M"),
                    status="scheduled")
        cols.update(extra)
        conn = db.get_db()
        rid = seed_row(conn, "scheduled_flights", **cols)
        conn.commit()
        conn.close()
        return rid

    def _next_lesson_card(self, body):
        # The static, hidden schedule-detail-modal markup also contains the
        # literal words "Start Flight"/"Log Flight" (populated by JS when a
        # card/chip is clicked), so assertions have to be scoped to just the
        # Next Lesson card's own HTML, not the whole page.
        start = body.index("Your Next Lesson")
        end = body.index("Showing the whole school", start)
        return body[start:end]

    def test_more_than_30_min_out_shows_only_details_button(self):
        self._book(45)
        c = self.login("cfi")
        card = self._next_lesson_card(c.get("/flight/dashboard").get_data(as_text=True))
        self.assertIn("Details &amp; changes", card)
        self.assertNotIn("Start Flight", card)
        self.assertNotIn("Log Flight", card)

    def test_within_30_min_shows_start_and_log_flight(self):
        self._book(15)
        c = self.login("cfi")
        card = self._next_lesson_card(c.get("/flight/dashboard").get_data(as_text=True))
        self.assertIn("Start Flight", card)
        self.assertIn("Log Flight", card)
        # Details shrinks to a small link, still present.
        self.assertIn("Details &amp; changes", card)

    def test_within_30_min_but_before_the_slot_uses_early_start_form(self):
        self._book(15)
        c = self.login("cfi")
        card = self._next_lesson_card(c.get("/flight/dashboard").get_data(as_text=True))
        self.assertIn("early-start-form", card)
        self.assertIn("starting now will ask to move it", card)

    def test_at_the_slot_uses_a_plain_start_form(self):
        self._book(0)  # this exact minute - both "ready" and "can start"
        c = self.login("cfi")
        card = self._next_lesson_card(c.get("/flight/dashboard").get_data(as_text=True))
        self.assertIn("Start Flight", card)
        self.assertNotIn("early-start-form", card)

    def test_balance_hold_booking_never_shows_start_log_even_when_close(self):
        self._book(10, status="balance_hold")
        c = self.login("cfi")
        card = self._next_lesson_card(c.get("/flight/dashboard").get_data(as_text=True))
        self.assertIn("Details &amp; changes", card)
        self.assertNotIn("Start Flight", card)
        self.assertNotIn("Log Flight", card)

    def _chip_detail(self, body):
        start = body.index('class="flight-chip-detail"')
        return body[start:start + 1500]  # comfortably covers one chip's detail block

    def test_todays_flight_chip_follows_the_same_rule(self):
        self._book(45)
        c = self.login("cfi")
        chip = self._chip_detail(c.get("/flight/dashboard").get_data(as_text=True))
        self.assertNotIn("early-start-form", chip)
        self.assertNotIn('href="/flight/log/new', chip)

    def test_todays_flight_chip_shows_start_and_log_once_close(self):
        self._book(10)
        c = self.login("cfi")
        chip = self._chip_detail(c.get("/flight/dashboard").get_data(as_text=True))
        self.assertIn("early-start-form", chip)
        self.assertIn('href="/flight/log/new', chip)
