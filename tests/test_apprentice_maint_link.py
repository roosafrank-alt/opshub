"""An Apprentice's aircraft page shows only the maintenance items linked to
their own task; admin and tech link items on the full project page."""
from harness import OpsHubTestCase
import db


class MaintLinkTests(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.me = self.users["shop_student"]["id"]
        self.asset = self.make_asset("N111AA")
        self.other_asset = self.make_asset("N222BB")
        self.proj = self.make_project("Annual - N111AA", asset_id=self.asset)
        self.exec("UPDATE projects SET intake_status='done' WHERE id=?", (self.proj,))
        self.sec = self.exec("INSERT INTO project_sections (project_id,name,created_at,assigned_to) VALUES (?,?,?,?)",
                             (self.proj, "Brakes", db.now_iso(), self.me))
        self.m_linked = self._item(self.asset, "Brake check")
        self.m_other = self._item(self.asset, "Oil change")
        self.m_foreign = self._item(self.other_asset, "Foreign item")

    def _item(self, asset, name):
        return self.exec("INSERT INTO maintenance_items (asset_id,name,type,interval_days,active) VALUES (?,?,?,?,1)",
                         (asset, name, "calendar", 365))

    def _link(self, role, ids):
        return self.login(role).post(f"/projects/{self.proj}/sections/{self.sec}/maint-items",
                                     data={"maint_item_id": [str(i) for i in ids]}, follow_redirects=True)

    def _linked(self):
        return {r["maint_item_id"] for r in self.q("SELECT maint_item_id FROM project_section_maint_items WHERE section_id=?", (self.sec,))}

    def test_admin_links_items(self):
        r = self._link("shop_admin", [self.m_linked])
        self.assertIn("linked to 1 maintenance item", r.get_data(as_text=True))
        self.assertEqual(self._linked(), {self.m_linked})
        body = self.login("shop_admin").get(f"/projects/{self.proj}").get_data(as_text=True)
        self.assertIn("Related maintenance items: Brake check", body)

    def test_tech_links_and_clears(self):
        self._link("tech", [self.m_linked, self.m_other])
        self.assertEqual(self._linked(), {self.m_linked, self.m_other})
        self._link("tech", [])
        self.assertEqual(self._linked(), set())

    def test_apprentice_cannot_link(self):
        self._link("shop_student", [self.m_linked])
        self.assertEqual(self._linked(), set())

    def test_other_aircraft_item_rejected(self):
        r = self._link("shop_admin", [self.m_linked, self.m_foreign])
        self.assertIn("aircraft only", r.get_data(as_text=True))
        self.assertEqual(self._linked(), set())

    def test_apprentice_sees_only_linked(self):
        self._link("shop_admin", [self.m_linked])
        body = self.login("shop_student").get(f"/assets/{self.asset}").get_data(as_text=True)
        self.assertIn("Brake check", body)
        self.assertNotIn("Oil change", body)
        self.assertNotIn("Foreign item", body)
        for page in (f"/projects/{self.proj}", "/my-tasks"):
            b = self.login("shop_student").get(page).get_data(as_text=True)
            self.assertIn("Brake check", b)
            self.assertNotIn("Oil change", b)

    def test_another_apprentice_sees_none(self):
        # The linked task belongs to someone else; this apprentice has a different, unlinked task on the same aircraft.
        self._link("shop_admin", [self.m_linked])
        self.exec("UPDATE project_sections SET assigned_to=? WHERE id=?", (self.users["tech"]["id"], self.sec))
        self.exec("INSERT INTO project_sections (project_id,name,created_at,assigned_to) VALUES (?,?,?,?)",
                  (self.proj, "Engine", db.now_iso(), self.me))
        body = self.login("shop_student").get(f"/assets/{self.asset}").get_data(as_text=True)
        self.assertNotIn("Brake check", body)
        self.assertIn("No maintenance items are linked to your task yet. Ask an admin.", body)

    def test_unlinked_message(self):
        body = self.login("shop_student").get(f"/assets/{self.asset}").get_data(as_text=True)
        self.assertIn("No maintenance items are linked to your task yet. Ask an admin.", body)
        self.assertNotIn("Oil change", body)

    def test_migration_creates_table(self):
        conn = db.get_db()
        conn.execute("DROP TABLE project_section_maint_items")
        conn.commit()
        conn.close()
        db.init_db()
        cols = [r["name"] for r in self.q("PRAGMA table_info(project_section_maint_items)")]
        self.assertEqual(sorted(cols), ["maint_item_id", "section_id"])
