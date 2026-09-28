"""Frank reported: "Printer does not print any barcodes now. Shows green
sent success pop up but flashes red and no print." brother_ql's send() only
raises when it can't reach the printer at all - if the USB connection works
but the printer silently fails to actually produce the label (out of paper,
cover open, jammed), send() still returns normally with a status dict
(did_print=False, maybe an errors list). label_printer.py never checked that
return value, so every print route's `except Exception` never fired and the
false "Label sent to printer" success flash went out anyway.

label_printer._raise_if_not_printed(result) is the fix: it inspects the
status dict brother_ql's send() returns and raises when the print didn't
really happen, so the existing except-block in each app.py print route
flashes a real error instead. This test exercises it directly (brother_ql
itself isn't installed in this sandbox - it only runs on the Pi - but this
helper is pure Python with no brother_ql import, so it doesn't need it)."""
import unittest

import label_printer


class RaiseIfNotPrintedTest(unittest.TestCase):
    def test_real_success_does_not_raise(self):
        # what a normal, successful print looks like
        label_printer._raise_if_not_printed({
            "instructions_sent": True, "outcome": "printed",
            "printer_state": {"errors": [], "status_type": "Printing completed"},
            "did_print": True, "ready_for_next_job": True,
        })  # should not raise

    def test_silent_failure_raises(self):
        # printer connects fine and send() completes without an exception,
        # but the printer never confirmed it actually printed - the "green
        # success, nothing came out" bug
        with self.assertRaises(Exception):
            label_printer._raise_if_not_printed({
                "instructions_sent": True, "outcome": "sent",
                "printer_state": None, "did_print": False,
                "ready_for_next_job": False,
            })

    def test_out_of_paper_raises_with_clear_message(self):
        with self.assertRaises(Exception) as ctx:
            label_printer._raise_if_not_printed({
                "instructions_sent": True, "outcome": "error",
                "printer_state": {"errors": ["No media when printing"]},
                "did_print": False, "ready_for_next_job": False,
            })
        self.assertIn("No media when printing", str(ctx.exception))

    def test_cover_open_raises_with_clear_message(self):
        with self.assertRaises(Exception) as ctx:
            label_printer._raise_if_not_printed({
                "instructions_sent": True, "outcome": "error",
                "printer_state": {"errors": ["Cover opened while printing (Except QL-500)"]},
                "did_print": False, "ready_for_next_job": False,
            })
        self.assertIn("Cover opened", str(ctx.exception))

    def test_none_result_raises(self):
        with self.assertRaises(Exception):
            label_printer._raise_if_not_printed(None)


if __name__ == "__main__":
    unittest.main()
