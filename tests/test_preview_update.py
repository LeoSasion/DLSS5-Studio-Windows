import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging'))
import apply_nvidia_preview as update


class PreviewUpdateTests(unittest.TestCase):
    def test_rerun_after_locked_file_keeps_both_previous_versions_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installer = root / 'apply-preview.py'
            media = root / 'ffmpeg.zip'
            with zipfile.ZipFile(media, 'w') as archive:
                archive.writestr('ffmpeg.exe', b'new ffmpeg')
            patch_dir = root / 'preview-update'
            payload = patch_dir / 'files'
            payload.mkdir(parents=True)
            wanted = {'server.py': b'new server', 'Studio.exe': b'new launcher', 'ffmpeg.exe': b'new ffmpeg'}
            for name in wanted:
                (root / name).write_bytes(b'old ' + name.encode())
            for name in ('server.py', 'Studio.exe'):
                (payload / name).write_bytes(wanted[name])
            spec = {'base': {name: update.digest(root / name) for name in ('server.py', 'Studio.exe')},
                    'files': {name: hashlib.sha256(content).hexdigest() for name, content in wanted.items()},
                    'ffmpeg': {'sha256': update.digest(media), 'paths': {'ffmpeg.exe': 'ffmpeg.exe'}}}
            (patch_dir / 'patch.json').write_text(json.dumps(spec), encoding='utf-8')
            replace = Path.replace

            def locked(path, target):
                if target.name == 'Studio.exe':
                    raise PermissionError(target)
                return replace(path, target)

            with patch.object(update, '__file__', str(installer)), patch.object(sys, 'argv', [str(installer), '--ffmpeg-archive', str(media)]):
                with patch.object(Path, 'replace', locked), self.assertRaisesRegex(SystemExit, 'Close DLSS Studio and rerun'):
                    update.main()
                self.assertEqual((root / 'server.py').read_bytes(), wanted['server.py'])
                self.assertEqual((root / 'Studio.exe').read_bytes(), b'old Studio.exe')
                update.main()
                update.main()
            self.assertEqual({name: (root / name).read_bytes() for name in wanted}, wanted)
