"""Photos: upload (bad types, several files, missing owner), set cover, delete, permissions.

Uploads go to a throwaway folder, never static/uploads.
"""
import io
import os
import shutil
import tempfile

from harness import OpsHubTestCase, seed_row, open_finding
import db


class PhotoBase(OpsHubTestCase):
    def setUp(self):
        super().setUp()
        self._up = tempfile.mkdtemp()
        self._old_up = db.UPLOAD_DIR
        db.UPLOAD_DIR = self._up
        import app as app_module
        self._old_app_up = getattr(app_module, "UPLOAD_DIR", None)
        app_module.UPLOAD_DIR = self._up
        self._app_module = app_module

    def tearDown(self):
        db.UPLOAD_DIR = self._old_up
        if self._old_app_up is not None:
            self._app_module.UPLOAD_DIR = self._old_app_up
        shutil.rmtree(self._up, ignore_errors=True)
        super().tearDown()

    def upload(self, url, *names):
        data = {"photos": [(io.BytesIO(b"\x89PNG fake"), n) for n in names]}
        return self.client.post(url, data=data, content_type="multipart/form-data")

    def photos(self, col, owner):
        return self.q(f"SELECT * FROM photos WHERE {col}=? ORDER BY id", (owner,))


class UploadTest(PhotoBase):
    def test_tech_uploads_to_part_project_and_asset(self):
        part = self.make_part()
        proj = self.make_project()
        asset = self.make_asset()
        self.login("tech")
        for url, col, oid in ((f"/parts/{part}/photos", "part_id", part),
                              (f"/projects/{proj}/photos", "project_id", proj),
                              (f"/assets/{asset}/photos", "asset_id", asset)):
            r = self.upload(url, "a.png")
            self.assertEqual(r.status_code, 302, url)
            rows = self.photos(col, oid)
            self.assertEqual(len(rows), 1, url)
            self.assertTrue(os.path.exists(os.path.join(self._up, rows[0]["filename"])))

    def test_several_files_in_one_upload(self):
        part = self.make_part()
        self.login("shop_admin")
        self.upload(f"/parts/{part}/photos", "a.png", "b.jpg", "c.JPEG")
        self.assertEqual(len(self.photos("part_id", part)), 3)

    def test_non_image_files_skipped(self):
        part = self.make_part()
        self.login("shop_admin")
        self.upload(f"/parts/{part}/photos", "evil.exe", "notes.txt", "noext", "page.html")
        self.assertEqual(len(self.photos("part_id", part)), 0)
        self.assertEqual(os.listdir(self._up), [])

    def test_mixed_batch_keeps_only_images(self):
        part = self.make_part()
        self.login("shop_admin")
        self.upload(f"/parts/{part}/photos", "a.png", "x.exe")
        self.assertEqual(len(self.photos("part_id", part)), 1)

    def test_empty_upload_is_friendly(self):
        part = self.make_part()
        self.login("shop_admin")
        r = self.client.post(f"/parts/{part}/photos", data={})
        self.assertEqual(r.status_code, 302)

    def test_stored_name_ignores_path_tricks(self):
        part = self.make_part()
        self.login("shop_admin")
        self.upload(f"/parts/{part}/photos", "../../etc/passwd.png")
        row = self.photos("part_id", part)[0]
        self.assertNotIn("/", row["filename"])
        self.assertNotIn("..", row["filename"])
        self.assertTrue(os.path.exists(os.path.join(self._up, row["filename"])))

    def test_missing_owner_is_404(self):
        self.login("shop_admin")
        for url in ("/parts/9999/photos", "/projects/9999/photos", "/assets/9999/photos"):
            self.assertEqual(self.upload(url, "a.png").status_code, 404, url)

    def test_roles_without_shop_access_cannot_upload(self):
        part = self.make_part()
        for role in ("shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            self.upload(f"/parts/{part}/photos", "a.png")
            self.assertEqual(len(self.photos("part_id", part)), 0, role)

    def test_disk_full_rolls_back_whole_batch(self):
        part = self.make_part()
        self.login("shop_admin")
        real = self._app_module.save_upload
        calls = []

        def flaky(f):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(f)
        self._app_module.save_upload = flaky
        try:
            r = self.upload(f"/parts/{part}/photos", "a.png", "b.png")
        finally:
            self._app_module.save_upload = real
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.photos("part_id", part)), 0)


