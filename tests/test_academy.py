"""Flight Academy: leaderboard page access and the "Log Your Flying" entries
(add, bad input, landing-currency link, delete, who may do what).
"""
from harness import OpsHubTestCase


class AcademyBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.student = self.q1("SELECT id FROM students WHERE user_id=?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE users SET academy_access=1 WHERE id=?", (self.users["flight_student"]["id"],))

    def add(self, **form):
        data = dict(kind="distance_nm", value="120", entry_date="2026-09-01")
        data.update(form)
        return self.client.post("/academy/entry", data=data)

    def entries(self):
        return self.q("SELECT * FROM academy_entries ORDER BY id")

    def landings(self):
        return self.q("SELECT * FROM manual_landings ORDER BY id")


class AcademyPageTest(AcademyBase):
    def test_student_with_access_sees_page(self):
        self.login("flight_student")
        self.assertEqual(self.client.get("/academy").status_code, 200)

    def test_all_periods_and_levels_render(self):
        self.login("master")
        for period in ("all", "year", "month", "bogus"):
            for level in ("", "all", "bogus"):
                with self.subTest(period=period, level=level):
                    self.assertEqual(self.client.get(f"/academy?period={period}&level={level}").status_code, 200)

    def test_user_without_access_is_sent_away(self):
        self.exec("UPDATE users SET academy_access=0 WHERE id=?", (self.users["flight_student"]["id"],))
        self.login("flight_student")
        r = self.client.get("/academy")
        self.assertEqual(r.status_code, 302)

    def test_logged_out_goes_to_login(self):
        self.login(None)
        self.assertEqual(self.client.get("/academy").status_code, 302)

    def test_unknown_picked_student_does_not_crash(self):
        self.login("master")
        self.assertEqual(self.client.get("/academy?student_id=99999").status_code, 200)
        self.assertEqual(self.client.get("/academy?student_id=abc").status_code, 200)


class AddEntryTest(AcademyBase):
    def test_student_logs_own_entry(self):
        self.login("flight_student")
        r = self.add()
        self.assertEqual(r.status_code, 302)
        e = self.entries()
        self.assertEqual(len(e), 1)
        self.assertEqual((e[0]["student_id"], e[0]["kind"], e[0]["value"]), (self.student, "distance_nm", 120.0))

    def test_student_cannot_log_for_someone_else(self):
        other = self.exec("INSERT INTO students (name, username, password_hash, active) VALUES ('Other', 'other', 'x', 1)")
        self.login("flight_student")
        self.add(student_id=str(other))
        self.assertEqual([e["student_id"] for e in self.entries()], [self.student])

    def test_admin_logs_for_picked_student(self):
        self.login("master")
        self.add(student_id=str(self.student))
        self.assertEqual([e["student_id"] for e in self.entries()], [self.student])

    def test_admin_without_student_saves_nothing(self):
        self.login("master")
        self.add()
        self.assertEqual(len(self.entries()), 0)

    def test_bad_values_save_nothing(self):
        self.login("flight_student")
        for bad in ("", "0", "-5", "abc", "100001"):
            with self.subTest(value=bad):
                self.add(value=bad)
                self.assertEqual(len(self.entries()), 0, bad)

    def test_nan_value_is_rejected_not_a_crash(self):
        self.login("flight_student")
        r = self.add(value="nan")
        self.assertLess(r.status_code, 500)
        self.assertEqual(len(self.entries()), 0)

    def test_infinite_value_is_not_stored(self):
        self.login("flight_student")
        self.add(value="inf")
        self.assertEqual(len(self.entries()), 0)

    def test_unknown_kind_saves_nothing(self):
        self.login("flight_student")
        self.add(kind="bogus")
        self.assertEqual(len(self.entries()), 0)

    def test_bad_entry_date_does_not_crash(self):
        self.login("flight_student")
        r = self.add(entry_date="garbage")
        self.assertLess(r.status_code, 500)

    def test_cfi_cannot_log(self):
        self.exec("UPDATE users SET academy_access=1 WHERE id=?", (self.users["cfi"]["id"],))
        self.login("cfi")
        self.add(student_id=str(self.student))
        self.assertEqual(len(self.entries()), 0)

    def test_no_access_cannot_log(self):
        self.exec("UPDATE users SET academy_access=0 WHERE id=?", (self.users["flight_student"]["id"],))
        self.login("flight_student")
        self.add()
        self.assertEqual(len(self.entries()), 0)

    def test_landings_also_create_currency_record(self):
        self.login("flight_student")
        self.add(kind="landings", value="3")
        l = self.landings()
        self.assertEqual(len(l), 1)
        self.assertEqual((l[0]["day_landings"], l[0]["night_landings"]), (3, 0))
        self.assertEqual(l[0]["academy_entry_id"], self.entries()[0]["id"])

    def test_night_landings_go_in_night_column(self):
        self.login("flight_student")
        self.add(kind="night_landings", value="2")
        l = self.landings()[0]
        self.assertEqual((l["day_landings"], l["night_landings"]), (0, 2))

    def test_non_landing_kinds_make_no_currency_record(self):
        self.login("flight_student")
        self.add(kind="xc_flights", value="1")
        self.assertEqual(len(self.landings()), 0)


class DeleteEntryTest(AcademyBase):
    def make(self, kind="landings", student=None):
        sid = student or self.student
        eid = self.exec("INSERT INTO academy_entries (student_id, entry_date, kind, value, created_at) VALUES (?,?,?,?,?)",
                        (sid, "2026-09-01", kind, 2, "2026-09-01"))
        if kind == "landings":
            self.exec("INSERT INTO manual_landings (student_id, landing_date, day_landings, night_landings, academy_entry_id) VALUES (?,?,?,?,?)",
                      (sid, "2026-09-01", 2, 0, eid))
        return eid

    def test_student_deletes_own_and_linked_landing(self):
        eid = self.make()
        self.login("flight_student")
        self.client.post(f"/academy/entry/{eid}/delete")
        self.assertEqual(len(self.entries()), 0)
        self.assertEqual(len(self.landings()), 0)

    def test_student_cannot_delete_someone_elses(self):
        other = self.exec("INSERT INTO students (name, username, password_hash, active) VALUES ('Other', 'other', 'x', 1)")
        eid = self.make(student=other)
        self.login("flight_student")
        self.client.post(f"/academy/entry/{eid}/delete")
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(len(self.landings()), 1)

    def test_admin_deletes_any(self):
        eid = self.make()
        self.login("master")
        self.client.post(f"/academy/entry/{eid}/delete")
        self.assertEqual(len(self.entries()), 0)

    def test_cfi_cannot_delete(self):
        eid = self.make()
        self.login("cfi")
        self.client.post(f"/academy/entry/{eid}/delete")
        self.assertEqual(len(self.entries()), 1)

    def test_missing_entry_is_not_a_crash(self):
        self.login("master")
        r = self.client.post("/academy/entry/99999/delete")
        self.assertLess(r.status_code, 500)

    def test_double_delete_is_harmless(self):
        eid = self.make()
        self.login("flight_student")
        self.client.post(f"/academy/entry/{eid}/delete")
        r = self.client.post(f"/academy/entry/{eid}/delete")
        self.assertLess(r.status_code, 500)

    def test_logged_out_cannot_delete(self):
        eid = self.make()
        self.login(None)
        self.client.post(f"/academy/entry/{eid}/delete")
        self.assertEqual(len(self.entries()), 1)
