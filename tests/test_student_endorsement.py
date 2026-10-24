"""Idea 'Write endorsement': the student profile's Solo sign-off tile has a
button that opens a pop-up with six endorsements filled in to copy or print;
solo ones save today as the solo sign-off date (expiry +90 days)."""
from datetime import date, timedelta
from harness import OpsHubTestCase


class StudentEndorsementTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]

    def test_profile_has_button_and_popup(self):
        html = self.login("master").get(f"/flight/students/{self.sid}/edit").get_data(as_text=True)
        self.assertIn("Write endorsement", html)
        self.assertIn('id="endorse-modal"', html)
        self.assertIn("61.87(n)", html)

    def test_save_sets_date_and_90_day_expiry(self):
        r = self.login("master").post(f"/flight/students/{self.sid}/solo-signoff/save")
        self.assertTrue(r.get_json()["ok"])
        row = self.q1("SELECT solo_signoff_date, solo_signoff_expires FROM students WHERE id = ?", (self.sid,))
        self.assertEqual(row["solo_signoff_date"], date.today().isoformat())
        self.assertEqual(row["solo_signoff_expires"], (date.today() + timedelta(days=90)).isoformat())

    def test_student_cannot_save(self):
        r = self.login("flight_student").post(f"/flight/students/{self.sid}/solo-signoff/save")
        self.assertNotEqual(r.status_code, 200)
        self.assertIsNone(self.q1("SELECT solo_signoff_date FROM students WHERE id = ?", (self.sid,))["solo_signoff_date"])


class StudentMilestonesBlockTest(OpsHubTestCase):
    """Idea 'Milestones block': First solo, TSA and Solo sign-off are three
    date boxes with Today buttons; a date means done, blank means not yet."""
    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]
        self.url = f"/flight/students/{self.sid}/edit"

    def test_block_has_no_checkboxes(self):
        html = self.login("master").get(self.url).get_data(as_text=True)
        self.assertIn('id="milestones"', html)
        self.assertNotIn("first_solo_done", html)
        self.assertNotIn("tsa_verified_done", html)
        self.assertIn("change expiry", html)
        self.assertIn("Write endorsement", html)

    def test_dates_save_and_blank_clears(self):
        c = self.login("master")
        name = self.q1("SELECT name FROM students WHERE id = ?", (self.sid,))["name"]
        c.post(self.url, data={"name": name, "email": "a@b.co", "phone": "5551234567", "active": "1", "first_solo_date": "2026-09-01",
                               "tsa_verified_date": "2026-08-01", "solo_signoff_date": "2026-09-01"})
        r = self.q1("SELECT first_solo_date, tsa_verified_date, solo_signoff_date, solo_signoff_expires FROM students WHERE id = ?", (self.sid,))
        self.assertEqual((r["first_solo_date"], r["tsa_verified_date"], r["solo_signoff_date"]), ("2026-09-01", "2026-08-01", "2026-09-01"))
        self.assertEqual(r["solo_signoff_expires"], "2026-11-30")
        c.post(self.url, data={"name": name, "email": "a@b.co", "phone": "5551234567", "active": "1"})
        r = self.q1("SELECT first_solo_date, tsa_verified_date, solo_signoff_date FROM students WHERE id = ?", (self.sid,))
        self.assertEqual((r["first_solo_date"], r["tsa_verified_date"], r["solo_signoff_date"]), (None, None, None))


class EndorsementCopiesTest(OpsHubTestCase):
    """Round 2: a copy of each endorsement is kept on the student's profile and
    on the signing CFI's profile."""
    BODY = "I certify that Test Student has received the required training."

    def setUp(self):
        super().setUp()
        self.sid = self.q1("SELECT id FROM students WHERE user_id = ?", (self.users["flight_student"]["id"],))["id"]

    def _post(self, c, **kw):
        d = dict(kind="pre_knowledge", label="Pre-solo aeronautical knowledge", plane="Cessna 172", body=self.BODY, solo="0")
        d.update(kw)
        return c.post(f"/flight/students/{self.sid}/solo-signoff/save", data=d)

    def test_copy_kept_without_touching_solo_date(self):
        c = self.login("master")
        self.assertTrue(self._post(c).get_json()["ok"])
        self.assertTrue(self._post(c).get_json()["ok"])  # Copy then Print: one row
        n = self.q1("SELECT COUNT(*) AS n FROM student_endorsements WHERE student_id = ?", (self.sid,))["n"]
        self.assertEqual(n, 1)
        self.assertIsNone(self.q1("SELECT solo_signoff_date FROM students WHERE id = ?", (self.sid,))["solo_signoff_date"])

    def test_shown_on_student_and_cfi_profiles(self):
        c = self.login("master")
        self._post(c)
        html = c.get(f"/flight/students/{self.sid}/edit").get_data(as_text=True)
        self.assertIn("Endorsements signed", html)
        self.assertIn(self.BODY, html)
        row = self.q1("SELECT cfi_id FROM student_endorsements WHERE student_id = ?", (self.sid,))
        self.assertTrue(row["cfi_id"])
        html = c.get(f"/flight/cfis/{row['cfi_id']}/edit").get_data(as_text=True)
        self.assertIn(self.BODY, html)

    def test_cfi_sees_own_on_profile(self):
        c = self.login("cfi")
        self._post(c)
        html = c.get("/flight/cfis/me").get_data(as_text=True)
        self.assertIn("Endorsements signed", html)
        self.assertIn(self.BODY, html)
