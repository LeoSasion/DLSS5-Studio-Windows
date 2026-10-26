"""Application update transactions never include or overwrite user media/data."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

MODULE = Path(__file__).resolve().parents[1] / "packaging/incremental_update.py"
SPEC = importlib.util.spec_from_file_location("dlss_incremental_update", MODULE)
update = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(update)


class IncrementalUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dlss-update-")
        self.root = Path(self.temp.name)
        self.source, self.target = self.root / "source", self.root / "target"
        for root, prefix in ((self.source, b"new"), (self.target, b"old")):
            for name in ("app/serve_local.py", "app/web/index.html", "DLSS Studio.exe"):
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(prefix + name.encode())
        (self.source / "app/new_feature.py").write_bytes(b"new feature")
        self.protected = {"app/uploads/source/private.png": b"user media", "app/ui_out/result/master.mkv": b"user master",
                          "app/studio-state.json": b"user history", "desktop-profile/preferences": b"user presets",
                          "app/out/dlss5/rtx50/nvngx_dlssnr.dll": b"user selected runtime"}
        for name, data in self.protected.items():
            for root in (self.target, self.source):
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        self.manifest = {"existing": {"bytes": 1, "sha256": "kept"}}
        (self.target / "manifest.json").write_text(json.dumps(self.manifest))
        self.archive = self.root / "update.zip"
        update.create(self.source, self.archive)

    def tearDown(self):
        self.temp.cleanup()

    def assert_protected(self):
        for name, data in self.protected.items():
            self.assertEqual((self.target / name).read_bytes(), data, name)

    def assert_old(self):
        self.assertEqual((self.target / "app/serve_local.py").read_bytes(), b"oldapp/serve_local.py")
        self.assertEqual((self.target / "app/web/index.html").read_bytes(), b"oldapp/web/index.html")
        self.assertFalse((self.target / "app/new_feature.py").exists())
        self.assertEqual(json.loads((self.target / "manifest.json").read_text()), self.manifest)
        self.assert_protected()

    def assert_new(self):
        self.assertEqual((self.target / "app/serve_local.py").read_bytes(), b"newapp/serve_local.py")
        self.assertEqual((self.target / "app/web/index.html").read_bytes(), b"newapp/web/index.html")
        self.assertEqual((self.target / "app/new_feature.py").read_bytes(), b"new feature")
        self.assert_protected()

    def test_apply_and_rollback_include_manifest_and_preserve_all_data(self):
        with zipfile.ZipFile(self.archive) as archive:
            self.assertFalse(set(self.protected) & set(archive.namelist()))
        revision = update.apply_update(self.target, self.archive)
        self.assert_new()
        self.assertIn("app/new_feature.py", json.loads((self.target / "manifest.json").read_text()))
        update.rollback(self.target, revision)
        self.assert_old()

    def test_commit_failure_restores_application_and_package_manifest(self):
        original = update.journal
        def fail_commit(path, value):
            if value.get("status") == "applied":
                raise OSError("simulated journal commit failure")
            return original(path, value)
        with patch.object(update, "journal", side_effect=fail_commit):
            with self.assertRaises(OSError):
                update.apply_update(self.target, self.archive)
        self.assert_old()
        records = list((self.target / "updates/history").glob("*/rollback.json"))
        self.assertEqual(json.loads(records[0].read_text())["status"], "failed_restored")

    def test_rollback_failure_restores_installed_version_transactionally(self):
        revision = update.apply_update(self.target, self.archive)
        original = update.atomic_copy; failed = [False]
        def fail_one(source, target):
            if not failed[0] and Path(target).resolve() == (self.target / "app/serve_local.py").resolve() and "before" in Path(source).parts:
                failed[0] = True
                raise OSError("simulated rollback failure")
            return original(source, target)
        with patch.object(update, "atomic_copy", side_effect=fail_one):
            with self.assertRaises(RuntimeError):
                update.rollback(self.target, revision)
        self.assert_new()
        update.rollback(self.target, revision)
        self.assert_old()

    def test_user_changes_or_corrupt_backup_are_rejected_before_any_restore(self):
        revision = update.apply_update(self.target, self.archive)
        path = self.target / "app/web/index.html"; path.write_bytes(b"user edited application")
        with self.assertRaises(ValueError):
            update.rollback(self.target, revision)
        self.assertEqual(path.read_bytes(), b"user edited application")
        self.assertEqual((self.target / "app/serve_local.py").read_bytes(), b"newapp/serve_local.py")
        self.assert_protected()

    def test_windows_ambiguous_paths_and_case_collisions_are_not_valid_updates(self):
        for name in ("app/web/../uploads/source.png", "app/web/.. /private", "app/web/NUL.txt", "app/web/a.", "app/web/a:b"):
            self.assertFalse(update.allowed(name), name)
        names = {"app/web/index.html": b"a", "app/web/INDEX.HTML": b"b"}
        archive_path = self.root / "collision.zip"
        manifest = {"format": "dlss-studio-update", "version": 1, "files": {
            name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in names.items()}}
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("update.json", json.dumps(manifest))
            for name, data in names.items():
                archive.writestr(name, data)
        with zipfile.ZipFile(archive_path) as archive, self.assertRaises(ValueError):
            update.verify(archive)

    @unittest.skipUnless(os.name == "nt", "Windows junction protection")
    def test_internal_junction_cannot_redirect_web_update_into_user_uploads(self):
        import _winapi
        web = self.target / "app/web"
        (web / "index.html").unlink(); web.rmdir()
        uploads = self.target / "app/uploads"
        sentinel = uploads / "index.html"; sentinel.write_bytes(b"private data")
        _winapi.CreateJunction(str(uploads), str(web))
        try:
            with self.assertRaises(ValueError):
                update.apply_update(self.target, self.archive)
            self.assertEqual(sentinel.read_bytes(), b"private data")
            self.assert_protected()
        finally:
            web.rmdir()


if __name__ == "__main__":
    unittest.main()
