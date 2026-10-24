"""Daily email/text reminders (notify.py): who gets each one, nothing sent
twice on the same day, and nothing sent for things that no longer count
(retired parts, trashed aircraft, cancelled flights, switched-off people).

Email and texts are replaced by a recorder, so nothing leaves the test.
"""
from datetime import date, datetime, timedelta

import db
import notify
from harness import OpsHubTestCase


class NotifyBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self._orig = (notify.send_email, notify.send_sms)
        notify.send_email = lambda s, to, subj, body, brand=notify.DEFAULT_BRAND, contact="": (self.sent.append(("email", to, subj, body, brand)) or (True, None))
        notify.send_sms = lambda s, to, body: (self.sent.append(("sms", to, None, body, None)) or (True, None))

    def tearDown(self):
        notify.send_email, notify.send_sms = self._orig
        super().tearDown()

    def opt_in(self, role, email=None, phone=None, **flags):
        uid = self.users[role]["id"]
        email = email or f"{role}@example.com"
        sets = {"notify_email": 1, "email": email}
        if phone:
            sets.update(notify_sms=1, phone=phone)
        sets.update(flags)
        self.exec("UPDATE users SET " + ",".join(f"{k}=?" for k in sets) + " WHERE id=?", (*sets.values(), uid))
        return email

    def run_check(self, fn):
        conn = db.get_db()
        try:
            return fn(conn, {})
        finally:
            conn.close()

    def subjects(self):
        return [s[2] for s in self.sent if s[0] == "email"]


class LowStockTest(NotifyBase):
    def test_low_part_emails_opted_in_user_once(self):
        self.opt_in("shop_admin", notify_low_stock=1)
        self.make_part("Spark Plug", "SP-1", qty=1, reorder=2)
        self.assertEqual(self.run_check(notify.check_low_stock), 1)
        self.assertEqual(self.subjects(), ["Low stock: Spark Plug"])
        self.assertEqual(self.sent[0][1], "shop_admin@example.com")

    def test_second_run_same_day_sends_nothing(self):
        self.opt_in("shop_admin", notify_low_stock=1)
        self.make_part("Spark Plug", "SP-1", qty=1, reorder=2)
        self.run_check(notify.check_low_stock)
        self.sent.clear()
        self.assertEqual(self.run_check(notify.check_low_stock), 0)
        self.assertEqual(self.sent, [])

    def test_exactly_at_reorder_point_counts_as_low(self):
        self.opt_in("shop_admin", notify_low_stock=1)
        self.make_part("Oil", "OIL-1", qty=2, reorder=2)
        self.assertEqual(self.run_check(notify.check_low_stock), 1)

    def test_plenty_in_stock_sends_nothing(self):
        self.opt_in("shop_admin", notify_low_stock=1)
        self.make_part("Oil", "OIL-1", qty=3, reorder=2)
        self.assertEqual(self.run_check(notify.check_low_stock), 0)
        self.assertEqual(self.sent, [])

    def test_silenced_part_sends_nothing(self):
        self.opt_in("shop_admin", notify_low_stock=1)
        pid = self.make_part("Oil", "OIL-1", qty=0, reorder=2)
        self.exec("UPDATE parts SET notify_low_stock=0 WHERE id=?", (pid,))
        self.assertEqual(self.run_check(notify.check_low_stock), 0)

    def test_retired_part_sends_nothing(self):
        self.opt_in("shop_admin", notify_low_stock=1)
        pid = self.make_part("Oil", "OIL-1", qty=0, reorder=2)
        self.exec("UPDATE parts SET retired_at=datetime('now') WHERE id=?", (pid,))
        self.assertEqual(self.run_check(notify.check_low_stock), 0)

    def test_inactive_or_unsubscribed_user_gets_nothing(self):
        self.opt_in("shop_admin", notify_low_stock=1, active=0)
        self.opt_in("tech", notify_low_stock=0)
        self.make_part("Oil", "OIL-1", qty=0, reorder=2)
        self.assertEqual(self.run_check(notify.check_low_stock), 0)
        self.assertEqual(self.sent, [])

    def test_user_with_no_address_is_not_marked_as_notified(self):
        self.exec("UPDATE users SET notify_email=1, notify_low_stock=1, email=NULL WHERE id=?", (self.users["shop_admin"]["id"],))
        self.make_part("Oil", "OIL-1", qty=0, reorder=2)
        self.assertEqual(self.run_check(notify.check_low_stock), 0)
        # once an address is added, tomorrow's/the next run still tells them
        self.exec("UPDATE users SET email='a@example.com' WHERE id=?", (self.users["shop_admin"]["id"],))
        self.assertEqual(self.run_check(notify.check_low_stock), 1)

    def test_text_goes_to_phone_and_email_to_address(self):
        self.opt_in("shop_admin", phone="+15550001111", notify_low_stock=1)
        self.make_part("Oil", "OIL-1", qty=0, reorder=2)
        self.run_check(notify.check_low_stock)
        kinds = sorted((s[0], s[1]) for s in self.sent)
        self.assertEqual(kinds, [("email", "shop_admin@example.com"), ("sms", "+15550001111")])

    def test_each_recipient_gets_one_message_per_part(self):
        self.opt_in("shop_admin", notify_low_stock=1)
        self.opt_in("tech", notify_low_stock=1)
        self.make_part("Oil", "OIL-1", qty=0, reorder=2)
        self.make_part("Plug", "SP-1", qty=0, reorder=2)
        self.assertEqual(self.run_check(notify.check_low_stock), 2)
        self.assertEqual(len(self.sent), 4)


