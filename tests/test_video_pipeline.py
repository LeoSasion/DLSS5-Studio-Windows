"""Real bundled-FFmpeg coverage of precision, PTS, media and checkpoint reuse."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import nr_video
import render_engine
import video_pipeline
from video_pipeline import PAUSED, run_pipeline, source_timeline, resolve_seam, digest, timeline_payload, read_timeline_cache, atomic_json
from video_quality import analyze, global_shift
from process_runner import ProcessGroup


def hdr_metadata_fixture(data):
    """Add known HDR mastering/CLL EBML to a tiny test file, without an encoder SDK.

    Rebuild enclosing sizes and omit offset indexes/CRCs so the synthetic fixture
    remains independently readable. This does not participate in product export.
    """
    def elements(blob):
        pos=0
        while pos<len(blob):
            begin=pos
            count=next(n for n in range(1,5) if blob[pos]&(1<<(8-n)))
            key=int.from_bytes(blob[pos:pos+count],"big");pos+=count
            count=next(n for n in range(1,9) if blob[pos]&(1<<(8-n)))
            size=int.from_bytes(blob[pos:pos+count],"big")&((1<<(7*count))-1);pos+=count
            end=min(len(blob),pos+size)
            yield key,blob[pos:end],blob[begin:end]
            pos=end
    def element(key,payload):
        encoded=key.to_bytes((key.bit_length()+7)//8,"big")
        count=next(n for n in range(1,9) if len(payload)<(1<<(7*n))-1)
        return encoded+((1<<(7*count))|len(payload)).to_bytes(count,"big")+payload
    mastering=b"".join(element(key,struct.pack(">d",value)) for key,value in
        [(0x55D1,.708),(0x55D2,.292),(0x55D3,.170),(0x55D4,.797),(0x55D5,.131),
         (0x55D6,.046),(0x55D7,.3127),(0x55D8,.3290),(0x55D9,1000),(0x55DA,.005)])
    def rebuild(blob):
        result=b""
        for key,payload,raw in elements(blob):
            if key in {0xBF,0x114D9B74,0x1C53BB6B}:continue
            if key in {0x18538067,0x1654AE6B,0xAE,0xE0}:result+=element(key,rebuild(payload))
            elif key==0x55B0:
                result+=element(key,payload+element(0x55D0,mastering)+element(0x55BC,(1000).to_bytes(2,"big"))+element(0x55BD,(400).to_bytes(2,"big")))
            else:result+=raw
        return result
    return rebuild(data)


class VideoPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dlss-video-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ffmpeg, self.ffprobe = nr_video.find_tool("ffmpeg"), nr_video.find_tool("ffprobe")
        self.engine = self.root / "identity.py"
        self.calls = self.root / "calls.txt"
        self.engine.write_text("import sys,json\nfrom pathlib import Path\n"
            f"p=Path({str(self.calls)!r});p.write_text(p.read_text()+'1' if p.exists() else '1')\n"
            "total=0\nwhile True:\n b=sys.stdin.buffer.read(262144)\n if not b:break\n total+=len(b)\n sys.stdout.buffer.write(b)\n"
            "w,h=map(int,sys.argv[sys.argv.index('--nr-in')+1].split('x'))\n"
            "depth=8 if sys.argv[sys.argv.index('--nr-pixel-format')+1]=='rgba64le' else 4\n"
            "print('NRMETRIC '+json.dumps({'frames':total//(w*h*depth),'cached_nr_frames':0,'stage_ms':{'nr':1.0}}),file=sys.stderr)\n",
            encoding="utf-8")

    def command(self, arguments):
        result = subprocess.run([self.ffmpeg, "-y", "-v", "error", *map(str, arguments)],
                                capture_output=True, timeout=60, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    def info(self, path):
        return json.loads(subprocess.check_output([self.ffprobe,"-v","error","-show_streams","-show_format","-of","json",str(path)],
                          creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)))

    def args(self, source, output, *extra):
        return nr_video.build_parser().parse_args(["--in",str(source),"--out",str(output),"--exe",str(self.engine),
            "--codec","ffv1","--work-dir",str(self.root/"work"),"--segment-seconds","1","--warmup-frames","3","--resume",*extra])

    def run_video(self, args):
        with redirect_stdout(io.StringIO()):
            return run_pipeline(args)

    def test_vfr_pts_and_clip_preroll_survive_segment_boundaries(self):
        source, output = self.root/"vfr.mkv", self.root/"result.mkv"
        self.command(["-f","lavfi","-i","testsrc2=s=128x72:r=20:d=3","-vf","select='not(eq(mod(n,3),1))'",
                      "-fps_mode","passthrough","-c:v","ffv1",source])
        before = source_timeline(self.ffprobe,source,nr_video.probe(self.ffprobe,str(source)))
        args = self.args(source,output,"--start","0.2","--duration","2.5")
        self.assertEqual(self.run_video(args),0)
        after = source_timeline(self.ffprobe,output,nr_video.probe(self.ffprobe,str(output)))
        expected = [round(t-.25,6) for t in before["pts"] if .2-.000001 <= t < 2.7-.000001]
        self.assertEqual(len(expected),len(after["pts"]))
        self.assertTrue(np.allclose(expected,after["pts"],atol=.001), (expected,after["pts"]))
        frame_map=json.loads((self.root/"work/frame-map.json").read_text())
        self.assertEqual(frame_map["actual_start"],.25)
        checkpoint=json.loads((self.root/"work/checkpoint.json").read_text())
        self.assertGreater(len(checkpoint["segments"]),1)
        self.assertEqual(sum(x["frames"] for x in checkpoint["segments"]),len(expected))
        before_raw=subprocess.check_output([self.ffmpeg,"-v","error","-ss","0.25","-i",str(source),
            "-frames:v",str(len(expected)),"-vf",f"setparams=colorspace=smpte170m:color_primaries=smpte170m:color_trc=smpte170m,scale=flags={nr_video.SWS},format=rgba",
            "-fps_mode","passthrough","-f","rawvideo","-"],creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        after_raw=subprocess.check_output([self.ffmpeg,"-v","error","-i",str(output),"-pix_fmt","rgba","-fps_mode","passthrough","-f","rawvideo","-"],
             creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(before_raw,after_raw,"pre-roll must not duplicate, skip, or change a visible frame")

    def test_pause_resume_and_encoding_change_reuse_completed_nr(self):
        source,output=self.root/"source.mkv",self.root/"result.mkv"
        self.command(["-f","lavfi","-i","testsrc2=s=96x64:r=8:d=2.5","-c:v","ffv1",source])
        args=self.args(source,output)
        pause=self.root/"pause"
        pause.touch();args.pause_file=str(pause)
        self.assertEqual(self.run_video(args),PAUSED)
        self.assertFalse(output.exists());self.assertFalse(self.calls.exists())
        pause.unlink()
        self.assertEqual(self.run_video(args),0)
        calls=self.calls.read_text()
        self.assertEqual(len(calls),3)
        initial_metrics=json.loads((self.root/"work/checkpoint.json").read_text())["metrics"]
        self.assertGreater(initial_metrics["nr_evaluated_frames"],0)
        args.out=str(self.root/"result.mp4");args.codec="h264_cpu";args.bitrate=1500
        self.assertEqual(self.run_video(args),0)
        self.assertEqual(self.calls.read_text(),calls)
        checkpoint=json.loads((self.root/"work/checkpoint.json").read_text())
        self.assertEqual(checkpoint["metrics"]["reused_frames"],20)
        self.assertEqual(checkpoint["metrics"]["nr_evaluated_frames"],0)
        self.assertEqual(checkpoint["metrics"]["engine_stage_ms"],{})
        self.assertEqual(self.info(args.out)["streams"][0]["codec_name"],"h264")

    def test_pause_at_committed_segment_and_corrupt_segment_is_recomputed(self):
        source,output=self.root/"source.mkv",self.root/"result.mkv"
        self.command(["-f","lavfi","-i","testsrc2=s=96x64:r=8:d=2.5","-c:v","ffv1",source])
        pause=self.root/"pause"
        content=self.engine.read_text(encoding="utf-8")
        content=content.replace("while True:",f"if len(p.read_text())==1:Path({str(pause)!r}).touch()\nwhile True:")
        self.engine.write_text(content,encoding="utf-8")
        args=self.args(source,output,"--pause-file",str(pause))
        self.assertEqual(self.run_video(args),PAUSED)
        checkpoint=json.loads((self.root/"work/checkpoint.json").read_text())
        self.assertEqual(len(checkpoint["segments"]),1)
        first_hash=checkpoint["segments"][0]["sha256"]
        self.assertFalse(output.exists())
        pause.unlink()
        self.assertEqual(self.run_video(args),0)
        checkpoint=json.loads((self.root/"work/checkpoint.json").read_text())
        self.assertEqual(checkpoint["segments"][0]["sha256"],first_hash)
        self.assertEqual(self.calls.read_text(),"111")
        (self.root/"work"/checkpoint["segments"][1]["path"]).write_bytes(b"corrupt")
        self.assertEqual(self.run_video(args),0)
        self.assertEqual(self.calls.read_text(),"1111")

    def test_cfr_is_explicit_and_media_preservation_can_be_disabled(self):
        source,output=self.root/"vfr.mkv",self.root/"result.mp4"
        self.command(["-f","lavfi","-i","testsrc2=s=96x64:r=20:d=2","-vf","select='not(eq(mod(n,3),1))'",
                      "-fps_mode","passthrough","-c:v","ffv1",source])
        args=self.args(source,output,"--codec","h264_cpu","--timing","cfr","--preserve-media","0")
        self.assertEqual(self.run_video(args),0)
        after=source_timeline(self.ffprobe,output,nr_video.probe(self.ffprobe,str(output)))
        deltas=np.diff(after["pts"])
        self.assertLess(float(np.max(deltas)-np.min(deltas)),.0011)
        self.assertEqual(json.loads((self.root/"work/frame-map.json").read_text())["timing"],"cfr")

    def test_hdr_rgb16_intermediate_and_master_keep_precision(self):
        source,output=self.root/"hdr.mkv",self.root/"result.mkv"
        self.command(["-f","lavfi","-i","nullsrc=s=512x16:r=4:d=1,format=gbrp16le,geq=r='X/W*65535':g='X/W*65535':b='X/W*65535'",
                      "-vf","setparams=colorspace=gbr:color_primaries=bt2020:color_trc=smpte2084:range=pc",
                      "-color_primaries","bt2020","-color_trc","smpte2084","-colorspace","rgb",
                      "-c:v","ffv1","-pix_fmt","gbrp16le",source])
        source.write_bytes(hdr_metadata_fixture(source.read_bytes()))
        args=self.args(source,output)
        self.assertEqual(self.run_video(args),0)
        checkpoint=json.loads((self.root/"work/checkpoint.json").read_text())
        self.assertEqual((checkpoint["pixel_format"],checkpoint["transfer"]),("rgba64le","pq"))
        info=self.info(output)["streams"][0]
        self.assertEqual(info["color_transfer"],"smpte2084")
        self.assertEqual(nr_video.probe(self.ffprobe,str(source))["side_data"],nr_video.probe(self.ffprobe,str(output))["side_data"])
        data=subprocess.check_output([self.ffmpeg,"-v","error","-i",str(output),"-frames:v","1","-f","rawvideo","-pix_fmt","rgba64le","-"],
             creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        red=np.frombuffer(data,dtype="<u2").reshape(-1,4)[:,0]
        self.assertGreater(len(np.unique(red)),256)
        self.assertGreater(np.count_nonzero(red%257),256)
        preview=render_engine.make_preview(str(output),nvenc=False)
        self.assertTrue(preview)
        preview_info=self.info(preview)["streams"][0]
        self.assertEqual((preview_info["color_transfer"],preview_info["color_primaries"]),("bt709","bt709"))

    def test_audio_can_begin_before_first_video_pts(self):
        source,output=self.root/"offset.mkv",self.root/"result.mkv"
        self.command(["-itsoffset","0.125","-f","lavfi","-i","testsrc2=s=96x64:r=8:d=1",
                      "-f","lavfi","-i","sine=duration=1.125","-map","0:v","-map","1:a","-c:v","ffv1","-c:a","pcm_s16le",source])
        info=nr_video.probe(self.ffprobe,str(source))
        self.assertEqual(info["duration"],1.125)
        self.assertEqual(self.run_video(self.args(source,output)),0)
        checkpoint=json.loads((self.root/"work/checkpoint.json").read_text())
        self.assertEqual(checkpoint["actual_start"],.125)
        self.assertEqual(checkpoint["total_frames"],8)
        self.assertEqual(checkpoint["duration"],1)

    def test_p3_hdr_keeps_original_encoded_anchor_and_h264_is_sdr(self):
        source,output=self.root/"p3.mkv",self.root/"result.mkv"
        self.command(["-f","lavfi","-i","nullsrc=s=256x64:r=4:d=1,format=gbrp16le,geq=r='X/W*65535':g='X/W*32767':b='X/W*16383'",
                      "-vf","setparams=colorspace=gbr:color_primaries=smpte432:color_trc=smpte2084:range=pc",
                      "-c:v","ffv1","-pix_fmt","gbrp16le",source])
        args=self.args(source,output)
        self.assertEqual(self.run_video(args),0)
        checkpoint=json.loads((self.root/"work/checkpoint.json").read_text())
        self.assertEqual(checkpoint["working_primaries"],"smpte432")
        def raw(path):
            return subprocess.check_output([self.ffmpeg,"-v","error","-i",str(path),"-f","rawvideo","-pix_fmt","rgba64le","-"],
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(raw(source),raw(output))
        args.out=str(self.root/"sdr.mp4");args.codec="h264_cpu"
        self.assertEqual(self.run_video(args),0)
        self.assertEqual(nr_video.probe(self.ffprobe,args.out)["trc"],"bt709")
        self.assertEqual(json.loads((self.root/"work/checkpoint.json").read_text())["output"]["hdr_mode"],"sdr_tonemapped")

    def test_multiple_audio_subtitles_and_languages_preserved_compatibly(self):
        source,output=self.root/"media.mkv",self.root/"result.mp4"
        subtitle=self.root/"captions.srt"
        subtitle.write_text("1\n00:00:00,200 --> 00:00:01,700\nCaption\n",encoding="utf-8")
        self.command(["-f","lavfi","-i","testsrc2=s=128x72:r=8:d=2","-f","lavfi","-i","sine=frequency=440:duration=2",
                      "-f","lavfi","-i","sine=frequency=880:duration=2","-i",subtitle,"-map","0:v","-map","1:a","-map","2:a","-map","3:s",
                      "-c:v","ffv1","-c:a","pcm_s16le","-c:s","srt","-metadata:s:a:0","language=eng","-metadata:s:a:1","language=jpn",source])
        args=self.args(source,output,"--codec","h264_cpu","--bitrate","1500")
        self.assertEqual(self.run_video(args),0)
        streams=self.info(output)["streams"]
        audio=[s for s in streams if s["codec_type"]=="audio"]
        self.assertEqual(len(audio),2)
        self.assertEqual([s["tags"]["language"] for s in audio],["eng","jpn"])
        subtitles=[s for s in streams if s["codec_type"]=="subtitle"]
        self.assertEqual([s["codec_name"] for s in subtitles],["mov_text"])

    def test_clipped_chapters_drop_empty_entries_and_shift_to_result_time(self):
        source,output=self.root/"chapters.mkv",self.root/"result.mkv"
        metadata=self.root/"chapters.ffmetadata"
        metadata.write_text(";FFMETADATA1\ntitle=Example\n"+"".join(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={i*1000}\nEND={(i+1)*1000}\ntitle=Chapter {i+1}\n" for i in range(3)),encoding="utf-8")
        self.command(["-f","lavfi","-i","testsrc2=s=96x64:r=8:d=3","-i",metadata,"-map_metadata","1","-map_chapters","1","-c:v","ffv1",source])
        self.assertEqual(self.run_video(self.args(source,output,"--start","1","--duration","1.5")),0)
        chapters=json.loads(subprocess.check_output([self.ffprobe,"-v","error","-show_chapters","-of","json",str(output)],
             creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)))["chapters"]
        self.assertEqual([c["tags"]["title"] for c in chapters],["Chapter 2","Chapter 3"])
        self.assertEqual([(float(c["start_time"]),float(c["end_time"])) for c in chapters],[(0,1),(1,1.5)])

    def test_quality_diagnostics_report_exact_identity_and_translation(self):
        source=self.root/"source.mkv"
        self.command(["-f","lavfi","-i","testsrc2=s=96x64:r=8:d=1","-c:v","ffv1",source])
        report=analyze(source,source,max_frames=8,width=96)
        self.assertTrue(report["exact_pixels"])
        self.assertAlmostEqual(report["global_luma_ssim"],1)
        self.assertAlmostEqual(report["correction_temporal_error"],0)
        self.assertEqual(report["matched_frames"],8)
        self.assertTrue(all(frame["pts_error_ms"]==0 for frame in report["frames"]))
        rng=np.random.default_rng(5);original=rng.random((64,96))
        shifted=np.roll(original,(2,-3),axis=(0,1))
        self.assertEqual(global_shift(original,shifted),(2,-3))

    def test_prediction_overlap_keeps_old_first_frame_and_new_last_frame(self):
        work=self.root.resolve()
        for name,color,count in [("old","black",8),("new","white",4)]:
            self.command(["-f","lavfi","-i",f"color={color}:s=64x64:r=8","-frames:v",str(count),
                          "-pix_fmt","bgra","-c:v","ffv1","-f","nut",work/f"{name}.nut"])
        previous={"frames":4,"stored_frames":8,"raw_path":"old.nut","raw_sha256":digest(work/"old.nut"),
                  "path":"old.nut","sha256":digest(work/"old.nut")}
        current={"index":1,"first":4,"stop":8,"source_start":.5,"frames":4,"stored_frames":4,
                 "raw_path":"new.nut","raw_sha256":digest(work/"new.nut")}
        resolved=resolve_seam(current,previous,{"pts":[i/8 for i in range(8)]},work,self.ffmpeg,"rgba",{})
        raw=subprocess.check_output([self.ffmpeg,"-v","error","-i",str(work/resolved["path"]),"-pix_fmt","rgb24","-f","rawvideo","-"],
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        pixels=np.frombuffer(raw,np.uint8).reshape(-1,64,64,3)
        self.assertEqual(pixels[:,0,0,0].tolist(),[0,85,170,255])

    def test_hdr_source_display_proxy_preserves_vfr_pts_and_reports_real_progress(self):
        source=self.root/"hdr-vfr.mkv"
        self.command(["-itsoffset","0.125","-f","lavfi","-i","nullsrc=s=256x64:r=20:d=2.5,format=gbrp16le,geq=r='X/W*65535':g='X/W*32767':b='X/W*16383'",
                      "-f","lavfi","-i","sine=duration=2.625","-map","0:v","-map","1:a",
                      "-vf","select='not(eq(mod(n,3),1))',setparams=colorspace=gbr:color_primaries=bt2020:color_trc=smpte2084:range=pc",
                      "-fps_mode","passthrough","-c:v","ffv1","-pix_fmt","gbrp16le","-c:a","pcm_s16le",source])
        before=source_timeline(self.ffprobe,source,nr_video.probe(self.ffprobe,str(source)))
        updates=[];group=ProcessGroup()
        destination=self.root/"source_preview.partial.mp4"
        try:preview=render_engine.make_preview(str(source),runner=group,nvenc=False,progress=updates.append,destination=destination)
        finally:group.close()
        self.assertTrue(preview)
        self.assertEqual(preview,str(destination))
        info=nr_video.probe(self.ffprobe,str(preview))
        after=source_timeline(self.ffprobe,preview,info)
        self.assertEqual(len(before["pts"]),len(after["pts"]))
        self.assertTrue(np.allclose(before["pts"],after["pts"],atol=.000001),(before["pts"],after["pts"]))
        self.assertEqual((info["trc"],info["primaries"]),("bt709","bt709"))
        self.assertTrue(updates)
        self.assertTrue(all(update["phase"]=="preview" and 0<=update["progress"]<=99 for update in updates))
        self.assertGreater(updates[-1]["preview_frame"],0)

    def test_timeline_cache_invalidates_source_tool_and_payload_changes(self):
        path=self.root/"source.timeline.json"
        timeline={"pts":[0,.05,.15],"end":.2,"origin":0}
        saved=timeline_payload(timeline,"source-a","probe-a")
        atomic_json(path,saved)
        self.assertEqual(read_timeline_cache(path,"source-a","probe-a"),timeline)
        self.assertIsNone(read_timeline_cache(path,"source-b","probe-a"))
        self.assertIsNone(read_timeline_cache(path,"source-a","probe-b"))
        saved["timeline"]["pts"][1]=.1
        atomic_json(path,saved)
        self.assertIsNone(read_timeline_cache(path,"source-a","probe-a"))

    @unittest.skipUnless(os.name=="nt","Windows rename sharing semantics")
    def test_atomic_json_retries_transient_windows_replace_denials(self):
        original_replace=Path.replace
        for code in (5,32):
            with self.subTest(winerror=code):
                path=self.root/f"checkpoint-{code}.json"
                original=b'{"state":"previous-complete"}'
                path.write_bytes(original)
                foreign=path.with_name(path.name+".tmp")
                foreign.write_bytes(b"another writer or retained diagnostic")
                attempts=[]
                def replace(temporary,target):
                    attempts.append(str(temporary))
                    self.assertEqual(path.read_bytes(),original)
                    if len(attempts)==1:
                        error=PermissionError(13,"temporarily denied")
                        error.winerror=code
                        raise error
                    return original_replace(temporary,target)
                with patch.object(Path,"replace",new=replace),patch.object(video_pipeline.time,"sleep") as sleeper:
                    atomic_json(path,{"state":"new-complete"})
                self.assertEqual(json.loads(path.read_text()),{"state":"new-complete"})
                self.assertEqual(len(attempts),2)
                sleeper.assert_called_once()
                self.assertEqual(foreign.read_bytes(),b"another writer or retained diagnostic")
                self.assertEqual(list(self.root.glob(path.name+".*.tmp")),[])

    @unittest.skipUnless(os.name=="nt","Windows rename sharing semantics")
    def test_atomic_json_permanent_windows_denial_preserves_previous_record(self):
        path=self.root/"checkpoint.json"
        original=b'{"state":"previous-complete"}'
        path.write_bytes(original)
        error=PermissionError(13,"permanent ACL denial")
        error.winerror=5
        attempts=[];delays=[]
        def replace(temporary,target):
            attempts.append(str(temporary))
            self.assertEqual(path.read_bytes(),original)
            raise error
        with patch.object(Path,"replace",new=replace),patch.object(video_pipeline.time,"sleep",side_effect=delays.append):
            with self.assertRaises(PermissionError) as caught:
                atomic_json(path,{"state":"must-not-be-claimed-committed"})
        self.assertIs(caught.exception,error)
        self.assertGreater(len(attempts),1)
        self.assertLessEqual(len(attempts),12)
        self.assertLessEqual(sum(delays),4)
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(list(self.root.glob("checkpoint.json.*.tmp")),[])

    def test_atomic_json_does_not_retry_other_errors_or_delete_foreign_temp(self):
        path=self.root/"checkpoint.json";path.write_bytes(b"previous")
        error=OSError(28,"not a sharing violation");error.winerror=112
        with patch.object(Path,"replace",side_effect=error) as replace,patch.object(video_pipeline.time,"sleep") as sleeper:
            with self.assertRaises(OSError) as caught:atomic_json(path,{"state":"new"})
        self.assertIs(caught.exception,error)
        replace.assert_called_once();sleeper.assert_not_called()
        self.assertEqual(path.read_bytes(),b"previous")
        self.assertEqual(list(self.root.glob("checkpoint.json.*.tmp")),[])
        class SameId:
            hex="fixed-id"
        foreign=path.with_name(path.name+".fixed-id.tmp")
        foreign.write_bytes(b"foreign-writer-data")
        with patch.object(video_pipeline.uuid,"uuid4",return_value=SameId()):
            with self.assertRaises(FileExistsError):atomic_json(path,{"state":"new"})
        self.assertEqual(foreign.read_bytes(),b"foreign-writer-data")
        self.assertEqual(path.read_bytes(),b"previous")

    @unittest.skipUnless(os.name=="nt","Real Win32 handle sharing test")
    def test_atomic_json_waits_for_real_windows_reader_handle_release(self):
        import ctypes
        from ctypes import wintypes
        path=self.root/"完成点 checkpoint.json"
        path.write_bytes(b'{"state":"previous-complete"}')
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.CloseHandle.restype=wintypes.BOOL
        # Deliberately omit FILE_SHARE_DELETE; this reproduces a backend reader
        # that allows data reads but temporarily prevents atomic replacement.
        handle=kernel.CreateFileW(str(path),0x80000000,0x1|0x2,None,3,0x80,None)
        self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
        holder=[handle];guard=threading.Lock()
        def release():
            with guard:
                if holder[0] is not None:
                    kernel.CloseHandle(holder[0]);holder[0]=None
        timer=threading.Timer(.15,release)
        real_replace=Path.replace;denials=[]
        def observed_replace(temporary,target):
            try:return real_replace(temporary,target)
            except OSError as exc:
                denials.append(getattr(exc,"winerror",None))
                self.assertEqual(json.loads(path.read_text()),{"state":"previous-complete"})
                raise
        started=time.monotonic();timer.start()
        try:
            with patch.object(Path,"replace",new=observed_replace):atomic_json(path,{"state":"new-complete"})
        finally:
            timer.cancel();release();timer.join(timeout=1)
        self.assertTrue(any(code in {5,32,33} for code in denials),denials)
        self.assertGreaterEqual(time.monotonic()-started,.1)
        self.assertLess(time.monotonic()-started,5)
        self.assertEqual(json.loads(path.read_text()),{"state":"new-complete"})
        self.assertEqual(list(self.root.glob(path.name+".*.tmp")),[])


if __name__ == "__main__":
    unittest.main()
