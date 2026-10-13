"""Availability grid starts at 6:30 am and steps every 1.5 hours."""
from harness import OpsHubTestCase
import flight
import db


class AvailabilityStart(OpsHubTestCase):
    def test_slots_start_630_every_90_minutes(self):
        conn = db.get_db()
        with self.app.test_request_context("/"):
            slots = flight._build_availability_day(conn, "2099-01-01")["slots"]
        conn.close()
        self.assertEqual([s["start_min"] for s in slots[:3]], [390, 480, 570])
        self.assertEqual(slots[0]["label"], flight._format_time_12h("06:30"))
        self.assertEqual(slots[-1]["start_min"], 20 * 60)
