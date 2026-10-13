"""Flight Academy Resources: natural sort and grouping by certificate category."""
from harness import OpsHubTestCase
import db
import groundschool as gs

GS = "/flight/groundschool"


class ResourcesCategoriesTest(OpsHubTestCase):
    def add(self, title, designation=None, categories=None, doc_type="14 CFR Part"):
        self.exec("INSERT INTO acs_resources (title, designation, categories, doc_type, filename, uploaded_at) "
                  "VALUES (?,?,?,?,?,?)", (title, designation, categories, doc_type, "x.pdf", db.now_iso()))

    def test_natural_key(self):
        names = ["14 CFR Part 91", "14 CFR Part 7", "AC 61-100", "14 CFR Part 61", "AC 61-65"]
        self.assertEqual(sorted(names, key=gs.natural_key),
                         ["14 CFR Part 7", "14 CFR Part 61", "14 CFR Part 91", "AC 61-65", "AC 61-100"])

    def test_grouping_order_multi_and_other(self):
        self.add("Rules", "14 CFR Part 91", "Private,Sport")
        self.add("Rules7", "14 CFR Part 7", "Private")
        self.add("Handbook")
        rows = self.q("SELECT * FROM acs_resources")
        secs = gs.group_resources(rows)
        self.assertEqual([n for n, _ in secs], ["Sport", "Private", "Other / General"])
        self.assertEqual([r["designation"] for r in secs[1][1]], ["14 CFR Part 7", "14 CFR Part 91"])
        self.assertEqual(len(secs[0][1]), 1)

    def test_page_edit_and_upload_form(self):
        self.add("Rules", "14 CFR Part 61")
        rid = self.q1("SELECT id FROM acs_resources")["id"]
        c = self.login("master")
        c.post(f"{GS}/resources/{rid}/edit", data={"title": "Rules", "categories": ["CFI", "Private"]})
        self.assertEqual(self.q1("SELECT categories FROM acs_resources")["categories"], "Private,CFI")
        body = c.get(f"{GS}/resources").get_data(as_text=True)
        self.assertIn('data-section="Private"', body)
        self.assertIn('data-section="CFI"', body)
        self.assertNotIn('data-section="Other / General"', body)
        self.assertIn('name="categories"', body)
