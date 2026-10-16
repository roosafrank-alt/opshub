"""Compression page: each cylinder box gets OK / Watch / LOW against the last
check, a red or yellow note appears above the boxes, and the red note has an
Add to discrepancy button that files a squawk on the plane."""
from harness import OpsHubTestCase
import db


class CompressionStatus(OpsHubTestCase):
    def _check(self, aid, date, cyls, master=80):
        cols = {f"cyl{i + 1}": v for i, v in enumerate(cyls)}
        names = ", ".join(cols)
        self.exec(f"INSERT INTO compression_checks (asset_id, checked_date, tach_hours, master_orifice, {names}, created_at) "
                  f"VALUES (?, ?, 100, ?, {','.join('?' * len(cols))}, ?)",
                  [aid, date, master] + list(cols.values()) + [db.now_iso()])

    def _page(self, aid, role="tech"):
        return self.login(role).get(f"/assets/{aid}/compression").get_data(as_text=True)

    def test_levels_and_note(self):
        aid = self.make_asset()
        self._check(aid, "2026-06-02", [75, 75, 72, 76])
        self._check(aid, "2026-10-01", [75, 68, 61, 71])
        html = self._page(aid)
        self.assertIn("Watch · -7 since last", html)
        self.assertIn("LOW · -11 since last", html)
        self.assertIn("Cyl 3 is low: 61/80, down 11 since Jun 2", html)
        self.assertIn("Cyl 2 is on watch (68)", html)
        self.assertIn("Add to discrepancy", html)
        self.assertIn(">OK<", html)

    def test_yellow_only_has_no_button(self):
        aid = self.make_asset()
        self._check(aid, "2026-06-02", [75, 75])
        self._check(aid, "2026-10-01", [75, 68])
        html = self._page(aid)
        self.assertIn("Cyl 2 is on watch (68)", html)
        self.assertNotIn("Add to discrepancy", html)

    def test_no_note_when_all_ok_or_steady(self):
        aid = self.make_asset()
        self._check(aid, "2026-06-02", [68, 76])
        self._check(aid, "2026-10-01", [68, 76])
        html = self._page(aid)
        self.assertNotIn('id="cc-note"', html)
        self.assertNotIn("Watch", html)

    def test_scales_with_other_reference(self):
        aid = self.make_asset()
        self._check(aid, "2026-10-01", [50], master=100)  # 50/100 = 40/80
        self.assertIn("LOW", self._page(aid))

    def test_button_adds_squawk(self):
        aid = self.make_asset()
        self._check(aid, "2026-06-02", [75])
        self._check(aid, "2026-10-01", [61])
        c = self.login("tech")
        c.post(f"/assets/{aid}/compression/discrepancy")
        row = self.q1("SELECT notes FROM plane_squawks WHERE asset_id = ?", (aid,))
        self.assertEqual(row["notes"], "Compression: Cyl 1 is low: 61/80, down 14 since Jun 2")

    def test_button_refused_when_nothing_low(self):
        aid = self.make_asset()
        self._check(aid, "2026-10-01", [76])
        self.login("tech").post(f"/assets/{aid}/compression/discrepancy")
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM plane_squawks")["n"], 0)

    def test_already_added_note_and_no_duplicate(self):
        aid = self.make_asset()
        self._check(aid, "2026-06-02", [75])
        self._check(aid, "2026-10-01", [61])
        c = self.login("tech")
        self.assertIn("Add to discrepancy", c.get(f"/assets/{aid}/compression").get_data(as_text=True))
        c.post(f"/assets/{aid}/compression/discrepancy")
        c.post(f"/assets/{aid}/compression/discrepancy")
        self.assertEqual(self.q1("SELECT COUNT(*) n FROM plane_squawks WHERE asset_id = ?", (aid,))["n"], 1)
        html = c.get(f"/assets/{aid}/compression").get_data(as_text=True)
        self.assertIn("Already added to discrepancies", html)
        self.assertNotIn(">Add to discrepancy<", html)
