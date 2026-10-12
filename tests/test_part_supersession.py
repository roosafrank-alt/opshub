"""Superseded parts (links old part -> replacement), SHOP-04 (no reorder alerts for
retired parts) and FLY-24 (Flight History From / To date filter)."""
import re
from unittest import mock

from harness import OpsHubTestCase, SIDE_EFFECTS
import db
import notify
import app as opshub_app


class SupersessionBase(OpsHubTestCase):
    def link(self, old, new):
        self.exec("INSERT INTO part_supersessions (old_part_id, new_part_id, created_at) VALUES (?,?,?)",
                  (old, new, db.now_iso()))

    def links(self):
        conn = db.get_db()
        rows = [(r["old_part_id"], r["new_part_id"]) for r in conn.execute("SELECT * FROM part_supersessions")]
        conn.close()
        return rows

    def post_link(self, c, old, label):
        return c.post(f"/parts/{old}/supersede", data={"new_part": label}, follow_redirects=True)


class MigrationTest(SupersessionBase):
    def test_table_exists_and_migration_is_repeatable(self):
        conn = db.get_db()
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(part_supersessions)")]
        self.assertEqual(cols, ["old_part_id", "new_part_id", "created_by", "created_at"])
        db.init_db()  # running startup again on an existing database is harmless
        conn.close()

    def test_creates_table_on_old_database(self):
        conn = db.get_db()
        conn.execute("DROP TABLE part_supersessions")
        conn.commit()
        conn.close()
        db.init_db()
        conn = db.get_db()
        self.assertTrue(conn.execute("SELECT name FROM sqlite_master WHERE name='part_supersessions'").fetchone())
        conn.close()


class LinkingTest(SupersessionBase):
    def setUp(self):
        super().setUp()
        self.a = self.make_part("Old Filter", "A-100", qty=0)
        self.b = self.make_part("Mid Filter", "B-200", qty=0)
        self.c_ = self.make_part("New Filter", "C-300", qty=5)

    def test_admin_links_and_unlinks(self):
        c = self.login("shop_admin")
        r = self.post_link(c, self.a, "B-200 - Mid Filter")
        self.assertIn("Linked", r.get_data(as_text=True))
        self.assertEqual(self.links(), [(self.a, self.b)])
        r = c.post(f"/parts/{self.a}/supersede/remove", data={"old_part_id": self.a, "new_part_id": self.b},
                   follow_redirects=True)
        self.assertIn("Link removed", r.get_data(as_text=True))
        self.assertEqual(self.links(), [])

    def test_tech_can_link(self):
        c = self.login("tech")
        self.post_link(c, self.a, "B-200")
        self.assertEqual(self.links(), [(self.a, self.b)])

    def test_several_replacements_for_one_part(self):
        c = self.login("shop_admin")
        self.post_link(c, self.a, "B-200")
        self.post_link(c, self.a, "C-300")
        self.assertEqual(sorted(self.links()), sorted([(self.a, self.b), (self.a, self.c_)]))

    def test_self_link_rejected(self):
        c = self.login("shop_admin")
        r = self.post_link(c, self.a, "A-100 - Old Filter")
        self.assertIn("cannot be superseded by itself", r.get_data(as_text=True))
        self.assertEqual(self.links(), [])

    def test_loops_rejected(self):
        c = self.login("shop_admin")
        self.post_link(c, self.a, "B-200")
        self.post_link(c, self.b, "C-300")
        r = self.post_link(c, self.c_, "A-100")  # C -> A would close the circle A -> B -> C -> A
        self.assertIn("circle", r.get_data(as_text=True))
        r = self.post_link(c, self.b, "A-100")
        self.assertIn("circle", r.get_data(as_text=True))
        self.assertEqual(sorted(self.links()), sorted([(self.a, self.b), (self.b, self.c_)]))

    def test_duplicate_and_unknown_part(self):
        c = self.login("shop_admin")
        self.post_link(c, self.a, "B-200")
        r = self.post_link(c, self.a, "B-200")
        self.assertIn("already linked", r.get_data(as_text=True))
        r = self.post_link(c, self.a, "NOPE-1")
        self.assertIn("Pick a part from the list", r.get_data(as_text=True))
        self.assertEqual(len(self.links()), 1)

    def test_retired_parts_can_be_linked(self):
        self.exec("UPDATE parts SET retired_at = ? WHERE id = ?", (db.now_iso(), self.a))
        c = self.login("shop_admin")
        self.post_link(c, self.a, "C-300")
        self.assertEqual(self.links(), [(self.a, self.c_)])

    def test_apprentice_and_inspector_cannot_edit(self):
        for role in ("shop_student", "inspector"):
            c = self.login(role)
            c.post(f"/parts/{self.a}/supersede", data={"new_part": "B-200"})
            self.assertEqual(self.links(), [], role)
        self.link(self.a, self.b)
        for role in ("shop_student", "inspector"):
            c = self.login(role)
            c.post(f"/parts/{self.a}/supersede/remove", data={"old_part_id": self.a, "new_part_id": self.b})
            self.assertEqual(self.links(), [(self.a, self.b)], role)

    def test_part_page_shows_link_form_for_admin_and_tech(self):
        for role in ("shop_admin", "tech"):
            html = self.login(role).get(f"/parts/{self.a}").get_data(as_text=True)
            self.assertIn("Superseded by / Replaces", html)
            self.assertIn("This part is superseded by...", html)
            self.assertIn('value="B-200 - Mid Filter"', html)

    def test_deleting_a_part_removes_its_links(self):
        extra = self.make_part("Never Used", "Z-1", qty=0)
        self.link(extra, self.c_)
        self.login("shop_admin").post(f"/parts/{extra}/delete", follow_redirects=True)
        self.assertEqual(self.links(), [])


