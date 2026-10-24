"""One 'Payments to confirm' menu line / Billing button with a combined count,
and one page: card requests first, then cash/check collected. Old URLs still
land on the right section."""
from test_cash_check_confirm import _Base

PAGE = "/flight/billing/to-confirm"


class PaymentsToConfirmTest(_Base):
    def setUp(self):
        super().setUp()
        self.connect_wave()

    def make_card_request(self):
        fid = self.running()
        self.login("flight_student").post(f"/flight/log/{fid}/end", data=dict(
            hobbs_end="101.0", tach_end="90.8", charge_choice="wave", consent="1"))

    def test_combined_count_page_and_old_links(self):
        self.pay("cfi_billing", self.ended(), "Cash")
        self.make_card_request()
        for role in ("master", "flight_admin"):
            c = self.login(role)
            menu = c.get("/flight/dashboard").get_data(as_text=True)
            self.assertIn("Payments to confirm", menu, role)
            self.assertNotIn("Card requests</a>", menu, role)
            self.assertNotIn("Cash/check collected</a>", menu, role)
            self.assertIn('badge bg-danger">2</span>', menu, role)
            html = c.get(PAGE).get_data(as_text=True)
            self.assertLess(html.index('id="cards"'), html.index('id="cash"'))
            for want in ("Accept &amp; bill", "Received at office", "Not received"):
                self.assertIn(want, html, (role, want))
        html = self.login("master").get("/flight/billing", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Payments to confirm", html)
        c = self.login("master")
        r = c.get("/flight/billing/approvals")
        self.assertEqual((r.status_code, r.headers["Location"].endswith(PAGE + "#cards")), (302, True))
        r = c.get("/flight/billing/cash-check?sort=date")
        self.assertEqual((r.status_code, r.headers["Location"].endswith(PAGE + "?sort=date#cash")), (302, True))

    def test_other_roles_cannot_open_it(self):
        for role in ("cfi", "cfi_billing", "shop_admin", "flight_student"):
            self.assertEqual(self.login(role).get(PAGE).status_code, 302, role)
