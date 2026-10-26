import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from gpu_runtime import GpuRuntime, classify


def card(name, index=0, vendor=0x10de, luid='a', software=False):
    return dict(name=name, index=index, vendor=vendor, luid=luid, software=software)


class GpuTests(unittest.TestCase):
    def test_series_and_laptop_names(self):
        for name, expected in [('NVIDIA GeForce RTX 3060 Ti','rtx30'),
                               ('NVIDIA GeForce RTX 4090','rtx40'),
                               ('NVIDIA GeForce RTX 5070 Laptop GPU','rtx50'),
                               ('NVIDIA RTX PRO 6000 Blackwell Workstation Edition','rtx50'),
                               ('NVIDIA RTX 6000 Ada Generation','rtx40'),
                               ('NVIDIA RTX A6000','rtx30')]:
            with self.subTest(name=name):
                self.assertEqual(classify(card(name), {}), expected)

    def test_unsupported_is_not_guessed(self):
        for name in ['NVIDIA GeForce RTX 2080', 'NVIDIA GeForce RTX 6090', 'NVIDIA RTX 6000', 'NVIDIA A100']:
            self.assertIsNone(classify(card(name), {}))
        self.assertIsNone(classify(card('RTX 4090', vendor=0x1002), {}))
        self.assertIsNone(classify(card('RTX 4090', software=True), {}))
        self.assertIsNone(classify(card('RTX 4090'), {'a': (9, 0)}))

    def test_cuda_matching_uses_luid(self):
        self.assertEqual(classify(card('unlabelled NVIDIA RTX', luid='second'), {'first':(8,6),'second':(12,0)}), 'rtx50')

    def test_explicit_adapter_and_dll_refresh(self):
        # TemporaryDirectory owns this exact test directory; cleanup stays under it.
        with tempfile.TemporaryDirectory(prefix='dlss-gpu-test-') as folder:
            root = Path(folder).resolve()
            profiles = {}
            for profile in ['rtx30','rtx40','rtx50']:
                data = ('test fixture '+profile).encode()
                dll = root / 'out/dlss5' / profile / 'nvngx_dlssnr.dll'
                dll.parent.mkdir(parents=True); dll.write_bytes(data)
                profiles[profile] = dict(path=dll.relative_to(root).as_posix(), bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            (root/'dlss5-components.json').write_text(json.dumps(dict(profiles=profiles)))
            adapters = [card('Intel',0,0x8086),card('RTX 4090',2,luid='ada'),card('RTX 5090',4,luid='blackwell')]
            runtime=GpuRuntime(root,lambda: adapters,lambda: {})
            selected=runtime.prepare()
            self.assertEqual(selected.profile,'rtx40')
            self.assertEqual(selected.args()[:2],['--adapter','2'])
            self.assertIn(str(root/'out/dlss5/rtx40'),selected.args())
            self.assertEqual(selected.args()[-2:],['--nr-sr-preset','default'])
            self.assertEqual(runtime.public()['status'],'matched')
            runtime.mark_verified(selected)
            self.assertEqual(runtime.public()['status'],'verified')
            self.assertFalse(runtime.public()['capabilities']['continuous_validation'])
            runtime.mark_continuous_verified(selected)
            self.assertTrue(runtime.public()['capabilities']['continuous_validation'])
            adapters[:]=[card('RTX 5090',1,luid='blackwell')]
            self.assertEqual(runtime.prepare().profile,'rtx50')
            self.assertEqual(runtime.public()['status'],'matched')
            (root/'out/dlss5/rtx50/nvngx_dlssnr.dll').write_bytes(b'corrupted')
            with self.assertRaises(RuntimeError): runtime.prepare()
            self.assertFalse(runtime.public()['ready'])
            self.assertIn('校验失败',runtime.public()['message'])
            (root/'out/dlss5/rtx50/nvngx_dlssnr.dll').unlink()
            with self.assertRaises(RuntimeError): runtime.prepare()
            adapters.clear()
            self.assertFalse(runtime.refresh()['ready'])


if __name__ == '__main__':
    unittest.main()