class MaintenanceTest(NotifyBase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N777QA")
        self.exec("UPDATE assets SET tach_hours=100, hobbs_hours=100 WHERE id=?", (self.plane,))
        self.opt_in("shop_admin", notify_maintenance=1)

    def item(self, name="Oil change", **over):
        row = dict(asset_id=self.plane, name=name, type="hours", hour_type="tach", interval_hours=50,
                   last_done_hours=40, active=1)
        row.update(over)
        cols = ",".join(row)
        return self.exec(f"INSERT INTO maintenance_items ({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))

    def test_overdue_item_is_reported_once_a_day(self):
        self.item(last_done_hours=40)          # due at 90, plane at 100: overdue
        self.assertEqual(self.run_check(notify.check_maintenance), 1)
        self.assertIn("overdue", self.subjects()[0].lower())
        self.sent.clear()
        self.assertEqual(self.run_check(notify.check_maintenance), 0)

    def test_item_far_from_due_sends_nothing(self):
        self.item(last_done_hours=99)
        self.assertEqual(self.run_check(notify.check_maintenance), 0)

    def test_due_soon_then_overdue_each_send_once(self):
        iid = self.item(last_done_hours=52)    # due at 102: due soon
        self.assertEqual(self.run_check(notify.check_maintenance), 1)
        self.exec("UPDATE assets SET tach_hours=110 WHERE id=?", (self.plane,))
        self.assertEqual(self.run_check(notify.check_maintenance), 1)   # now overdue: a new message
        self.assertEqual(self.run_check(notify.check_maintenance), 0)

    def test_trashed_aircraft_and_switched_off_item_are_ignored(self):
        self.item("Off", last_done_hours=40, active=0)
        p2 = self.make_asset("N888QA")
        self.exec("UPDATE assets SET tach_hours=100, deleted_at=datetime('now') WHERE id=?", (p2,))
        self.item("Trashed plane item", asset_id=p2, last_done_hours=40)
        self.assertEqual(self.run_check(notify.check_maintenance), 0)
        self.assertEqual(self.sent, [])

    def test_only_opted_in_people_are_told(self):
        self.opt_in("tech", notify_maintenance=0)
        self.item(last_done_hours=40)
        self.run_check(notify.check_maintenance)
        self.assertEqual({s[1] for s in self.sent}, {"shop_admin@example.com"})


class FlightReminderTest(NotifyBase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N300FR")
        self.exec("UPDATE assets SET is_flight_asset=1 WHERE id=?", (self.plane,))
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id=?", (self.users["cfi"]["id"],))["id"]
        self.student = self.q1("SELECT id FROM students WHERE user_id=?", (self.users["flight_student"]["id"],))["id"]
        self.opt_in("flight_student", notify_flight_reminders=1)
        self.opt_in("cfi", notify_flight_reminders=1)

    def flight(self, days=1, status="scheduled", cfi=True):
        d = (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")
        return self.exec(
            "INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date, scheduled_time, status) "
            "VALUES (?,?,?,?,?,?)", (self.plane, self.cfi if cfi else None, self.student, d, "09:30", status))

    def test_tomorrows_flight_tells_student_and_instructor(self):
        self.flight()
        self.assertEqual(self.run_check(notify.check_flight_reminders), 1)
        self.assertEqual({s[1] for s in self.sent}, {"flight_student@example.com", "cfi@example.com"})
        self.assertIn("N300FR", self.sent[0][3])
        self.assertIn("09:30", self.sent[0][3])
        self.assertEqual(self.sent[0][4], "Fly with Kate!")

    def test_not_sent_twice_in_one_day(self):
        self.flight()
        self.run_check(notify.check_flight_reminders)
        self.sent.clear()
        self.assertEqual(self.run_check(notify.check_flight_reminders), 0)
        self.assertEqual(self.sent, [])

    def test_today_next_week_and_cancelled_are_skipped(self):
        self.flight(days=0)
        self.flight(days=7)
        self.flight(days=1, status="cancelled")
        self.assertEqual(self.run_check(notify.check_flight_reminders), 0)
        self.assertEqual(self.sent, [])

    def test_student_who_opted_out_is_skipped_but_instructor_still_told(self):
        self.exec("UPDATE users SET notify_flight_reminders=0 WHERE id=?", (self.users["flight_student"]["id"],))
        self.flight()
        self.run_check(notify.check_flight_reminders)
        self.assertEqual({s[1] for s in self.sent}, {"cfi@example.com"})

    def test_deactivated_student_is_not_messaged(self):
        self.exec("UPDATE users SET active=0 WHERE id=?", (self.users["flight_student"]["id"],))
        self.flight(cfi=False)
        self.assertEqual(self.run_check(notify.check_flight_reminders), 0)
        self.assertEqual(self.sent, [])

    def test_solo_flight_without_instructor_only_tells_student(self):
        self.flight(cfi=False)
        self.run_check(notify.check_flight_reminders)
        self.assertEqual({s[1] for s in self.sent}, {"flight_student@example.com"})


class SendingTest(OpsHubTestCase):
    """The real senders, with the network already blocked by the harness."""

    def test_unconfigured_email_and_text_are_skipped_not_errors(self):
        s = notify.get_settings(db.get_db())
        self.assertEqual(notify.send_email(s, "a@example.com", "x", "y"), (False, "not configured"))
        self.assertEqual(notify.send_sms(s, "+15550001111", "y"), (False, "not configured"))

    def test_blank_recipient_is_skipped(self):
        s = dict(smtp_host="h", smtp_username="u", smtp_password="p", smtp_port="587", smtp_from="",
                 twilio_account_sid="a", twilio_auth_token="t", twilio_from_number="+1")
        self.assertEqual(notify.send_email(s, "", "x", "y")[0], False)
        self.assertEqual(notify.send_sms(s, "", "y")[0], False)

    def test_bad_port_text_falls_back_instead_of_crashing(self):
        s = dict(smtp_host="h", smtp_username="u", smtp_password="p", smtp_port="abc", smtp_from="")
        ok, err = notify.send_email(s, "a@example.com", "x", "y")
        self.assertFalse(ok)     # smtp is blocked in tests; the point is it returns instead of raising
        self.assertTrue(err)

    def test_failed_send_is_not_logged_as_sent(self):
        u = dict(notify_email=1, email="a@example.com", notify_sms=0, phone=None)
        s = dict(smtp_host="h", smtp_username="u", smtp_password="p", smtp_port="587", smtp_from="")
        self.assertFalse(notify.notify_user(s, u, "x", "y"))
