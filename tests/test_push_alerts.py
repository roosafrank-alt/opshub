"""Phone alerts during a flying session (flight.check_session_alerts):
"30 minutes left", "time is up" and "running late" go to the right people,
once (late: every 15 minutes), only to people who switched that alert on,
and never to deactivated accounts. Sending itself is replaced by a recorder.
"""
from datetime import datetime, timedelta

import db
import flight
import push
from harness import OpsHubTestCase


class SessionAlertsTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self._orig = push.queue_and_push
        push.queue_and_push = lambda conn, uid, title, body=None, tag=None, url=None: (self.sent.append((uid, title)) or [])
        self.plane = self.make_asset("N400PA")
        self.exec("UPDATE assets SET is_flight_asset=1 WHERE id=?", (self.plane,))
        self.cfi_user = self.users["cfi"]["id"]
        self.stu_user = self.users["flight_student"]["id"]
        self.cfi = self.q1("SELECT id FROM cfis WHERE user_id=?", (self.cfi_user,))["id"]
        self.student = self.q1("SELECT id FROM students WHERE user_id=?", (self.stu_user,))["id"]
        for u in (self.cfi_user, self.stu_user):
            self.exec("UPDATE users SET notify_push_30min=1, notify_push_timeup=1, notify_push_late=1 WHERE id=?", (u,))

    def tearDown(self):
        push.queue_and_push = self._orig
        super().tearDown()

    def session(self, started_min_ago, hours=1.0, solo=False):
        now = datetime.strptime(db.now_iso(), "%Y-%m-%d %H:%M:%S")
        started = (now - timedelta(minutes=started_min_ago)).strftime("%Y-%m-%d %H:%M:%S")
        sf = self.exec("INSERT INTO scheduled_flights (asset_id, cfi_id, student_id, scheduled_date, scheduled_time, "
                       "duration_hours, solo) VALUES (?,?,?,?,?,?,?)",
                       (self.plane, None if solo else self.cfi, self.student, started[:10], started[11:16], hours, 1 if solo else 0))
        return self.exec("INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, solo, scheduled_flight_id, started_at) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (None if solo else self.cfi, self.student, self.plane, started[:10], 1 if solo else 0, sf, started))

    def titles(self):
        return sorted((t, u) for u, t in self.sent)

    def test_nothing_sent_early_in_the_block(self):
        self.session(started_min_ago=10)
        flight.check_session_alerts()
        self.assertEqual(self.sent, [])

    def test_thirty_minute_warning_goes_to_both_once(self):
        self.session(started_min_ago=35)
        flight.check_session_alerts()
        self.assertEqual({u for u, t in self.sent if t == "30 minutes left"}, {self.cfi_user, self.stu_user})
        self.sent.clear()
        flight.check_session_alerts()
        self.assertEqual(self.sent, [])

    def test_time_up_goes_once(self):
        self.session(started_min_ago=61)
        flight.check_session_alerts()
        self.assertEqual({t for u, t in self.sent}, {"Flight time is up"})
        self.sent.clear()
        flight.check_session_alerts()
        self.assertEqual(self.sent, [])

    def test_only_people_who_switched_it_on_are_alerted(self):
        self.exec("UPDATE users SET notify_push_30min=0 WHERE id=?", (self.stu_user,))
        self.session(started_min_ago=35)
        flight.check_session_alerts()
        self.assertEqual([u for u, t in self.sent], [self.cfi_user])

    def test_deactivated_person_is_not_alerted(self):
        self.exec("UPDATE users SET active=0 WHERE id=?", (self.stu_user,))
        self.session(started_min_ago=35)
        flight.check_session_alerts()
        self.assertEqual([u for u, t in self.sent], [self.cfi_user])

    def test_running_late_goes_to_instructor_only_and_repeats_every_15_minutes(self):
        fid = self.session(started_min_ago=80)
        flight.check_session_alerts()
        late = [(u, t) for u, t in self.sent if t == "Flight is running late"]
        self.assertEqual(late, [(self.cfi_user, "Flight is running late")])
        self.sent.clear()
        flight.check_session_alerts()
        self.assertEqual([t for u, t in self.sent if t == "Flight is running late"], [])   # not again right away
        old = (datetime.strptime(db.now_iso(), "%Y-%m-%d %H:%M:%S") - timedelta(minutes=16)).strftime("%Y-%m-%d %H:%M:%S")
        self.exec("UPDATE flights SET overdue_alert_sent_at=? WHERE id=?", (old, fid))
        flight.check_session_alerts()
        self.assertEqual(len([1 for u, t in self.sent if t == "Flight is running late"]), 1)

    def test_acknowledged_late_flight_stays_quiet(self):
        fid = self.session(started_min_ago=80)
        self.exec("UPDATE flights SET overdue_acknowledged_at=? WHERE id=?", (db.now_iso(), fid))
        flight.check_session_alerts()
        self.assertEqual([t for u, t in self.sent if t == "Flight is running late"], [])

    def test_solo_late_flight_alerts_admins(self):
        self.exec("UPDATE users SET notify_push_late=1 WHERE id=?", (self.users["master"]["id"],))
        self.session(started_min_ago=80, solo=True)
        flight.check_session_alerts()
        self.assertIn(self.users["master"]["id"], [u for u, t in self.sent if t == "Flight is running late"])

    def test_ended_flight_sends_nothing(self):
        fid = self.session(started_min_ago=80)
        self.exec("UPDATE flights SET ended_at=? WHERE id=?", (db.now_iso(), fid))
        flight.check_session_alerts()
        self.assertEqual(self.sent, [])

    def test_flight_without_a_booked_block_sends_nothing(self):
        fid = self.session(started_min_ago=300)
        self.exec("UPDATE scheduled_flights SET duration_hours=NULL")
        flight.check_session_alerts()
        self.assertEqual(self.sent, [])

    def test_paused_time_does_not_count(self):
        fid = self.session(started_min_ago=61)
        self.exec("UPDATE flights SET paused_seconds=1800 WHERE id=?", (fid,))   # 31 min actually flown
        flight.check_session_alerts()
        self.assertEqual([t for u, t in self.sent if t == "Flight time is up"], [])
