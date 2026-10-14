"""Parts list on a phone: no Total Parts box, no category button row, Print Labels hidden until a part is ticked."""
import unittest
from pathlib import Path

T = (Path(__file__).resolve().parent.parent / "templates" / "parts.html").read_text()


class PartsPhoneTop(unittest.TestCase):
    def test_total_parts_box_desktop_only(self):
        self.assertIn('col-6 col-md-3 d-none d-md-block', T)

    def test_category_button_row_desktop_only(self):
        self.assertIn('d-none d-md-flex flex-wrap gap-2 mb-3', T)

    def test_print_labels_hidden_on_phone_until_ticked(self):
        self.assertIn('#print-labels-btn:disabled { display: none; }', T)


if __name__ == "__main__":
    unittest.main()
