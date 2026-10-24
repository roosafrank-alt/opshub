"""Flight details pop up: the 'Not time yet' note comes first, then the action buttons, then the date."""
from pathlib import Path
import unittest


class ModalOrderTest(unittest.TestCase):
    def test_note_then_actions_then_date(self):
        html = (Path(__file__).resolve().parent.parent / "templates/flight/_schedule_detail_modal.html").read_text()
        note, actions, date = (html.index(x) for x in ('id="sd-start-locked"', 'id="sd-actions"', 'id="sd-date"'))
        self.assertLess(note, actions)
        self.assertLess(actions, date)