class ChainAndHintTest(SupersessionBase):
    def setUp(self):
        super().setUp()
        self.a = self.make_part("Old Filter", "A-100", qty=0)
        self.b = self.make_part("Mid Filter", "B-200", qty=0)
        self.c_ = self.make_part("New Filter", "C-300", qty=5)
        self.link(self.a, self.b)
        self.link(self.b, self.c_)

    def test_chain_followed_to_the_end(self):
        conn = db.get_db()
        self.assertEqual([r["barcode"] for r in opshub_app.part_chain(conn, self.a)], ["B-200", "C-300"])
        hints = opshub_app.replacement_hints(conn, [self.a, self.b, self.c_])
        conn.close()
        self.assertEqual([h["barcode"] for h in hints[self.a]], ["B-200", "C-300"])
        self.assertEqual([h["qty"] for h in hints[self.b]], [5])
        self.assertNotIn(self.c_, hints)

    def test_branches_are_all_followed(self):
        d = self.make_part("Branch", "D-400", qty=2)
        e = self.make_part("Branch End", "E-500", qty=3)
        self.link(self.b, d)
        self.link(d, e)
        conn = db.get_db()
        h = opshub_app.replacement_hints(conn, [self.a])[self.a]
        conn.close()
        # B (direct, 0 on hand) is shown, plus every further part that has stock
        self.assertEqual(sorted(x["barcode"] for x in h), ["B-200", "C-300", "D-400", "E-500"])

    def test_banner_on_zero_stock_part(self):
        html = self.login("shop_admin").get(f"/parts/{self.a}").get_data(as_text=True)
        self.assertIn("Superseded by B-200 (Mid Filter) - 0 ea on hand", html)   # 0 on hand is not hidden
        self.assertIn("Superseded by C-300 (New Filter) - 5 ea on hand", html)   # further replacement on its own line
        self.assertIn("B-200 - Mid Filter", html)

    def test_real_unit_is_used(self):
        self.exec("UPDATE parts SET unit = 'ft' WHERE id = ?", (self.c_,))
        html = self.login("shop_admin").get(f"/parts/{self.a}").get_data(as_text=True)
        self.assertIn("Superseded by C-300 (New Filter) - 5 ft on hand", html)

    def test_banner_on_retired_part_with_stock(self):
        self.exec("UPDATE parts SET qty_on_hand = 4, retired_at = ? WHERE id = ?", (db.now_iso(), self.a))
        html = self.login("shop_admin").get(f"/parts/{self.a}").get_data(as_text=True)
        self.assertIn("Superseded by C-300 (New Filter) - 5 ea on hand", html)

    def test_zero_stock_replacement_still_shown(self):
        self.exec("UPDATE parts SET qty_on_hand = 0 WHERE id = ?", (self.c_,))
        html = self.login("shop_admin").get(f"/parts/{self.a}").get_data(as_text=True)
        self.assertIn("Superseded by B-200 (Mid Filter) - 0 ea on hand", html)
        self.assertNotIn("Superseded by C-300", html)  # C is not a direct replacement and has no stock

    def test_no_banner_when_part_itself_has_stock(self):
        self.exec("UPDATE parts SET qty_on_hand = 3 WHERE id = ?", (self.a,))
        html = self.login("shop_admin").get(f"/parts/{self.a}").get_data(as_text=True)
        self.assertNotIn('id="replacement-banner"', html)

    def test_retired_replacement_is_not_offered(self):
        self.exec("UPDATE parts SET retired_at = ? WHERE id = ?", (db.now_iso(), self.c_))
        html = self.login("shop_admin").get(f"/parts/{self.a}").get_data(as_text=True)
        self.assertNotIn("Superseded by C-300", html)

    def test_parts_list_row_hint(self):
        html = self.login("tech").get("/parts?q=Filter").get_data(as_text=True)
        self.assertIn("Superseded by C-300 (New Filter) - 5 ea on hand", html)

    def test_scan_lookup_hint(self):
        j = self.login("tech").get("/api/lookup/A-100").get_json()
        self.assertIn("Superseded by C-300 (New Filter) - 5 ea on hand", j["replacement_hint"])
        j = self.login("tech").get("/api/lookup/C-300").get_json()
        self.assertIsNone(j["replacement_hint"])

    def test_apprentice_sees_nothing_anywhere(self):
        self.exec("UPDATE parts SET reorder_point = 2 WHERE id = ?", (self.a,))
        c = self.login("shop_student")
        j = c.get("/api/lookup/A-100").get_json()
        self.assertNotIn("replacement_hint", j)
        self.assertNotIn("Superseded", str(j))
        self.assertNotIn("unit_cost", j)
        self.assertNotIn("Superseded", c.get("/shop").get_data(as_text=True))
        for url in ("/parts", f"/parts/{self.a}", "/parts?low_stock=1"):
            self.assertNotIn("Superseded by", c.get(url, follow_redirects=True).get_data(as_text=True))

    def test_inspector_scan_lookup_keeps_hint(self):
        j = self.login("inspector").get("/api/lookup/A-100").get_json()
        self.assertIn("Superseded by C-300", j["replacement_hint"])

    def test_low_stock_hint_on_dashboard_and_page_without_changing_counts(self):
        self.exec("UPDATE parts SET reorder_point = 2 WHERE id = ?", (self.a,))
        c = self.login("shop_admin")
        dash = c.get("/shop").get_data(as_text=True)
        self.assertIn("Superseded by C-300 (New Filter) - 5 ea on hand", dash)
        page = c.get("/parts?low_stock=1").get_data(as_text=True)
        self.assertIn("Superseded by C-300 (New Filter) - 5 ea on hand", page)
        conn = db.get_db()
        low = [r["barcode"] for r in opshub_app.get_low_stock(conn)]
        conn.close()
        self.assertIn("A-100", low)  # still counted as low; only a hint was added


