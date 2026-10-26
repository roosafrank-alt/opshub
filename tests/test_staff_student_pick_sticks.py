"""A staff member's chosen student stays selected when moving between Academy tabs."""
from harness import OpsHubTestCase


class StaffStudentPickSticksTest(OpsHubTestCase):
    def test_pick_carries_to_other_tabs(self):
        for i in range(2):
            self.exec("INSERT INTO students (name, username, password_hash, active) VALUES (?, ?, ?, 1)", ("Zed Pick%d" % i, "zp%d" % i, "x"))
        last = self.q1("SELECT id, name FROM students WHERE name = 'Zed Pick1'")
        c = self.login("flight_admin_cfi")
        c.get("/academy/logbook", query_string={"student_id": last["id"]})
        for url in ("/academy/progress", "/academy/endorsements/", "/academy/map"):
            html = c.get(url).get_data(as_text=True)
            self.assertRegex(html, r'<option value="%d"\s+selected' % last["id"], url)