class CoverAndDeleteTest(PhotoBase):
    def _two(self):
        part = self.make_part()
        self.login("shop_admin")
        self.upload(f"/parts/{part}/photos", "a.png", "b.png")
        rows = self.photos("part_id", part)
        return part, rows[0]["id"], rows[1]["id"]

    def test_set_cover_moves_between_photos(self):
        part, a, b = self._two()
        self.client.post(f"/photos/{a}/set_cover")
        self.client.post(f"/photos/{b}/set_cover")
        covers = [r["id"] for r in self.photos("part_id", part) if r["is_cover"]]
        self.assertEqual(covers, [b])

    def test_cover_is_per_owner(self):
        part, a, b = self._two()
        proj = self.make_project()
        self.upload(f"/projects/{proj}/photos", "p.png")
        pid = self.photos("project_id", proj)[0]["id"]
        self.client.post(f"/photos/{a}/set_cover")
        self.client.post(f"/photos/{pid}/set_cover")
        self.assertEqual(self.q1("SELECT is_cover FROM photos WHERE id=?", (a,))["is_cover"], 1)

    def test_delete_removes_row_and_file(self):
        part, a, b = self._two()
        fn = self.q1("SELECT filename FROM photos WHERE id=?", (a,))["filename"]
        r = self.client.post(f"/photos/{a}/delete")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.q1("SELECT id FROM photos WHERE id=?", (a,)))
        self.assertFalse(os.path.exists(os.path.join(self._up, fn)))
        self.assertIsNotNone(self.q1("SELECT id FROM photos WHERE id=?", (b,)))

    def test_delete_when_file_already_gone(self):
        part, a, b = self._two()
        fn = self.q1("SELECT filename FROM photos WHERE id=?", (a,))["filename"]
        os.remove(os.path.join(self._up, fn))
        self.assertEqual(self.client.post(f"/photos/{a}/delete").status_code, 302)
        self.assertIsNone(self.q1("SELECT id FROM photos WHERE id=?", (a,)))

    def test_double_delete_is_404_not_500(self):
        part, a, b = self._two()
        self.client.post(f"/photos/{a}/delete")
        self.assertEqual(self.client.post(f"/photos/{a}/delete").status_code, 404)

    def test_missing_photo_404(self):
        self.login("shop_admin")
        self.assertEqual(self.client.post("/photos/9999/delete").status_code, 404)
        self.assertEqual(self.client.post("/photos/9999/set_cover").status_code, 404)

    def test_tech_can_set_cover_but_not_delete(self):
        part, a, b = self._two()
        self.login("tech")
        self.client.post(f"/photos/{b}/set_cover")
        self.assertEqual(self.q1("SELECT is_cover FROM photos WHERE id=?", (b,))["is_cover"], 1)
        self.client.post(f"/photos/{a}/delete")
        self.assertIsNotNone(self.q1("SELECT id FROM photos WHERE id=?", (a,)))

    def test_other_roles_cannot_change_photos(self):
        part, a, b = self._two()
        for role in ("shop_student", "cfi", "flight_student", "no_roles"):
            self.login(role)
            self.client.post(f"/photos/{a}/set_cover")
            self.client.post(f"/photos/{a}/delete")
            row = self.q1("SELECT * FROM photos WHERE id=?", (a,))
            self.assertIsNotNone(row, role)
            self.assertFalse(row["is_cover"], role)