class RetiredNoReorderTest(SupersessionBase):
    """SHOP-04: a retired part never triggers or shows a reorder alert."""

    def setUp(self):
        super().setUp()
        self.live = self.make_part("Live Low", "L-1", qty=0, reorder=2)
        self.gone = self.make_part("Retired Low", "R-1", qty=0, reorder=2)
        self.exec("UPDATE parts SET retired_at = ? WHERE id = ?", (db.now_iso(), self.gone))
        self.exec("UPDATE users SET notify_low_stock=1, notify_email=1, email='x@example.com' WHERE username='shop_admin'")

    def test_nightly_alert_skips_retired_parts(self):
        sent = []
        conn = db.get_db()
        with mock.patch.object(notify, "notify_user", side_effect=lambda s, u, subj, body, **k: sent.append(subj) or True):
            n = notify.check_low_stock(conn, notify.get_settings(conn))
        conn.close()
        self.assertEqual(n, 1)
        self.assertEqual(sent, ["Low stock: Live Low"])
        self.assertEqual([e for e in SIDE_EFFECTS if e[0] in ("urlopen", "smtp")], [])

    def test_unretired_part_alerts_again(self):
        self.exec("UPDATE parts SET retired_at = NULL WHERE id = ?", (self.gone,))
        sent = []
        conn = db.get_db()
        with mock.patch.object(notify, "notify_user", side_effect=lambda s, u, subj, body, **k: sent.append(subj) or True):
            notify.check_low_stock(conn, notify.get_settings(conn))
        conn.close()
        self.assertEqual(sorted(sent), ["Low stock: Live Low", "Low stock: Retired Low"])

    def test_dashboard_low_stock_list_and_reorder_prefill_skip_retired(self):
        c = self.login("shop_admin")
        dash = c.get("/shop").get_data(as_text=True)
        self.assertIn("Live Low", dash)
        self.assertNotIn("Retired Low", dash)
        conn = db.get_db()
        self.assertEqual([r["name"] for r in opshub_app.get_low_stock(conn)], ["Live Low"])
        conn.close()
        c.get(f"/orders/new?part_id={self.gone}")
        self.assertNotIn("Retired Low", c.get("/parts?low_stock=1").get_data(as_text=True))

    def test_retired_list_and_detail_show_no_low_stock_warning(self):
        c = self.login("shop_admin")
        html = c.get("/parts?retired=1").get_data(as_text=True)
        self.assertIn("Retired Low", html)
        self.assertNotIn('title="Low stock"', html)
        self.assertNotIn("low-stock-row", html)
        detail = c.get(f"/parts/{self.gone}").get_data(as_text=True)
        self.assertRegex(detail, r'<strong class="">0 ea</strong>')


class FlightHistoryDateFilterTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.asset_id = self.make_asset("N81PA")
        conn = db.get_db()
        self.student_id = conn.execute("SELECT id FROM students WHERE user_id = ?",
                                       (self.users["flight_student"]["id"],)).fetchone()["id"]
        self.cfi_id = conn.execute("SELECT id FROM cfis WHERE user_id = ?",
                                   (self.users["cfi_billing"]["id"],)).fetchone()["id"]
        other = conn.execute("INSERT INTO students (name, username, password_hash, created_at) VALUES ('Other Pilot', 'otherpilot', 'x', ?)", (db.now_iso(),)).lastrowid
        conn.commit()
        conn.close()
        # 130 flights, one per day from 2026-01-01 (older than the latest 100 for the early ones)
        from datetime import date, timedelta
        for i in range(130):
            d = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
            self.exec("INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, hobbs_start, hobbs_end, solo, paid, notes, created_at) "
                      "VALUES (?,?,?,?,?,?,0,0,?,?)", (self.cfi_id, self.student_id, self.asset_id, d, 1.0, 2.0, f"note-{d}", db.now_iso()))
        self.exec("INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, hobbs_start, hobbs_end, solo, paid, notes, created_at) "
                  "VALUES (?,?,?,?,?,?,0,0,?,?)", (self.cfi_id, other, self.asset_id, "2026-01-05", 1.0, 2.0, "other-pilot-note", db.now_iso()))

    def test_old_flights_found_by_server_even_beyond_latest_100(self):
        c = self.login("cfi_billing")
        plain = c.get("/flight/log").get_data(as_text=True)
        self.assertNotIn("note-2026-01-03", plain)       # older than the latest 100
        self.assertIn("Show older flights", plain)
        html = c.get("/flight/log?date_from=2026-01-02&date_to=2026-01-06").get_data(as_text=True)
        for d in ("2026-01-02", "2026-01-03", "2026-01-06"):
            self.assertIn(f"note-{d}", html)
        self.assertNotIn("note-2026-01-01", html)
        self.assertNotIn("note-2026-01-07", html)
        self.assertNotIn("Show older flights", html)
        self.assertIn('name="date_from"', html)
        self.assertIn('id="history-search"', html)       # search box still there
        self.assertIn("Clear dates", html)

    def test_student_only_sees_own_flights_in_range(self):
        c = self.login("flight_student")
        html = c.get("/flight/log?date_from=2026-01-04&date_to=2026-01-06").get_data(as_text=True)
        self.assertIn("note-2026-01-05", html)
        self.assertNotIn("other-pilot-note", html)
        html = self.login("cfi_billing").get("/flight/log?date_from=2026-01-04&date_to=2026-01-06").get_data(as_text=True)
        self.assertIn("other-pilot-note", html)

    def test_from_only_to_only_swap_and_bad_dates(self):
        c = self.login("cfi_billing")
        html = c.get("/flight/log?date_to=2026-01-02").get_data(as_text=True)
        self.assertIn("note-2026-01-01", html)
        self.assertNotIn("note-2026-01-03", html)
        html = c.get("/flight/log?date_from=2026-01-06&date_to=2026-01-02", follow_redirects=True).get_data(as_text=True)
        self.assertIn("note-2026-01-04", html)
        self.assertIn("swapped", html)
        html = c.get("/flight/log?date_from=garbage").get_data(as_text=True)
        self.assertIn("Show older flights", html)

    def test_empty_range_message_and_show_older_keeps_dates(self):
        c = self.login("cfi_billing")
        html = c.get("/flight/log?date_from=2025-01-01&date_to=2025-01-31").get_data(as_text=True)
        self.assertIn("No flights in that date range.", html)
        html = c.get("/flight/log?date_from=2026-01-01&date_to=2026-05-10").get_data(as_text=True)
        self.assertIn("Show older flights", html)
        self.assertRegex(html, r"limit=200[^\"]*date_from=2026-01-01")

    def test_log_past_session_button_still_there(self):
        html = self.login("flight_admin_cfi").get("/flight/log?date_from=2026-01-01").get_data(as_text=True)
        self.assertIn("Log a Past Session", html)
