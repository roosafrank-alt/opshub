import unittest
from unittest import mock

import pts_parser

SAMPLE = """Section 1
I. AREA OF OPERATION: PREFLIGHT PREPARATION
A. TASK: CERTIFICATES AND DOCUMENTS (ASEL)
REFERENCES: 14 CFR part 61;
FAA-H-8083-25.
Objective. To determine that the applicant exhibits knowledge of the elements related to certificates by:
1. Explaining—
a. certificate privileges.
b. medical eligibility.
21
II. AREA OF OPERATION: PREFLIGHT PROCEDURES
NOTE: Area note.
A. TASK: TAXIING (ASEL)
REFERENCES: FAA-H-8083-3.
Objective. To determine that the applicant:
1. Exhibits knowledge of the elements related to taxiing.
2. Maintains directional control
and proper wind-drift correction.
Section 2
I. AREA OF OPERATION: OTHER
A. TASK: IGNORED
Objective. To determine that the applicant:
1. Nothing.
""".split("\n")


class PtsParserTests(unittest.TestCase):
    def parse(self, section=1):
        with mock.patch.object(pts_parser, "_lines", return_value=[l for l in SAMPLE if not l.isdigit()]):
            return pts_parser.parse_pts("x.pdf", section)

    def test_areas_tasks_elements(self):
        areas = self.parse()
        self.assertEqual([a["code"] for a in areas], ["I", "II"])
        t1 = areas[0]["tasks"][0]
        self.assertEqual(t1["references"], "14 CFR part 61; FAA-H-8083-25.")
        self.assertEqual(len(t1["knowledge"]), 1)
        self.assertIn("medical eligibility", t1["knowledge"][0]["text"])
        t2 = areas[1]["tasks"][0]
        self.assertEqual(t2["notes"], ["Area note."])
        self.assertEqual(t2["knowledge"][0]["code"], "SP.II.A.K1")
        self.assertEqual(t2["skills"][0]["text"], "Maintains directional control and proper wind-drift correction.")

    def test_other_section_is_separate(self):
        areas = self.parse(2)
        self.assertEqual([t["title"] for a in areas for t in a["tasks"]], ["IGNORED"])


if __name__ == "__main__":
    unittest.main()
