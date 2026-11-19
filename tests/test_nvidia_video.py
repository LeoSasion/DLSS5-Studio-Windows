import io
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import nvidia_video as nv
from studio_cache import canonical_settings
import video_pipeline as vp


class NvidiaVideoTests(unittest.TestCase):
    def test_missing_artifacts_disable_only_the_affected_features(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fruc = root / 'studio-fruc.exe'
            libs = root / 'nvvfx/libs'
            libs.mkdir(parents=True)
            (root / 'nvvfx/__init__.py').touch()
            (root / 'cupy').mkdir()
            (root / 'cupy/__init__.py').touch()
            for name in nv.VFX_BINARIES:
                (libs / name).touch()
            with patch.object(nv, 'RUNTIME', root), patch.object(nv, 'FRUC', fruc):
                self.assertEqual(nv.capabilities('rtx30'), {'upscale':True, 'interpolation':None})
                self.assertEqual(nv.capabilities('rtx40'), {'upscale':True, 'interpolation':'vfg'})
                self.assertEqual(nv.capabilities('rtx50'), {'upscale':True, 'interpolation':'vfg'})
                fruc.touch()
                fruc.with_name('NvOFFRUC.dll').touch()
                self.assertEqual(nv.capabilities('rtx30'), {'upscale':True, 'interpolation':'fruc'})
                (libs / 'nvVFXVideoFrameGeneration.dll').unlink()
                self.assertEqual(nv.capabilities('rtx50'), {'upscale':True, 'interpolation':None})

    def test_fractional_pts_with_offset_and_vfr_gap(self):
        rate = {'rate':'30000/1001'}
        points = [round(15 + i * 1001 / 30000, 6) for i in range(300)]
        self.assertEqual(vp.double_rate(rate, points), '60000/1001')
        points[144] += .012
        self.assertIsNone(vp.double_rate(rate, points))

    def test_comparison_uses_actual_delivery_times_for_jittery_cfr(self):
        original = [0., .0421, .0838]
        src = {'rate':'24/1', 'fps':24}
        self.assertEqual(vp.double_rate(src, original), '48')
        self.assertEqual(vp.delivered_source_times(original, src, 'preserve', '48'), [0., .041667, .083333])
        self.assertEqual(vp.delivered_source_times(original, src, 'preserve'), original)

    def test_pause_preserves_original_segments_and_discards_incomplete_interpolation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root / 'pause'
            segment = root / 'segment.nut'
            segment.write_bytes(b'original segment')
            checkpoint = {'dimensions':[720, 480], 'pixel_format':'rgba', 'total_frames':2,
                          'segments':[{'path':segment.name, 'sha256':vp.digest(segment), 'start':0, 'end':.1}]}
            src = {'fps':24,'w':720,'h':480,'matrix':'bt709','primaries':'bt709','trc':'bt709'}
            def request_pause(*args, **kwargs):
                (root / 'doubled.partial.nut').write_bytes(b'incomplete')
                marker.touch()
                vp.check_interpolation(root, marker)
            with patch.object(nv, 'effect_files', return_value=[segment]), patch.object(vp, 'monitor_pipeline', side_effect=request_pause), \
                    patch.object(vp, 'emit'), patch.object(vp.shutil, 'disk_usage', return_value=SimpleNamespace(free=3*1024**3)):
                with self.assertRaises(vp.InterpolationPaused):
                    vp.interpolate_master(SimpleNamespace(pause_file=marker, _gpu_luid=''), src, checkpoint, root, 'ffmpeg', 'ffprobe', 'vfg', '48')
            self.assertFalse((root / 'doubled.partial.nut').exists())
            self.assertEqual(segment.read_bytes(), b'original segment')

    def test_low_disk_reserve_rejects_interpolation(self):
        with patch.object(vp.shutil, 'disk_usage', return_value=SimpleNamespace(free=1024**3-1)):
            with self.assertRaises(OSError):
                vp.check_interpolation(Path('.'), None)

    def test_toggle_invalidates_delivery_but_preserves_neural_cache(self):
        base = {'frame_interpolation':False, 'codec':'av1_nvenc'}
        changed = {**base, 'frame_interpolation':True}
        item = {'kind':'video'}
        self.assertNotEqual(canonical_settings(base, item), canonical_settings(changed, item))
        self.assertEqual(canonical_settings(base, item, neural=True), canonical_settings(changed, item, neural=True))

    def test_truncated_raw_frame_is_not_silently_accepted(self):
        with self.assertRaises(RuntimeError):
            list(nv.frames(io.BytesIO(b'123456789'), 4))
