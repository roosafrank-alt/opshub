"""Phone-size (16px) text on the Assign to... pickers and logbook boxes, so
iPhone Safari does not zoom the page when they are tapped."""
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class PhoneNoZoomFieldsTests(unittest.TestCase):
    def test_phone_css_rule(self):
        css = _read("static", "css", "style.css")
        m = re.search(r"@media \(max-width: 575\.98px\) \{\s*\.squawk-select\.form-select-sm \{ font-size: 16px; \}\s*\.lb-phone16 \{ font-size: 16px !important; \}", css)
        self.assertIsNotNone(m)

    def test_logbook_boxes_use_class(self):
        for name in ("logbook_entry.html", "logbook_starter.html"):
            self.assertIn("lb-phone16", _read("templates", name))


if __name__ == "__main__":
    unittest.main()
