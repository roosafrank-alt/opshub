"""'How long to a target?' lists every unmet requirement, never just one that is already met."""
from datetime import date, timedelta
from harness import OpsHubTestCase
import pilotlog


class TargetEtaTest(OpsHubTestCase):
    def test_eta_covers_every_unmet_requirement(self):
        track = {"items": [
            {"label": "XC PIC", "kind": "hours", "need": 50, "have": 80.0, "done": True, "flights": [{"amount": 80}]},
            {"label": "Night", "kind": "hours", "need": 40, "have": 10.0, "done": False, "flights": [{"amount": 10}]},
            {"label": "Inst", "kind": "hours", "need": 40, "have": 0.0, "done": False, "flights": []},
            {"label": "Test", "kind": "manual", "need": 1, "have": 0, "done": False, "flights": []}]}
        first = (date.today() - timedelta(days=70)).isoformat()
        eta = pilotlog.target_eta([{"entry_date": first, "total": 20.0}], track)
        labels = [r["label"] for r in eta["rows"]]
        self.assertEqual(labels, ["Night", "Inst"])          # the met one is not the headline
        self.assertEqual(eta["manual_left"], 1)
        self.assertEqual(eta["rows"][0]["weeks"], 30.0)      # 30 to go at 1 hr/week
        self.assertIsNone(eta["rows"][1]["weeks"])           # no instrument time yet: no pace
        self.assertEqual(eta["weeks"], 30.0)
