"""QA finding feat-book-next-lesson: a two-line box in the "Session ended
and logged" confirmation (right after End Session) lets a CFI book the
student's next lesson in one tap - same instructor, plane and time slot,
one week out (same day-of-week, same time) from the lesson that just
ended - or tap the pencil icon to open Schedule Flight pre-filled with the
same combination so the date/time can be changed before booking. See
flight._next_lesson_preview (built in log_history, off log_end's
?ended_flight_id= redirect) and the box itself in base_flight.html's flash
loop."""
import re
from datetime import datetime, timedelta

import db
import flight
from harness import OpsHubTestCase, seed_row


def _extract(html, name):
    m = re.search(r'name="%s" value="([^"]*)"' % re.escape(name), html)
    return m.group(1) if m else None


class BookNextLessonTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        conn = db.get_db()
        self.other_cfi = seed_row(conn, "cfis", name="Other CFI", username="other-cfi-bnl", active=1,
                                  created_at=db.now_iso())
        conn.commit()
        conn.close()
        self.asset = self.exec(
            "INSERT INTO assets (tag, name, is_flight_asset, hobbs_hours, tach_hours, created_at, updated_at) "
            "VALUES (?,?,1,?,?,?,?)", ("N81PA", "Piper Archer", 500.0, 480.0, db.now_iso(), db.now_iso()))
        self.started_at = "2026-09-28 09:00:00"

    def _running_flight(self, **kw):
        kw.setdefault("cfi_id", self.cfi)
        kw.setdefault("student_id", self.student)
        kw.setdefault("asset_id", self.asset)
        kw.setdefault("flight_date", "2026-09-28")
        kw.setdefault("solo", 0)
        kw.setdefault("started_at", self.started_at)
        kw.setdefault("hobbs_start", 500.0)
        kw.setdefault("tach_start", 480.0)
        conn = db.get_db()
        fid = seed_row(conn, "flights", **kw)
        conn.commit()
        conn.close()
        return fid

    def _end_session(self, c, fid, **form_overrides):
        form = dict(hobbs_end="501.5", tach_end="480.8", paid="0")
        form.update(form_overrides)
        r = c.post(f"/flight/log/{fid}/end", data=form)
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"ended_flight_id={fid}", r.headers["Location"])
        return c.get(r.headers["Location"]).get_data(as_text=True)

    # ----- the box itself ---------------------------------------------------

    def test_ending_a_session_shows_a_one_tap_book_button_a_week_out(self):
        fid = self._running_flight()
        c = self.login("cfi")
        html = self._end_session(c, fid)
        self.assertIn("Session ended and logged.", html)
        expected_next = datetime.strptime(self.started_at, "%Y-%m-%d %H:%M:%S") + timedelta(days=7)
        self.assertEqual(_extract(html, "scheduled_date"), expected_next.strftime("%Y-%m-%d"))
        self.assertEqual(_extract(html, "scheduled_time"), expected_next.strftime("%H:%M"))
        self.assertEqual(_extract(html, "asset_id"), str(self.asset))
        self.assertEqual(_extract(html, "cfi_id"), str(self.cfi))
        self.assertEqual(_extract(html, "student_id"), str(self.student))
        # Frank asked for the box to stay small - just the button and the
        # pencil icon, no explanatory sentence.
        self.assertNotIn("Same instructor, plane and time", html)
        self.assertIn("bi-pencil", html)

    def test_book_button_posts_straight_to_schedule_new_and_books_it(self):
        fid = self._running_flight()
        c = self.login("cfi")
        html = self._end_session(c, fid)
        booked = c.post("/flight/schedule/new", data={
            "student_id": _extract(html, "student_id"),
            "cfi_id": _extract(html, "cfi_id"),
            "asset_id": _extract(html, "asset_id"),
            "scheduled_date": _extract(html, "scheduled_date"),
            "scheduled_time": _extract(html, "scheduled_time"),
        })
        self.assertEqual(booked.status_code, 302)
        row = self.q1("SELECT * FROM scheduled_flights WHERE asset_id = ? AND student_id = ? ORDER BY id DESC",
                       (self.asset, self.student))
        self.assertIsNotNone(row)
        self.assertEqual(row["cfi_id"], self.cfi)
        self.assertEqual(row["scheduled_date"], "2026-10-05")
        self.assertEqual(row["scheduled_time"], "09:00")
        self.assertEqual(row["status"], "scheduled")

    def test_pencil_icon_opens_schedule_flight_prefilled(self):
        fid = self._running_flight()
        c = self.login("cfi")
        html = self._end_session(c, fid)
        m = re.search(r'href="([^"]*/flight/schedule/new\?[^"]*)"', html)
        self.assertIsNotNone(m)
        edit_url = m.group(1).replace("&amp;", "&")
        form_html = c.get(edit_url).get_data(as_text=True)
        asset_option = re.search(r'<option value="%s"[^>]*>' % self.asset, form_html)
        self.assertIsNotNone(asset_option)
        self.assertIn("selected", asset_option.group(0))
        self.assertIn(f'value="{self.cfi}"', form_html)
        self.assertIn('value="2026-10-05"', form_html)
        # The hidden <select> that actually submits has the right student
        # marked selected (the visible combo box reads its label from this
        # on page load - see the student-combo script).
        self.assertIn(f'value="{self.student}"', form_html)

    # ----- who sees it -------------------------------------------------------

    def test_solo_student_ending_their_own_flight_gets_no_book_box(self):
        fid = self._running_flight(cfi_id=None, solo=1)
        c = self.login("flight_student")
        html = self._end_session(c, fid)
        self.assertIn("Session ended and logged.", html)
        self.assertNotIn('action="/flight/schedule/new"', html)

    def test_next_lesson_preview_is_only_for_the_cfi_who_flew_it_or_an_admin(self):
        from flask import session as flask_session
        fid = self._running_flight()
        self._end_session(self.login("cfi"), fid)
        # A different CFI viewing the same ended_flight_id (however they got
        # there) doesn't get someone else's booking preview.
        with self.app.test_request_context("/"):
            flask_session["cfi_id"] = self.other_cfi
            self.assertIsNone(flight._next_lesson_preview(fid))
        with self.app.test_request_context("/"):
            flask_session["cfi_id"] = self.cfi
            self.assertIsNotNone(flight._next_lesson_preview(fid))
        with self.app.test_request_context("/"):
            flask_session["is_master_admin"] = True
            self.assertIsNotNone(flight._next_lesson_preview(fid))
        with self.app.test_request_context("/"):
            self.assertIsNone(flight._next_lesson_preview(fid))  # no cfi/admin session at all

    def test_no_ended_flight_id_means_no_box(self):
        fid = self._running_flight()
        c = self.login("cfi")
        html = c.post(f"/flight/log/{fid}/end",
                       data={"hobbs_end": "501.5", "tach_end": "480.8", "paid": "0"}, follow_redirects=False)
        c2 = self.login("cfi")
        plain = c2.get("/flight/log").get_data(as_text=True)
        self.assertNotIn('action="/flight/schedule/new"', plain)
