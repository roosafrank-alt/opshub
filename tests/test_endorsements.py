"""ENDORSE: endorsements from the AC 61-65K catalog - wording, expiry, the student tab with
current / expired, the rating checklist, signing with auto-fill, the CFI's record, solo sign-off
for booking, and the expiry alerts."""
from datetime import date, timedelta

from harness import OpsHubTestCase
import endorsements as en


class CatalogTest(OpsHubTestCase):
    def test_catalog_has_all_96_with_exact_ac_wording(self):
        cat = en.catalog()
        self.assertEqual([e["num"] for e in cat], list(range(1, 97)))
        a6 = en.get("A.6")
        self.assertEqual(a6["group_short"], "Student pilot")
        self.assertTrue(a6["student_pilot_only"])
        self.assertIn("I certify that [First name, MI, Last name] has received the required training to qualify for solo flying.", a6["text"])
        self.assertIn("M/M", a6["blanks"])
        # A page break in the PDF must not split a sentence
        self.assertIn("from [origination airport] to [origination airport] via [route of flight]", en.get("A.10")["text"])

    def test_fill_and_expiry_rules(self):
        t = en.fill(en.get("A.12")["text"], {"First name, MI, Last name": "Ann B. Pilot", "name of Class B": "New York Class B"})
        self.assertIn("Ann B. Pilot", t)
        self.assertNotIn("[List any", t)                                   # empty conditions blank is dropped
        self.assertEqual(en.compute_expiry("A.6", "2026-10-01"), "2026-12-30")       # 90 days
        self.assertEqual(en.compute_expiry("A.69", "2026-03-10"), "2028-03-31")      # 24 calendar months
        self.assertEqual(en.compute_expiry("A.1", "2026-03-10"), "2026-05-31")       # 2 calendar months
        self.assertEqual(en.compute_expiry("A.10", "2026-03-10", {"date": "03-14-2026"}), "2026-03-14")
        self.assertIsNone(en.compute_expiry("A.36", "2026-03-10"))


class SignAndViewTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.exec("UPDATE users SET academy_access = 1 WHERE username IN ('flight_student', 'cfi')")

    def _sign(self, c, cid, signed_on=None, **blanks):
        e = en.get(cid)
        data = {"catalog_id": cid, "student_id": self.sid, "signed_on": signed_on or date.today().isoformat()}
        for i, b in enumerate(e["blanks"]):
            data["b%d" % i] = blanks.get(b, "x")
        return c.post("/academy/endorsements/new", data=data)

    def test_cfi_signs_solo_and_it_drives_solo_booking(self):
        c = self.login("cfi")
        self.assertIn("New endorsement", c.get(f"/academy/endorsements/new?student_id={self.sid}").get_data(as_text=True))
        d = c.get(f"/academy/endorsements/defaults?e=A.6&student_id={self.sid}").get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["values"][0], self.q1("SELECT name FROM students WHERE id = ?", (self.sid,))["name"])
        r = self._sign(c, "A.6", **{"First name, MI, Last name": "Flight Student", "M/M": "Cessna 172"})
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM student_endorsements WHERE student_id = ?", (self.sid,))
        self.assertEqual(row["catalog_id"], "A.6")
        self.assertEqual(row["expires_on"], (date.today() + timedelta(days=90)).isoformat())
        self.assertIn("proficient to make solo flights in Cessna 172.", row["body"])
        st = self.q1("SELECT solo_signoff_date, solo_signoff_expires FROM students WHERE id = ?", (self.sid,))
        self.assertEqual((st["solo_signoff_date"], st["solo_signoff_expires"]), (row["signed_on"], row["expires_on"]))
        self.assertIn("A.6", c.get("/academy/endorsements/given").get_data(as_text=True))

    def test_student_tab_current_expired_and_checklist(self):
        c = self.login("cfi")
        self._sign(c, "A.6", signed_on=(date.today() - timedelta(days=200)).isoformat())   # expired
        self._sign(c, "A.36")
        stu = self.login("flight_student")
        body = stu.get("/academy/endorsements/").get_data(as_text=True)
        self.assertIn("Current endorsements", body)
        self.assertIn("View 1 expired endorsement", body)
        self.assertIn("Needed for Private Pilot", body)
        self.assertIn("Still missing before the test", body)
        self.assertNotIn("New endorsement", body)                      # students don't sign
        self.assertNotEqual(stu.get("/academy/endorsements/new").status_code, 200)

    def test_renewal_replaces_the_older_solo(self):
        c = self.login("cfi")
        self._sign(c, "A.6", signed_on=(date.today() - timedelta(days=100)).isoformat())
        self._sign(c, "A.7")
        items = en.student_items(__import__("db").get_db(), self.sid)
        cur, exp, old = en.split_items(items)
        self.assertEqual([i["cid"] for i in cur], ["A.7"])
        self.assertEqual([i["cid"] for i in exp], ["A.6"])

    def test_private_pilot_hides_student_pilot_ones(self):
        c = self.login("cfi")
        self._sign(c, "A.3")
        self.exec("UPDATE students SET pilot_certificate = 'private' WHERE id = ?", (self.sid,))
        body = self.login("flight_student").get("/academy/endorsements/").get_data(as_text=True)
        self.assertIn("no longer needed", body)
        self.assertIn("Needed for Instrument Rating", body)

    def test_expiry_alerts_once_to_admins_and_cfi(self):
        c = self.login("cfi")
        self._sign(c, "A.6", signed_on=(date.today() - timedelta(days=95)).isoformat())
        conn = __import__("db").get_db()
        self.assertEqual(en.check_expiries(conn), 1)
        self.assertEqual(en.check_expiries(conn), 0)                  # only once
        self.assertTrue(conn.execute("SELECT 1 FROM push_pending WHERE title = 'Endorsement expired'").fetchone())
        conn.close()

    def test_remove_a_mistake(self):
        c = self.login("cfi")
        self._sign(c, "A.6")
        eid = self.q1("SELECT id FROM student_endorsements WHERE student_id = ?", (self.sid,))["id"]
        c.post(f"/academy/endorsements/{eid}/void", data={"reason": "typo"})
        self.assertTrue(self.q1("SELECT voided_at FROM student_endorsements WHERE id = ?", (eid,))["voided_at"])
        self.assertIsNone(self.q1("SELECT solo_signoff_date FROM students WHERE id = ?", (self.sid,))["solo_signoff_date"])


class ReviewFixesTest(SignAndViewTest):
    def test_different_plane_solo_does_not_replace_and_flight_xcs_stay_history(self):
        c = self.login("cfi")
        self._sign(c, "A.6", **{"M/M": "Cessna 152"})
        self._sign(c, "A.6", **{"M/M": "Piper PA-28"})
        cur, exp, old = en.split_items(en.student_items(__import__("db").get_db(), self.sid))
        self.assertEqual(sorted(i["cid"] for i in cur), ["A.6", "A.6"])
        self._sign(c, "A.10", **{"date": "01-02-2026"})
        self._sign(c, "A.10", **{"date": "01-09-2026"})
        cur, exp, old = en.split_items(en.student_items(__import__("db").get_db(), self.sid))
        self.assertFalse(any(i.get("superseded") for i in exp if i["cid"] == "A.10"))

    def test_ipc_is_currency_not_an_expiry_alert(self):
        c = self.login("cfi")
        self._sign(c, "A.71", signed_on=(date.today() - timedelta(days=400)).isoformat())
        conn = __import__("db").get_db()
        self.assertEqual(en.check_expiries(conn), 0)
        conn.close()

    def test_defaults_dont_guess_wrong_test_names(self):
        c = self.login("cfi")
        d = c.get(f"/academy/endorsements/defaults?e=A.78&student_id={self.sid}").get_json()
        self.assertNotIn("Private Pilot", " ".join(d["values"]))
        d = c.get(f"/academy/endorsements/defaults?e=A.1&student_id={self.sid}&track=commercial").get_json()
        self.assertIn("Commercial Pilot", d["values"])
        self.assertEqual(c.get(f"/academy/endorsements/defaults?e=A.34&student_id={self.sid}").get_json()["expires"],
                         date.today().isoformat())                     # good for the one solo flight named
