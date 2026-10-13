"""Flight Academy Resources show which FAA document each one is: Designation,
Date, Revision and Effectivity, editable by admins without re-uploading."""
from harness import OpsHubTestCase
import db

GS = "/flight/groundschool"


class ResourcesDesignationTest(OpsHubTestCase):
    def add(self, title="General Operating and Flight Rules", **kw):
        cols = dict(title=title, filename="x.pdf", doc_type="14 CFR Part", uploaded_at=db.now_iso(), **kw)
        self.exec(f"INSERT INTO acs_resources ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                  tuple(cols.values()))
        return self.q1("SELECT id FROM acs_resources ORDER BY id DESC")["id"]

    def test_migration_adds_columns(self):
        cols = [r["name"] for r in self.q("PRAGMA table_info(acs_resources)")]
        for c in ("designation", "doc_date", "revision", "effectivity"):
            self.assertIn(c, cols)
        db.init_db()

    def test_table_shows_designation_dash_title_and_columns(self):
        self.add(designation="14 CFR Part 91", doc_date="2026-09-29", revision="Change 1", effectivity="2026-10-01")
        body = self.login("master").get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn("14 CFR Part 91 - General Operating and Flight Rules", body)
        for t in ("Date", "Revision", "Effectivity", "Change 1", "2026-10-01", "2026-09-29"):
            self.assertIn(t, body)

    def test_old_title_when_no_designation(self):
        self.add(title="Old Style Title")
        body = self.login("master").get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn("Old Style Title", body)
        self.assertNotIn("None - Old Style", body)

    def test_admin_edit_changes_labels_only(self):
        rid = self.add()
        self.exec("INSERT INTO acs_resource_pages (resource_id, page_num, text) VALUES (?,1,'hello')", (rid,))
        self.login("master").post(f"{GS}/resources/{rid}/edit", data={
            "title": "Fixed Title", "designation": "AC 61-65K", "doc_date": "2026-01-02",
            "revision": "K", "effectivity": ""})
        r = self.q1("SELECT * FROM acs_resources WHERE id = ?", (rid,))
        self.assertEqual((r["title"], r["designation"], r["doc_date"], r["revision"], r["effectivity"], r["filename"]),
                         ("Fixed Title", "AC 61-65K", "2026-01-02", "K", None, "x.pdf"))
        self.assertEqual(self.q("SELECT COUNT(*) AS c FROM acs_resource_pages")[0]["c"], 1)

    def test_edit_button_admin_only_and_edit_refused_for_others(self):
        rid = self.add()
        self.assertIn("resource-edit-btn", self.login("master").get(f"{GS}/resources").get_data(as_text=True))
        for who in ("cfi", "flight_student"):
            c = self.login(who)
            self.assertNotIn("resource-edit-btn", c.get(f"{GS}/resources").get_data(as_text=True))
            c.post(f"{GS}/resources/{rid}/edit", data={"title": "Hacked"})
        self.assertEqual(self.q1("SELECT title FROM acs_resources WHERE id = ?", (rid,))["title"],
                         "General Operating and Flight Rules")
