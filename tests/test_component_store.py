import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from component_store import ComponentStore, validate_dll
import component_store


def dll_fixture(payload):
    data = bytearray(88)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 68, 0x8664)
    struct.pack_into("<H", data, 86, 0x2000)
    return bytes(data) + payload


class ComponentStoreTests(unittest.TestCase):
    def fixture(self, root, profiles=("rtx50",)):
        original = {"profiles": {}}
        for profile in profiles:
            path = root / "out/dlss5" / profile / "nvngx_dlssnr.dll"
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(dll_fixture(b"old " + profile.encode()))
            original["profiles"][profile] = {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
                                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        (root / "dlss5-components.json").write_text(json.dumps(original))
        return original

    def test_import_is_immutable_and_rollback_restores_selection(self):
        with tempfile.TemporaryDirectory(prefix="dlss-components-") as temp:
            root = Path(temp)
            old = root / "out/dlss5/rtx50/nvngx_dlssnr.dll"
            old.parent.mkdir(parents=True)
            old.write_bytes(dll_fixture(b"old model"))
            info = {"path": old.relative_to(root).as_posix(), "bytes": old.stat().st_size,
                    "sha256": hashlib.sha256(old.read_bytes()).hexdigest()}
            original = {"profiles": {"rtx50": info}}
            (root / "dlss5-components.json").write_text(json.dumps(original))
            incoming = root / "incoming.dll"
            incoming.write_bytes(dll_fixture(b"new model"))
            digest = hashlib.sha256(incoming.read_bytes()).hexdigest()
            store = ComponentStore(root)
            result = store.import_dll(incoming, "rtx50", digest, "fixture-v2", "test fixture")
            self.assertEqual(old.read_bytes(), dll_fixture(b"old model"))
            self.assertEqual(store.read()["profiles"]["rtx50"]["sha256"], digest)
            store.rollback(result["rollback_revision"])
            self.assertEqual(store.read(), original)
            with self.assertRaises(ValueError):
                store.rollback("../../outside")

    def test_bad_digest_or_non_dll_never_replaces_manifest(self):
        with tempfile.TemporaryDirectory(prefix="dlss-bad-component-") as temp:
            root = Path(temp)
            path = root / "candidate.dll"
            path.write_bytes(dll_fixture(b"fixture"))
            with self.assertRaises(ValueError):
                validate_dll(path, "0" * 64)
            path.write_bytes(b"not a library")
            with self.assertRaises(ValueError):
                validate_dll(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_manifest_commit_failure_removes_unselected_new_component(self):
        with tempfile.TemporaryDirectory(prefix="dlss-component-commit-") as temp:
            root = Path(temp); original = self.fixture(root)
            incoming = root / "incoming.dll"; incoming.write_bytes(dll_fixture(b"new"))
            digest = hashlib.sha256(incoming.read_bytes()).hexdigest()
            store = ComponentStore(root)
            atomic = component_store.atomic_json
            def fail_manifest(path, value):
                if Path(path).resolve() == store.manifest:
                    raise OSError("simulated commit failure")
                return atomic(path, value)
            with patch.object(component_store, "atomic_json", side_effect=fail_manifest), self.assertRaises(OSError):
                store.import_dll(incoming, "rtx50", digest, "new", "fixture")
            self.assertEqual(store.read(), original)
            self.assertFalse((root / "out/dlss5/rtx50" / digest / "nvngx_dlssnr.dll").exists())
            self.assertEqual(list(store.history.glob("*.json")), [])

    def test_existing_digest_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="dlss-component-immutable-") as temp:
            root = Path(temp); self.fixture(root)
            incoming = root / "incoming.dll"; incoming.write_bytes(dll_fixture(b"new"))
            digest = hashlib.sha256(incoming.read_bytes()).hexdigest(); store = ComponentStore(root)
            store.import_dll(incoming, "rtx50", digest, "new", "fixture")
            selected = root / store.read()["profiles"]["rtx50"]["path"]
            before = selected.stat().st_mtime_ns
            with patch.object(component_store.shutil, "copyfile", side_effect=AssertionError("immutable file was replaced")):
                store.import_dll(incoming, "rtx50", digest, "new again", "fixture")
            self.assertEqual(selected.stat().st_mtime_ns, before)

    def test_concurrent_imports_do_not_lose_another_profiles_update(self):
        with tempfile.TemporaryDirectory(prefix="dlss-component-concurrency-") as temp:
            root = Path(temp); self.fixture(root, ("rtx30", "rtx50"))
            payloads = {}
            for profile in ("rtx30", "rtx50"):
                path = root / (profile + ".dll"); path.write_bytes(dll_fixture(b"new " + profile.encode()))
                payloads[profile] = (path, hashlib.sha256(path.read_bytes()).hexdigest())
            def install(profile):
                path, digest = payloads[profile]
                return ComponentStore(root).import_dll(path, profile, digest, "new", "fixture")
            with ThreadPoolExecutor(max_workers=2) as worker:
                list(worker.map(install, ("rtx30", "rtx50")))
            value = ComponentStore(root).read()
            for profile, (_, digest) in payloads.items():
                self.assertEqual(value["profiles"][profile]["sha256"], digest)

    @unittest.skipUnless(os.name == "nt", "Windows junction protection")
    def test_store_junction_cannot_write_into_protected_user_directory(self):
        import _winapi
        with tempfile.TemporaryDirectory(prefix="dlss-component-junction-") as temp:
            root = Path(temp); protected = root / "private"; protected.mkdir()
            sentinel = protected / "media.bin"; sentinel.write_bytes(b"user media")
            link = root / "out/dlss5"; link.parent.mkdir()
            _winapi.CreateJunction(str(protected), str(link))
            incoming = root / "incoming.dll"; incoming.write_bytes(dll_fixture(b"new"))
            try:
                with self.assertRaises(ValueError):
                    ComponentStore(root).import_dll(incoming, "rtx50", hashlib.sha256(incoming.read_bytes()).hexdigest(), "new", "fixture")
                self.assertEqual(list(protected.iterdir()), [sentinel])
                self.assertEqual(sentinel.read_bytes(), b"user media")
            finally:
                link.rmdir()


if __name__ == "__main__":
    unittest.main()
