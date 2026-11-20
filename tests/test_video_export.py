"""OS defaults, rate control and real bundled CPU encoder coverage."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
import render_engine as engine
import nr_video
import video_pipeline
from studio_server import Settings, build_command, check_nvenc


class VideoExportTests(unittest.TestCase):
    def test_missing_ffmpeg_disables_nvenc_probe(self):
        with patch.object(engine, 'find_tool', return_value=None), patch('studio_server.subprocess.run') as run:
            self.assertFalse(check_nvenc('av1_nvenc'))
            run.assert_not_called()

    def test_os_defaults_and_independent_codec_support(self):
        for build,gpu,cpu in [(19045,'h264_nvenc','h264_cpu'),(22000,'av1_nvenc','av1_svt'),(26200,'av1_nvenc','av1_svt')]:
            self.assertEqual(engine.default_video_codec(build,{}),cpu)
            self.assertEqual(engine.default_video_codec(build,{gpu:True}),gpu)
        self.assertEqual(engine.default_video_codec(26200,{'h264_nvenc':True}), 'av1_svt')

    def test_bitrate_by_output_preset_and_original_orientation(self):
        for size,value in [('720p (1280×720)',6000),('1080p (1920×1080)',6000),('1440p (2560×1440)',9000),('4K (3840×2160)',12000)]:
            self.assertEqual(engine.recommended_bitrate(size,640,360),value)
        for width,height,value in [(1080,1920,6000),(2560,1440,9000),(2160,3840,12000)]:
            self.assertEqual(engine.recommended_bitrate('原始尺寸（不放大）',width,height),value)

    def test_auto_and_manual_command_and_legacy_settings(self):
        source={'kind':'video','path':'fixture.mp4','width':960,'height':552}
        selection=SimpleNamespace(args=lambda:[])
        for fields,expected in [({'size':'4K (3840×2160)'},12000),({'bitrate':4500},4500),({'bitrate':0},0),({'bitrate':6000,'bitrate_mode':'auto','size':'1440p (2560×1440)'},9000)]:
            settings=Settings(asset_id='fixture',**fields)
            command=build_command(settings,source,Path('output'),selection)
            self.assertEqual(int(command[command.index('--bitrate')+1]),expected)
        self.assertEqual(Settings(asset_id='fixture',bitrate=0).bitrate_mode,'manual')
        self.assertEqual(Settings(asset_id='fixture').bitrate_mode,'auto')

    def test_nvenc_failure_reencodes_the_same_segments(self):
        for hw,container,sw in [('h264_nvenc','mp4','h264_cpu'),('hevc_nvenc','mov','prores'),
                                ('hevc_nvenc','mp4','av1_svt'),('av1_nvenc','webm','av1_svt')]:
            args=SimpleNamespace(codec=hw,out=f'result.{container}')
            segments={'segments':['already-rendered']}
            error = f"Unknown encoder '{hw}'" if container=='webm' else f'[enc:{hw} @ fff] Cannot open encoder'
            with self.subTest(codec=hw,container=container), patch.object(video_pipeline,'export_video_attempt',
                    side_effect=[video_pipeline.EncodeFailed(error),None]) as encode, \
                    patch.object(video_pipeline,'emit'), patch('sys.stderr'):
                _, actual=video_pipeline.export_video(args,{},segments,Path('.'),'ffmpeg',(1920,1080))
                self.assertEqual(actual,sw)
                self.assertEqual([call.args[-1] for call in encode.call_args_list],[hw,sw])
                self.assertTrue(all(call.args[2] is segments for call in encode.call_args_list))
                self.assertEqual(args.codec,hw)

    def test_nvenc_export_does_not_retry_a_mux_failure(self):
        args=SimpleNamespace(codec='av1_nvenc',out='result.mp4')
        with patch.object(video_pipeline,'export_video_attempt',side_effect=video_pipeline.EncodeFailed(
                '[vost#0:0/av1_nvenc @ fff] Error submitting a packet to the muxer: No space left on device\n'
                '[out#0/mp4 @ fff] Error writing trailer: No space left on device')) as encode:
            with self.assertRaisesRegex(video_pipeline.EncodeFailed,'No space left'):
                video_pipeline.export_video(args,{},{},Path('.'),'ffmpeg',(1920,1080))
            self.assertEqual(encode.call_count,1)

    def test_bundled_cpu_encoders_produce_playable_mp4(self):
        with tempfile.TemporaryDirectory(prefix='dlss-export-test-') as folder:
            for codec,expected in [('h264_cpu','h264'),('av1_svt','av1')]:
                args=SimpleNamespace(bit_depth=10,bitrate=6000,cq=19,sw_preset=10)
                flags,pixel_format=nr_video.video_args(codec,args,320,180,('bt709','bt709','bt709'))
                self.assertEqual(flags[flags.index('-b:v')+1],'6000k')
                target=Path(folder)/(codec+'.mp4')
                command=[engine.find_tool('ffmpeg'),'-v','error','-f','lavfi','-i','testsrc2=s=320x180:r=8:d=1','-pix_fmt',pixel_format,*flags,'-an','-movflags','+faststart',str(target)]
                result=subprocess.run(command,capture_output=True,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
                info=json.loads(subprocess.check_output([engine.find_tool('ffprobe'),'-v','error','-show_streams','-of','json',str(target)],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)))['streams'][0]
                self.assertEqual((info['codec_name'],info['width'],info['height']),(expected,320,180))
                self.assertGreater(target.stat().st_size,1000)

if __name__=='__main__':unittest.main()
