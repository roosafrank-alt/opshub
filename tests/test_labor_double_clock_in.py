"""QA fix qa-labor-double-clock-in: two clock-in scans of the same badge
arriving at the same instant (a double tap, the scanner sending the code
twice, or two phones at once) used to both succeed, leaving two open
labor_sessions rows for one worker - the second timer then ran forever
since only one scan-to-clock-out could ever find and close it.

The race is in the gap between api_labor_scan's "does this worker already
have an open timer" SELECT and its clock-in INSERT - not something a
single-threaded test can trigger on its own, so this uses two real threads
and a barrier to force both requests through that gap at the same instant,
same as the finding's own "automated test sent two clock-in scans at
exactly the same moment" reproduction.
"""
import threading
from unittest.mock import patch

from harness import OpsHubTestCase
import db


class LaborDoubleClockInTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.laborer_id = self.exec(
            "INSERT INTO laborers (name, code, rate, active, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            ("Alex Tech", "LABOR-DUP0001", 20, 1, db.now_iso(), db.now_iso()))
        self.project = self.make_project()

    def open_sessions(self):
        return self.q("SELECT * FROM labor_sessions WHERE laborer_id = ? AND ended_at IS NULL", (self.laborer_id,))

    def _fire_pair(self, body):
        """Posts the same /api/labor/scan body from two threads at once,
        holding both right at the SELECT-then-INSERT gap (via a barrier
        hooked into now_iso, called right before each INSERT) so neither
        can see the other's row commit before deciding to insert its own."""
        barrier = threading.Barrier(2)
        real_now_iso = db.now_iso
        call_count = [0]
        count_lock = threading.Lock()

        def blocking_now_iso():
            with count_lock:
                call_count[0] += 1
                should_wait = call_count[0] <= 2
            if should_wait:
                barrier.wait(timeout=5)
            return real_now_iso()

        results = []
        results_lock = threading.Lock()

        def fire():
            c = self.app.test_client()
            with c.session_transaction() as s:
                s["user_id"] = self.users["tech"]["id"]
                s["shop_role"] = "tech"
            r = c.post("/api/labor/scan", json=body)
            with results_lock:
                results.append(r.get_json())

        with patch("app.now_iso", side_effect=blocking_now_iso):
            threads = [threading.Thread(target=fire), threading.Thread(target=fire)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        return results

    def test_two_simultaneous_general_clock_ins_start_only_one_timer(self):
        results = self._fire_pair({"code": "LABOR-DUP0001", "general": True})
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(sorted(r["action"] for r in results), ["already_clocked_in", "clock_in"])
        self.assertEqual(len(self.open_sessions()), 1)

    def test_two_simultaneous_project_clock_ins_start_only_one_timer(self):
        results = self._fire_pair({"code": "LABOR-DUP0001", "project_id": self.project})
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(sorted(r["action"] for r in results), ["already_clocked_in", "clock_in"])
        sessions = self.open_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["project_id"], self.project)

    def test_a_later_scan_still_clocks_out_the_one_real_timer(self):
        self._fire_pair({"code": "LABOR-DUP0001", "general": True})
        c = self.login("tech")
        r = c.post("/api/labor/scan", json={"code": "LABOR-DUP0001", "note": "worked on stuff"})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(r.get_json()["action"], "clock_out")
        self.assertEqual(len(self.open_sessions()), 0)
