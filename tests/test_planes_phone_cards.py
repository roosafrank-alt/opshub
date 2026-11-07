"""QA finding ux-planes-phone-cards: on a phone, empty colour/make-model/100-hr
pieces of a plane card are hidden; tail spans the card."""
import db
from harness import OpsHubTestCase


class PlanesPhoneCards(OpsHubTestCase):
    def test_empty_pieces_marked_hidden_on_phone(self):
        self.exec("INSERT INTO assets (tag, name, is_flight_asset, schedule_order, created_at, updated_at) "
                  "VALUES ('N11PC','N11PC',1,10,?,?)", (db.now_iso(), db.now_iso()))
        html = self.login("master").get("/flight/planes").get_data(as_text=True)
        self.assertIn('data-label="Tail #" class="stack-full"', html)
        self.assertIn('data-label="Make/Model" class="empty-sm"', html)
        self.assertIn('data-label="" class="empty-sm"', html)
        self.assertIn("td.empty-sm{display:none}", html)
