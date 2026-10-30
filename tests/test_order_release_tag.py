"""Release tag (8130-3 / CoC) photo snapped at Received follows the part onto the job."""
import io
from harness import OpsHubTestCase
import db

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
       b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")


class ReleaseTagTest(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self.login("shop_admin")
        c = db.get_db()
        self.pid = c.execute("INSERT INTO parts (barcode, name) VALUES ('TAG-1', 'Tag Filter')").lastrowid
        self.job = c.execute("INSERT INTO projects (name) VALUES ('Annual')").lastrowid
        c.commit(); c.close()

    def order(self):
        c = db.get_db()
        oid = c.execute("INSERT INTO orders (part_id, description, qty_ordered) VALUES (?, 'Tag Filter', 5)",
                        (self.pid,)).lastrowid
        c.commit(); c.close()
        return oid

    def test_receive_with_photo_shows_on_part_and_job(self):
        oid = self.order()
        self.client.post(f"/orders/{oid}/receive", data={"tag_photo": (io.BytesIO(PNG), "tag.png")},
                         content_type="multipart/form-data")
        c = db.get_db()
        tag = c.execute("SELECT tag_photo FROM orders WHERE id=?", (oid,)).fetchone()[0]
        self.assertTrue(tag)
        c.execute("INSERT INTO transactions (part_id, project_id, type, qty) VALUES (?, ?, 'out', 1)",
                  (self.pid, self.job))
        c.commit(); c.close()
        part = self.client.get(f"/parts/{self.pid}").get_data(as_text=True)
        self.assertIn("Paperwork", part)
        self.assertIn(tag, part)
        job = self.client.get(f"/projects/{self.job}").get_data(as_text=True)
        self.assertIn(tag, job)

    def test_receive_without_photo_still_works(self):
        oid = self.order()
        self.client.post(f"/orders/{oid}/receive")
        c = db.get_db()
        row = c.execute("SELECT status, tag_photo FROM orders WHERE id=?", (oid,)).fetchone()
        c.close()
        self.assertEqual(row["status"], "received")
        self.assertIsNone(row["tag_photo"])
        self.assertEqual(self.client.get(f"/parts/{self.pid}").status_code, 200)
