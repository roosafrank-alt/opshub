"""Idea "flight history": editing a past flight's Hobbs time wasn't
reflected in the student's total bill.

The Hobbs Start / Tach Start boxes on the log-flight form (log_new.html,
reused for editing - see flight.log_edit) are disabled and have no name=
attribute, so a real submission never includes them - same as when logging
a brand-new flight. flight.log_edit used to read hobbs_start/tach_start
straight from request.form anyway, which is always None, silently wiping
the flight's starting readings to NULL on every save. That zeroed out
_flight_hours (neither the Hobbs nor the Tach pair was complete any more),
so the re-deducted ledger entry dropped to $0 instead of reflecting the
edited Hobbs time.
"""
from datetime import date

from harness import OpsHubTestCase, seed_row
import db
import flight


class LogEditHobbsBillingTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.plane = self.make_asset("N123AB")
        self.exec("UPDATE assets SET is_flight_asset = 1 WHERE id = ?", (self.plane,))
        self.student_id = self.q1("SELECT id FROM students WHERE user_id = ?",
                                  (self.users["flight_student"]["id"],))["id"]
        self.cfi_id = self.q1("SELECT id FROM cfis WHERE user_id = ?", (self.users["cfi"]["id"],))["id"]
        self.exec("UPDATE students SET plane_rate_override = 100 WHERE id = ?", (self.student_id,))
        self.exec("UPDATE cfis SET rate_per_hour = 50 WHERE id = ?", (self.cfi_id,))

    def _logged_flight(self, hobbs_start=100.0, hobbs_end=101.0):
        conn = db.get_db()
        fid = seed_row(conn, "flights", asset_id=self.plane, student_id=self.student_id, cfi_id=self.cfi_id,
                       flight_date=date.today().isoformat(), hobbs_start=hobbs_start, hobbs_end=hobbs_end,
                       solo=0, ended_at=db.now_iso())
        conn.commit()
        row = self.q1(flight._LOG_ROW_SQL + " WHERE f.id = ?", (fid,))
        cost = flight._row_with_cost(row)
        with self.app.app_context():
            flight._deduct_flight_cost(conn, cost, created_by="test")
        conn.commit()
        conn.close()
        return fid

    def _edit(self, fid, hobbs_end):
        # Only the fields the real form actually submits - hobbs_start and
        # tach_start are disabled/nameless on the page and never sent.
        return self.login("master").post(f"/flight/log/{fid}/edit", data={
            "student_id": str(self.student_id), "cfi_id": str(self.cfi_id), "asset_id": str(self.plane),
            "flight_date": date.today().isoformat(),
            "hobbs_end": str(hobbs_end),
            "paid": "",
        })

    def test_editing_hobbs_end_updates_the_students_bill(self):
        # 1 hr: $100 plane + $50 instructor = $150.
        fid = self._logged_flight(hobbs_start=100.0, hobbs_end=101.0)
        self.assertEqual(self.q1("SELECT balance FROM students WHERE id=?", (self.student_id,))["balance"], -150.0)

        # Now 3 hrs: $300 plane + $150 instructor = $450.
        r = self._edit(fid, hobbs_end=103.0)
        self.assertEqual(r.status_code, 302)

        row = self.q1(flight._LOG_ROW_SQL + " WHERE f.id = ?", (fid,))
        cost = flight._row_with_cost(row)
        self.assertEqual(cost["hours"], 3.0)
        self.assertEqual(cost["total"], 450.0)
        balance = self.q1("SELECT balance FROM students WHERE id=?", (self.student_id,))["balance"]
        self.assertEqual(balance, -450.0)

    def test_editing_a_logged_flight_keeps_its_starting_hobbs_reading(self):
        fid = self._logged_flight(hobbs_start=100.0, hobbs_end=101.0)
        self._edit(fid, hobbs_end=103.0)
        row = self.q1("SELECT hobbs_start, hobbs_end FROM flights WHERE id = ?", (fid,))
        self.assertEqual(row["hobbs_start"], 100.0)
        self.assertEqual(row["hobbs_end"], 103.0)
