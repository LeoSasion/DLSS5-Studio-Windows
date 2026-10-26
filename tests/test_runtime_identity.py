import sys
import tempfile
import unittest
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from runtime_identity import RuntimeIdentity


class RuntimeIdentityTests(unittest.TestCase):
    def test_transient_model_mapping_is_not_a_new_weight_identity(self):
        with tempfile.TemporaryDirectory(prefix="dlss-mapping-") as temp:
            root = Path(temp)
            models = root / "models"
            models.mkdir()
            mapping = models / "nvngx_mapping.json"
            mapping.write_text('{"model": "fixture-v1"}', encoding="utf-8")
            config = models / "nvngx_server_config.txt"
            config.write_text('model=fixture-v1', encoding="utf-8")
            identity = RuntimeIdentity(root, [], [models])
            stable = identity.models()
            mapping.write_bytes(b"")
            config.write_bytes(b"")
            restore = threading.Thread(target=lambda: (time.sleep(.05), mapping.write_text('{"model": "fixture-v1"}', encoding="utf-8"),config.write_text('model=fixture-v1', encoding="utf-8")))
            restore.start()
            try:
                self.assertEqual(identity.models(), stable)
            finally:
                restore.join()
            mapping.write_bytes(b"")
            self.assertFalse(identity.models()["stable"])

    def test_pixel_environment_changes_retire_cache_but_ui_edits_do_not(self):
        with tempfile.TemporaryDirectory(prefix="dlss-identity-") as temp:
            root = Path(temp)
            (root / "out/dlss5/rtx50").mkdir(parents=True)
            (root / "out/dlss5/rtx50/nvngx_dlssnr.dll").write_bytes(b"nr fixture")
            (root / "out/video2dlssnr.exe").write_bytes(b"renderer fixture")
            driver = root / "nvcuda.dll"
            driver.write_bytes(b"driver one")
            models = root / "models"
            models.mkdir()
            model = models / "weights.bin"
            model.write_bytes(b"model one")
            (root / "nr_video.py").write_text("version one", encoding="utf-8")
            identity = RuntimeIdentity(root, [driver], [models])
            selected = SimpleNamespace(dll_dir=root / "out/dlss5/rtx50", luid="fixture", profile="rtx50", name="RTX fixture")
            before = identity.build(selected)
            (root / "web").mkdir()
            (root / "web/index.html").write_text("new appearance", encoding="utf-8")
            self.assertEqual(before["digest"], identity.build(selected)["digest"])
            driver.write_bytes(b"driver version two")
            after_driver = identity.build(selected)
            self.assertNotEqual(before["digest"], after_driver["digest"])
            model.write_bytes(b"new model weights")
            after_model = identity.build(selected)
            self.assertNotEqual(after_driver["digest"], after_model["digest"])
            (root / "nr_video.py").write_text("changed pixel pipeline", encoding="utf-8")
            self.assertNotEqual(after_model["digest"], identity.build(selected)["digest"])

    def test_install_location_is_not_a_pixel_identity(self):
        with tempfile.TemporaryDirectory(prefix="dlss-portable-identity-") as temp:
            identities = []
            for name in ("original", "moved"):
                root = Path(temp) / name
                (root / "out/dlss5/rtx50").mkdir(parents=True)
                (root / "out/dlss5/rtx50/nvngx_dlssnr.dll").write_bytes(b"same fixture")
                selected = SimpleNamespace(dll_dir=root / "out/dlss5/rtx50", luid="same", profile="rtx50", name="same")
                identities.append(RuntimeIdentity(root, [], []).build(selected))
            self.assertEqual(identities[0], identities[1])


if __name__ == "__main__":
    unittest.main()
