"""Idea "month view": the Schedule's Month view left the boxes before day 1
and after the last day of the month totally blank, so a month that didn't
start or end on a Monday/Sunday looked broken at the edges. Those boxes now
show the adjacent month's real day numbers, greyed out (see _build_schedule_
month/_add_months in flight.py and the leading/trailing cell markup in
templates/flight/_schedule_live.html)."""
from harness import OpsHubTestCase
from flight import _build_schedule_month
import db


class ScheduleMonthViewFillsGridTest(OpsHubTestCase):
    def test_leading_cell_shows_the_previous_months_last_day(self):
        conn = db.get_db()
        # September 2026 starts on a Tuesday - one leading cell, showing
        # August's last day (31st).
        md = _build_schedule_month(conn, 2026, 9)
        conn.close()
        self.assertEqual(md["weeks"][0][0], {"day": 31, "in_month": False})
        self.assertEqual(md["weeks"][0][1], {"day": 1, "in_month": True})

    def test_trailing_cells_show_the_next_months_first_days(self):
        conn = db.get_db()
        md = _build_schedule_month(conn, 2026, 9)
        conn.close()
        last_week = md["weeks"][-1]
        trailing = [c for c in last_week if not c["in_month"]]
        self.assertEqual([c["day"] for c in trailing], [1, 2, 3, 4])

    def test_every_cell_in_every_week_is_filled(self):
        conn = db.get_db()
        for year, month in [(2026, 9), (2026, 10), (2026, 2)]:
            md = _build_schedule_month(conn, year, month)
            for week in md["weeks"]:
                self.assertEqual(len(week), 7)
                for cell in week:
                    self.assertIsNotNone(cell)
                    self.assertIn("day", cell)
        conn.close()

    def test_in_month_days_are_unaffected(self):
        conn = db.get_db()
        md = _build_schedule_month(conn, 2026, 9)
        conn.close()
        in_month_days = [c["day"] for week in md["weeks"] for c in week if c["in_month"]]
        self.assertEqual(in_month_days, list(range(1, 31)))

    def test_month_view_page_shows_the_adjacent_months_day_numbers(self):
        c = self.login("cfi")
        body = c.get("/flight/schedule?view=month&year=2026&month=9").get_data(as_text=True)
        self.assertIn('bg-light text-muted', body)
        # Leading day (August 31) and one of the trailing days (October 1-4).
        self.assertIn('<div class="small">31</div>', body)
        self.assertIn('<div class="small">1</div>', body)
