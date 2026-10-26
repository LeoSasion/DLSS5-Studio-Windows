"""Timestamp-preserving, bounded video checkpoints shared by Studio and the CLI.

Every completed segment is an independently validated lossless RGB file. The
encoded delivery file is a separate stage, so changing bitrate does not run NR.
Source PTS is restored before segment muxing instead of invented from a nominal
frame rate. Checkpoints are committed only after frame-count/hash validation.
"""
from __future__ import annotations

import bisect
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import nr_video as nv

PIPELINE_VERSION = "studio-video-pts16-v4b-overlap"
PAUSED = 75
CREATE_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_REPLACE_RETRY_DELAYS = (.02, .04, .08, .16, .32, .5, .5, .5, .5, .5)


def digest(path, progress=None):
    value = hashlib.sha256()
    total=Path(path).stat().st_size
    processed,last=0,time.monotonic()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            value.update(block)
            processed+=len(block)
            if progress and time.monotonic()-last>=1:
                progress(processed,total)
                last=time.monotonic()
    return value.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    # A backend reader or antivirus can briefly deny rename/delete sharing on
    # Windows. Keep the committed record intact and retry only the atomic rename,
    # with a bounded 3.12-second backoff. Each writer owns a distinct temp file.
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    owned, committed, failure = False, False, None
    try:
        with temporary.open("x", encoding="utf-8") as target:
            owned = True
            json.dump(value, target, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            target.flush()
            os.fsync(target.fileno())
        for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
            try:
                temporary.replace(path)
                committed = True
                return
            except OSError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == len(_REPLACE_RETRY_DELAYS):
                    raise
                time.sleep(_REPLACE_RETRY_DELAYS[attempt])
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if owned and not committed:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                # Preserve the actual commit error. Never remove an old record
                # or another writer's file to manufacture a successful commit.
                if failure is not None and hasattr(failure, "add_note"):
                    failure.add_note(f"本次临时记录未能清理，原完成点未删除：{temporary} ({cleanup_error})")
                elif failure is None:
                    raise


def emit(kind, value):
    print(kind + " " + json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


def checked(command, **kwargs):
    result = subprocess.run(command, creationflags=CREATE_FLAGS, capture_output=True, **kwargs)
    if result.returncode:
        error = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else result.stderr
        raise RuntimeError((error or "视频工具执行失败。")[-5000:])
    return result


def high_precision(src):
    pixel = src.get("pix_fmt", "")
    return (src.get("bits", 0) > 8 or src.get("trc") in {"smpte2084", "arib-std-b67"}
            or bool(re.search(r"(?:9|10|12|14|16)(?:le|be)?$", pixel))
            or pixel.startswith(("rgb48", "rgba64", "gbrpf", "grayf")))


def working_primaries(src):
    if src.get("trc") not in {"smpte2084","arib-std-b67"}:
        return "bt709"
    primaries=src.get("primaries","unknown")
    if primaries=="unknown":
        return "bt2020"
    if primaries not in {"bt709","bt2020","smpte432"}:
        raise ValueError(f"HDR 色域 {primaries} 尚无经过验证的模型代理，请先转换为 BT.2020、BT.709 或 P3-D65。")
    return primaries


def owned_timeline_path(source):
    """Only application-owned generated upload folders receive a sidecar."""
    original=Path(source)
    resolved=original.resolve()
    uploads=(Path(__file__).resolve().parent/"uploads").resolve()
    if resolved.parent.parent!=uploads or not re.fullmatch(r"[0-9a-f]{32}",resolved.parent.name):
        return None
    if original.is_symlink() or original.parent.is_symlink():
        return None
    if os.name=="nt" and any(getattr(item.lstat(),"st_file_attributes",0)&0x400 for item in (original,original.parent)):
        return None
    return resolved.parent/"source.timeline.json"


def timeline_payload(timeline, source_hash, probe_hash):
    encoded=json.dumps(timeline,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    return {"version":1,"source_sha256":source_hash,"ffprobe_sha256":probe_hash,
            "timeline_sha256":hashlib.sha256(encoded).hexdigest(),"timeline":timeline}


def read_timeline_cache(path, source_hash, probe_hash):
    try:
        saved=json.loads(Path(path).read_text(encoding="utf-8"))
        if saved.get("version")!=1 or saved.get("source_sha256")!=source_hash or saved.get("ffprobe_sha256")!=probe_hash:
            return None
        timeline=saved["timeline"]
        points=timeline["pts"]
        if not points or not isinstance(points,list) or any(not isinstance(p,(int,float)) or not math.isfinite(p) for p in points):
            return None
        if any(b<=a for a,b in zip(points,points[1:])) or not math.isfinite(timeline["end"]) or timeline["end"]<=points[-1] or not math.isfinite(timeline["origin"]):
            return None
        if timeline_payload(timeline,source_hash,probe_hash)["timeline_sha256"]!=saved.get("timeline_sha256"):
            return None
        return timeline
    except (OSError,ValueError,KeyError,TypeError,OverflowError):
        return None


def source_timeline(ffprobe, path, src):
    """Stream timestamps from ffprobe, without retaining a full frame JSON tree."""
    command = [ffprobe, "-v", "error", "-select_streams", str(src.get("video_index",0)), "-show_frames",
               "-show_entries", "frame=best_effort_timestamp_time,duration_time,pkt_duration_time",
               "-of", "compact=p=0:nk=0", str(path)]
    times, durations = [], []
    last_report=time.monotonic()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors,
                                   text=True, encoding="utf-8", creationflags=CREATE_FLAGS)
        try:
            for line in process.stdout:
                fields = dict(item.split("=", 1) for item in line.strip().split("|") if "=" in item)
                try:
                    stamp = float(fields["best_effort_timestamp_time"])
                except (KeyError, ValueError):
                    continue
                if not math.isfinite(stamp):
                    continue
                duration = fields.get("duration_time") or fields.get("pkt_duration_time") or "0"
                try:
                    duration = max(0.0, float(duration))
                except ValueError:
                    duration = 0.0
                times.append(stamp)
                durations.append(duration)
                if time.monotonic()-last_report>=1:
                    emit("NRINFO",{"phase":"timeline_scan","frames_scanned":len(times),
                                    "source_seconds":round(stamp-src.get("format_start_time",0),3)})
                    last_report=time.monotonic()
            code = process.wait(timeout=30)
            if code:
                errors.seek(0)
                raise RuntimeError(errors.read().decode("utf-8", "replace")[-3000:])
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    if not times:
        raise ValueError("视频没有可读取的呈现时间戳。")
    # Browser media time and FFmpeg -ss are relative to the container start,
    # not necessarily the first video frame (audio can begin earlier).
    origin = src.get("format_start_time",times[0])
    times = [round(t - origin, 6) for t in times]
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("视频帧时间戳不递增，请先修复时间戳或转换为固定帧率视频。")
    last_duration = durations[-1] or (times[-1] - times[-2] if len(times) > 1 else 1 / src["fps"])
    # Some demuxers store the final packet's nominal duration even for VFR. The
    # stream's declared endpoint is useful only when it actually follows the frame.
    declared = src.get("stream_duration", 0)+max(0,src.get("start_time",origin)-origin) if src.get("stream_duration",0) else 0
    end = declared if times[-1] < declared <= times[-1] + max(1.0, last_duration * 4) else times[-1] + last_duration
    return {"pts": times, "end": round(end, 6), "origin": origin}


def plan_segments(timeline, start, duration, seconds, warmup, frame_cap=0):
    points = timeline["pts"]
    end = min(timeline["end"], start + duration) if duration else timeline["end"]
    first = bisect.bisect_left(points, start - 0.0000005)
    stop = bisect.bisect_left(points, end - 0.0000005)
    if frame_cap:
        stop = min(stop, first + frame_cap)
        end = points[stop] if stop < len(points) else timeline["end"]
    if first >= stop:
        raise ValueError("所选范围内没有完整视频帧。")
    result, current = [], first
    while current < stop:
        boundary = points[current] + seconds
        following = min(stop, max(current + 1, bisect.bisect_left(points, boundary - 0.0000005)))
        finish = points[following] if following < stop else end
        result.append({"index": len(result), "first": current, "stop": following,
                       "warm_first": max(0, current - warmup), "source_start": points[current],
                       "source_end": finish, "start": points[current] - points[first],
                       "end": finish - points[first], "frames": following - current})
        current = following
    return result


def pts_expression(times):
    """A balanced lookup tree avoids expression depth proportional to frame count."""
    values = [str(round(value * 1000000)) for value in times]
    def branch(lo, hi):
        if hi - lo == 1:
            return values[lo]
        mid = (lo + hi) // 2
        return f"if(lt(N,{mid}),{branch(lo,mid)},{branch(mid,hi)})"
    return branch(0, len(values))


def engine_command(args, src, dimensions, pixel_format, transfer):
    command = ([sys.executable, args.exe] if str(args.exe).lower().endswith(".py") else [args.exe])
    command += ["--nr-video", "--nr-in", f"{src['w']}x{src['h']}",
                "--nr-pixel-format", pixel_format, "--nr-transfer", transfer,
                "--nr-fps", str(src["fps"]),"--nr-primaries",working_primaries(src)]
    for name in ("style", "preset", "intensity", "local_structure", "local_tone", "skin", "global_tone",
                 "detail", "color", "ui_correction", "motion", "motion_engine", "sr_preset",
                 "stability", "protect_skin", "protect_highlights", "render_scale"):
        command += ["--nr-" + name.replace("_", "-"), str(getattr(args, "nr_" + name))]
    if args.nr_protect:
        command += ["--nr-protect", args.nr_protect]
    if args.nr_auto_mask:
        command += ["--nr-auto-mask"]
    if args.nr_motion_vis:
        command += ["--nr-motion-vis"]
    if dimensions != (src["w"], src["h"]):
        command += ["--nr-width", str(dimensions[0]), "--nr-height", str(dimensions[1])]
    if args.dll_dir:
        command += ["--dll-dir", args.dll_dir]
    if args.adapter >= 0:
        command += ["--adapter", str(args.adapter)]
    return command


def monitor_pipeline(commands, total, offset, warm, overall, args):
    """Drain every error pipe; terminate all peers after failure or a real stall."""
    processes, threads, logs, metrics = [], [], [], []
    process_started, process_finished = {}, {}
    last_activity = [time.monotonic()]
    visible = [offset]
    lock = threading.Lock()
    started = time.monotonic()
    def drain(pipe, engine=False):
        for raw in iter(pipe.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            with lock:
                last_activity[0] = time.monotonic()
                logs.append(line)
                del logs[:-60]
            progress = re.match(r"NRPROG (\d+) ([\d.]+)", line) if engine else None
            if progress:
                count = min(total, max(0, int(progress.group(1)) - warm))
                visible[0] = offset + count
                elapsed = time.monotonic() - started
                fps = count / elapsed if elapsed else 0
                emit("NRJOB", {"frame": visible[0], "total_frames": overall,
                     "progress": min(94, round(94 * visible[0] / overall)), "fps": round(fps, 2),
                     "eta": nv.fmt_eta((overall-visible[0])/fps) if fps else "--:--",
                     "phase": "render", "message": "正在增强视频"})
            elif engine and line.startswith("NRMETRIC "):
                try:
                    metrics.append(json.loads(line[len("NRMETRIC "):]))
                except (ValueError, TypeError):
                    pass
            elif line and engine:
                print(line, file=sys.stderr, flush=True)
        pipe.close()
    try:
        p1 = subprocess.Popen(commands[0], stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_FLAGS)
        process_started["decode"] = time.monotonic()
        processes.append(p1)
        p2 = subprocess.Popen(commands[1], stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_FLAGS)
        process_started["engine"] = time.monotonic()
        processes.append(p2)
        p1.stdout.close()
        p3 = subprocess.Popen(commands[2], stdin=p2.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=CREATE_FLAGS)
        process_started["checkpoint_encode"] = time.monotonic()
        processes.append(p3)
        p2.stdout.close()
        for process in processes:
            thread = threading.Thread(target=drain, args=(process.stderr, process is p2), daemon=True)
            thread.start()
            threads.append(thread)
        while any(process.poll() is None for process in processes):
            for name,process in zip(("decode","engine","checkpoint_encode"),processes):
                if process.poll() is not None and name not in process_finished:
                    process_finished[name] = time.monotonic()
            failure = next((process.returncode for process in processes if process.poll() not in (None, 0)), None)
            if failure is not None:
                raise RuntimeError("视频处理阶段失败：\n" + "\n".join(logs)[-5000:])
            if time.monotonic() - last_activity[0] > args.stall_timeout:
                raise RuntimeError("视频处理长时间没有进展，已停止本段；已完成片段可继续使用。")
            time.sleep(0.05)
        for thread in threads:
            thread.join(timeout=5)
        codes = [process.wait() for process in processes]
        if any(codes):
            raise RuntimeError("视频处理阶段失败：\n" + "\n".join(logs)[-5000:])
        end=time.monotonic()
        return {"wall_ms": (end - started) * 1000, "engine": metrics[-1] if metrics else {},
                "process_wall_ms": {name:(process_finished.get(name,end)-stamp)*1000 for name,stamp in process_started.items()},
                "process_wall_note":"Stages overlap and may wait for peers; these are lifetimes, not additive compute costs."}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
        for process in processes:
            process.wait()
        for thread in threads:
            thread.join(timeout=2)


def resolve_seam(segment, previous, timeline, work, ffmpeg, raw_pixel, src):
    """Blend two predictions of the SAME source frames, never adjacent times.

    A previous segment keeps a short forward prediction. At a join its first
    prediction continues the old NR history, then fades to the new NR history.
    This removes a hard reset jump without pretending to serialize NGX state.
    """
    started=time.monotonic()
    raw=work/segment["raw_path"]
    fade=min(segment["frames"],max(0,previous.get("stored_frames",previous["frames"])-previous["frames"])) if previous else 0
    if not fade and segment["stored_frames"]==segment["frames"]:
        return {**segment,"path":segment["raw_path"],"sha256":segment["raw_sha256"],"seam_frames":0,"seam_wall_ms":0.0,"seam_source_sha256":None}
    target=work/f"resolved-{segment['index']:06d}.nut"
    partial=target.with_suffix(".partial.nut")
    command=[ffmpeg,"-y","-v","error","-i",str(raw)]
    if not fade:
        command += ["-map","0:v:0","-frames:v",str(segment["frames"]),"-c:v","copy","-f","nut",str(partial)]
    else:
        previous_raw=(work/previous.get("raw_path",previous["path"])).resolve()
        if previous_raw.parent!=work or not previous_raw.is_file() or digest(previous_raw)!=previous.get("raw_sha256",previous["sha256"]):
            raise RuntimeError("上一片段的接缝上下文已损坏，请重新处理该片段。")
        command += ["-i",str(previous_raw)]
        has_rest=segment["frames"]>fade
        graph=(f"[1:v:0]trim=start_frame={previous['frames']}:end_frame={previous['frames']+fade},setpts=PTS-STARTPTS,format=gbrap16le[old];")
        if has_rest:
            graph += "[0:v:0]split=2[headsrc][restsrc];"
            graph += f"[headsrc]trim=end_frame={fade},setpts=PTS-STARTPTS,format=gbrap16le[new];"
        else:
            graph += f"[0:v:0]trim=end_frame={fade},setpts=PTS-STARTPTS,format=gbrap16le[new];"
        # First frame keeps continuous old-history prediction; last frame is
        # entirely the new prediction. Identical predictions remain identical.
        # blend's N is one-based in the bundled FFmpeg; clamp both endpoints.
        weight=f"clip((N-1)/{max(1,fade-1)},0,1)"
        graph += f"[old][new]blend=all_expr='A*(1-({weight}))+B*({weight})':shortest=1:repeatlast=0[head];"
        if has_rest:
            graph += (f"[restsrc]trim=start_frame={fade}:end_frame={segment['frames']},setpts=PTS-STARTPTS,format=gbrap16le[rest];"
                      "[head][rest]concat=n=2:v=1:a=0[joined];")
            label="joined"
        else:
            label="head"
        local_times=[t-segment["source_start"] for t in timeline["pts"][segment["first"]:segment["stop"]]]
        graph += f"[{label}]settb=1/1000000,setpts='{pts_expression(local_times)}',format={raw_pixel if raw_pixel=='rgba64le' else 'bgra'}[resolved]"
        script=work/f"resolved-{segment['index']:06d}.filter"
        script.write_text(graph,encoding="utf-8")
        command += ["-/filter_complex",str(script),"-map","[resolved]","-fps_mode","passthrough","-enc_time_base","1/1000000",
                    "-c:v","ffv1","-level","3","-coder","1","-context","1","-g","1","-slicecrc","1","-f","nut",str(partial)]
    checked(command)
    partial.replace(target)
    return {**segment,"path":target.name,"sha256":digest(target),"bytes":target.stat().st_size,
            "seam_frames":fade,"seam_wall_ms":(time.monotonic()-started)*1000,
            "seam_source_sha256":previous.get("raw_sha256",previous.get("sha256")) if fade else None}


def render_segment(args, src, timeline, segment, work, ffmpeg, ffprobe, raw_pixel, transfer, dimensions, overall, previous=None):
    warm = segment["first"] - segment["warm_first"]
    source_start = timeline["pts"][segment["warm_first"]]
    count = segment["render_stop"] - segment["warm_first"]
    # Reserve a real raw segment plus headroom. This is deliberately independent
    # of guessed FFV1 compression ratios; a failed check leaves prior segments.
    stored_frames=segment["render_stop"]-segment["first"]
    raw_bytes=dimensions[0]*dimensions[1]*(8 if raw_pixel=="rgba64le" else 4)*(stored_frames+segment["frames"])
    disk=shutil.disk_usage(work)
    reserve=max(512*1024*1024, min(disk.total//10,2*1024*1024*1024))
    if not args.dry_run and disk.free < raw_bytes+reserve:
        raise OSError("磁盘剩余空间不足以安全写入下一片段；已完成片段已保留，可释放空间后继续。")
    tags = nv.colour_tags(src)
    setp = f"setparams=colorspace={tags[0]}:color_primaries={tags[1]}:color_trc={tags[2]}"
    work_primary=working_primaries(src)
    decode_filters=[setp]
    decode_filters += [f"scale=flags={nv.SWS}",f"format={raw_pixel}"]
    decode = [ffmpeg, "-v", "error", "-ss", f"{max(0,source_start-.000001):.6f}", "-i", args.inp,
              "-map", f"0:v:{src.get('video_ordinal',0)}", "-an", "-sn", "-dn", "-frames:v", str(count),
              "-vf", ",".join(decode_filters),
              "-fps_mode", "passthrough", "-f", "rawvideo", "-"]
    target = work / f"segment-{segment['index']:06d}.nut"
    partial = target.with_suffix(".partial.nut")
    local_times = [t-segment["source_start"] for t in timeline["pts"][segment["first"]:segment["render_stop"]]]
    filters = [f"trim=start_frame={warm}", "settb=1/1000000", f"setpts='{pts_expression(local_times)}'",
               f"format={raw_pixel if raw_pixel=='rgba64le' else 'bgra'}",
               f"setparams=colorspace=gbr:color_primaries={work_primary if transfer!='sdr' else tags[1]}:color_trc={tags[2]}:range=pc"]
    script = work / f"segment-{segment['index']:06d}.filter"
    script.write_text(",".join(filters), encoding="utf-8")
    encode = [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", raw_pixel,
              "-s", f"{dimensions[0]}x{dimensions[1]}", "-framerate", src["rate"], "-i", "-",
              "-/filter:v", str(script), "-fps_mode", "passthrough", "-enc_time_base", "1/1000000",
              "-c:v", "ffv1", "-level", "3", "-coder", "1", "-context", "1", "-g", "1",
              "-slicecrc", "1", "-f", "nut", str(partial)]
    nr_directory=work/"nr"/f"segment-{segment['index']:06d}"
    model_cache_key=hashlib.sha256(json.dumps({"model":args._nr_model_key,
                    "source_range":[segment["warm_first"],segment["render_stop"]]},sort_keys=True).encode()).hexdigest()
    tool=engine_command(args, src, dimensions, raw_pixel, transfer)
    tool += ["--nr-cache-dir",str(nr_directory),"--nr-cache-frames",str(count),"--nr-cache-key",model_cache_key]
    commands = [decode, tool, encode]
    if args.dry_run:
        for label, command in zip(("decode", "tool", "checkpoint"), commands):
            print(label + ": " + subprocess.list2cmdline(command))
        return None
    for attempt in range(2):
        try:
            metrics = monitor_pipeline(commands, segment["frames"], sum(s["frames"] for s in overall[:segment["index"]]),
                                       warm, sum(s["frames"] for s in overall), args)
            break
        except RuntimeError as exc:
            if attempt or "NR cache identity mismatch" not in str(exc):
                raise
            # Only this generated segment directory may be removed. Resolve and
            # validate the complete target before a recursive Windows deletion.
            allowed=(work/"nr").resolve()
            resolved=nr_directory.resolve()
            if resolved.parent != allowed or nr_directory.is_symlink():
                raise RuntimeError("NR 缓存目录无效，拒绝清理。") from exc
            if nr_directory.exists():
                shutil.rmtree(resolved)
            emit("NRINFO",{"message":"本段 NR 缓存校验不匹配，已丢弃并重新计算。"})
    record = json.loads(checked([ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
                                 "-show_entries", "stream=width,height,nb_read_frames,pix_fmt", "-of", "json", str(partial)]).stdout)["streams"][0]
    if int(record.get("nb_read_frames", 0)) != stored_frames or (record["width"], record["height"]) != dimensions:
        raise RuntimeError("片段帧数或尺寸校验未通过，未提交完成点。")
    if raw_pixel == "rgba64le" and not high_precision({"pix_fmt": record["pix_fmt"]}):
        raise RuntimeError("高精度片段意外降为 8-bit，未提交完成点。")
    partial.replace(target)
    rendered={**segment,"raw_path":target.name,"raw_sha256":digest(target),"stored_frames":stored_frames,
              "completed":True,"metrics":metrics,"pixel_format":record["pix_fmt"]}
    resolved=resolve_seam(rendered,previous,timeline,work,ffmpeg,raw_pixel,src)
    resolved["metrics"]["seam_wall_ms"]=resolved["seam_wall_ms"]
    return resolved


def sdr_proxy_filter(src, rgb_input=True):
    """Explicit HDR-to-SDR conversion; never attach HDR tags to an 8-bit proxy."""
    if src.get("trc") not in {"smpte2084", "arib-std-b67"}:
        return ""
    primaries = src.get("primaries") if src.get("primaries") != "unknown" else "bt2020"
    matrix = "gbr" if rgb_input else nv.colour_tags(src)[0]
    source_range = "full" if rgb_input or src.get("range") == "pc" else "limited"
    return (f"zscale=pin={primaries}:tin={src['trc']}:min={matrix}:rin={source_range}:p={primaries}:m=gbr:t=linear:npl=100,"
            "format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=mobius:desat=0,"
            "zscale=t=bt709:m=bt709:r=limited")


def media_args(src, ext, args):
    streams = src.get("audio_streams", [])
    if not args.preserve_media:
        return nv.audio_args(src, ext, args.audio, args.audio_bitrate)
    result = []
    if args.audio == "none":
        result += ["-an"]
    else:
        for index, stream in enumerate(streams):
            result += ["-map", f"1:a:{index}?"]
            mode = args.audio
            if mode == "auto":
                permitted = nv.COPYABLE_AUDIO[ext]
                mode = "copy" if permitted is None or stream.get("codec_name") in permitted else ("opus" if ext == ".webm" else "aac")
            result += [f"-c:a:{index}", "libopus" if mode == "opus" else "pcm_s24le" if mode == "pcm" else mode]
            if mode in {"aac", "opus"}:
                result += [f"-b:a:{index}", f"{args.audio_bitrate}k"]
    text_subtitles = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}
    selected = 0
    for index, stream in enumerate(src.get("subtitle_streams", [])):
        codec = stream.get("codec_name")
        if ext != ".mkv" and codec not in text_subtitles:
            emit("NRINFO", {"message": f"目标容器不支持字幕 {codec}，保留原素材中的该字幕。"})
            continue
        result += ["-map", f"1:s:{index}?", f"-c:s:{selected}", "copy" if ext == ".mkv" else "webvtt" if ext == ".webm" else "mov_text"]
        selected += 1
    if ext == ".mkv":
        result += ["-map", "1:t?", "-c:t", "copy"]
    result += ["-map_metadata", "1", "-map_chapters", "1"]
    return result


def write_chapters(src, start, duration, target):
    def escape(value):
        return str(value).replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", "\\\n")
    lines = [";FFMETADATA1"]
    for chapter in src.get("chapters", []):
        begin = max(start, float(chapter.get("start_time", 0)))
        end = min(start+duration, float(chapter.get("end_time", 0)))
        if end <= begin + .0000005:
            continue
        lines += ["[CHAPTER]", "TIMEBASE=1/1000000", f"START={round((begin-start)*1000000)}",
                  f"END={round((end-start)*1000000)}"]
        lines += [escape(key)+"="+escape(value) for key,value in chapter.get("tags",{}).items()]
    target.write_text("\n".join(lines)+"\n",encoding="utf-8")


def encode_with_progress(command, checkpoint, stall_timeout):
    """Report real encoded frames/bytes/time so long CPU exports remain observable."""
    command=command[:1]+["-stats_period","1","-progress","pipe:1","-nostats"]+command[1:]
    last=[time.monotonic()]
    observed=[0,0,0]
    duration=max(.000001,checkpoint["duration"])
    with tempfile.TemporaryFile() as errors:
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=errors,creationflags=CREATE_FLAGS)
        def read_progress():
            values={}
            for raw in iter(process.stdout.readline,b""):
                line=raw.decode("utf-8","replace").strip()
                if "=" not in line:continue
                key,value=line.split("=",1);values[key]=value
                if key!="progress":continue
                def integer(name):
                    try:return max(0,int(values.get(name,0)))
                    except (TypeError,ValueError):return 0
                current=[integer("frame"),integer("out_time_us"),integer("total_size")]
                if any(now>before for now,before in zip(current,observed)):
                    observed[:]=[max(now,before) for now,before in zip(current,observed)]
                    last[0]=time.monotonic()
                    emit("NRJOB",{"frame":checkpoint["total_frames"],"total_frames":checkpoint["total_frames"],
                         "encoded_frame":current[0],"encoded_seconds":current[1]/1000000,
                         "progress":min(99,95+int(4*current[1]/1000000/duration)),"phase":"encoding",
                         "message":"正在编码导出视频…"})
        reader=threading.Thread(target=read_progress,daemon=True)
        reader.start()
        try:
            while process.poll() is None:
                if time.monotonic()-last[0]>max(300,stall_timeout):
                    raise RuntimeError("视频编码长时间没有输出进展；增强片段已保留，可更换编码后继续导出。")
                time.sleep(.1)
            reader.join(timeout=5)
            if process.returncode:
                errors.seek(0)
                raise RuntimeError(errors.read().decode("utf-8","replace")[-5000:])
        finally:
            if process.poll() is None:process.kill();process.wait()
            reader.join(timeout=2)
            process.stdout.close()


def export_video(args, src, checkpoint, work, ffmpeg, out_dimensions):
    ext = Path(args.out).suffix.lower()
    codec = nv.resolve_codec(args.codec)
    if codec not in nv.CONTAINERS.get(ext, set()):
        raise ValueError("所选编码与输出容器不兼容。")
    tags = nv.colour_tags(src)
    encoded = copy.copy(args)
    hdr = src["trc"] in {"smpte2084", "arib-std-b67"}
    tone_map = hdr and codec.startswith("h264_")
    if hdr and not tone_map:
        encoded.bit_depth = 10
    if tone_map:
        tags = ("bt709", "bt709", "bt709")
        emit("NRINFO", {"message": "H.264 使用明确映射后的 SDR；HDR 高精度增强片段仍保留，可改选 10-bit 编码导出 HDR。"})
    flags, pixel = nv.video_args(codec, encoded, *out_dimensions, tags)
    if codec == "ffv1" and checkpoint["pixel_format"] == "rgba64le":
        pixel = "rgba64le"
    concat = work / "segments.ffconcat"
    lines = ["ffconcat version 1.0"]
    for segment in checkpoint["segments"]:
        path = segment["path"].replace("'", "'\\''")
        lines += [f"file '{path}'", f"duration {segment['end']-segment['start']:.6f}"]
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    filters = []
    if tone_map:
        filters.append(sdr_proxy_filter({**src,"primaries":checkpoint.get("working_primaries",src["primaries"])}))
    if checkpoint["dimensions"] != list(out_dimensions):
        filters.append(f"scale={out_dimensions[0]}:{out_dimensions[1]}:flags={nv.SWS}")
    rgb = pixel in {"bgr0", "bgra", "rgba64le", "rgb48le", "gbrp16le"}
    if not rgb:
        filters.append(f"scale=out_color_matrix={tags[0]}:out_range=tv:flags={nv.SWS}")
    filters += [f"format={pixel}", f"setparams=colorspace={'gbr' if rgb else tags[0]}:color_primaries={tags[1]}:color_trc={tags[2]}:range={'pc' if rgb else 'tv'}"]
    duration = checkpoint["duration"]
    command = [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
               "-ss", f"{checkpoint['source_start']:.6f}", "-i", args.inp]
    if args.preserve_media:
        chapters = work / "chapters.ffmetadata"
        write_chapters(src,checkpoint["source_start"],duration,chapters)
        command += ["-f","ffmetadata","-i",str(chapters)]
    if hdr and not tone_map:
        # FFmpeg blend copies AVFrame properties from its first input. Use the
        # original source as a metadata/timing donor and the enhanced lossless
        # pixels as the second input. Opacity zero copies those pixels without
        # reintroducing source content, while retaining HDR frame side data.
        width,height=checkpoint["dimensions"]
        properties=("sidedata=mode=delete:type=DISPLAYMATRIX,"
                    "sidedata=mode=delete:type=DOVI_RPU_BUFFER,sidedata=mode=delete:type=DOVI_METADATA,"
                    "sidedata=mode=delete:type=DYNAMIC_HDR_PLUS,sidedata=mode=delete:type=DYNAMIC_HDR_VIVID")
        graph=(f"[1:v:{src.get('video_ordinal',0)}]setpts=PTS-STARTPTS,{properties},scale={width}:{height}:flags=bilinear,format=gbrap16le[properties];"
               f"[0:v:0]format=gbrap16le[pixels];"
               "[properties][pixels]blend=all_mode=normal:all_opacity=0:shortest=1:repeatlast=0[master];"
               f"[master]{','.join(filter(None,filters))}[delivery]")
        command += ["-filter_complex",graph,"-map","[delivery]"]
    else:
        command += ["-map","0:v:0","-vf", ",".join(filter(None, filters))]
    command += media_args(src, ext, args)
    if args.preserve_media:
        command += ["-map_chapters","2"]
    command += ["-fps_mode", "passthrough" if args.timing == "preserve" else "cfr"]
    if args.timing == "cfr":
        command += ["-r", src["rate"]]
    elif ext in {".mp4", ".mov"}:
        command += ["-video_track_timescale", "1000000"]
    if args.timing == "preserve":
        command += ["-enc_time_base", "1/1000000"]
    command += flags + ["-t", f"{duration:.6f}"]
    if ext in {".mp4", ".mov"}:
        command += ["-movflags", "+faststart"]
    temporary = Path(args.out).with_name(Path(args.out).stem + ".partial" + ext)
    command.append(str(temporary))
    if args.dry_run:
        print("export: " + subprocess.list2cmdline(command))
        return 0.0
    emit("NRJOB", {"frame": checkpoint["total_frames"], "total_frames": checkpoint["total_frames"],
                   "progress": 95, "phase": "encoding", "message": "增强片段已完成，正在封装导出…"})
    started = time.monotonic()
    encode_with_progress(command,checkpoint,args.stall_timeout)
    temporary.replace(args.out)
    return (time.monotonic() - started) * 1000


def run_pipeline(args):
    if not 1 <= args.segment_seconds <= 60 or not 0 <= args.warmup_frames <= 120 or not 0<=args.seam_seconds<=1:
        raise ValueError("片段长度或预热帧数无效。")
    if not 15 <= args.stall_timeout <= 3600:
        raise ValueError("无进度超时应在 15–3600 秒之间。")
    ffmpeg, ffprobe = nv.find_tool("ffmpeg"), nv.find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ValueError("缺少 ffmpeg 或 ffprobe，请重新解压完整安装包。")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="dlss-video-") if not args.work_dir else None
    try:
        work = Path(args.work_dir or temporary.name).resolve()
        work.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        source_hash = digest(args.inp,progress=lambda done,total:emit("NRINFO",{"phase":"source_hash","bytes":done,"total_bytes":total}))
        media_tools={"ffprobe":digest(ffprobe),"ffmpeg":digest(ffmpeg)}
        src = nv.probe(ffprobe, args.inp)
        probe_ms = (time.monotonic() - started) * 1000
        if args.start < 0 or args.start >= src["duration"] or args.duration < 0 or args.duration and args.start + args.duration > src["duration"] + .001:
            raise ValueError("视频选区超出素材范围。")
        raw_pixel = "rgba64le" if high_precision(src) else "rgba"
        transfer = "pq" if src["trc"] == "smpte2084" else "hlg" if src["trc"] == "arib-std-b67" else "sdr"
        if transfer != "sdr" and src.get("primaries") == "unknown":
            emit("NRINFO",{"message":"HDR 素材未标明色域，本次明确按 BT.2020 处理。"})
        if any(any(word in data.get("side_data_type","").lower() for word in ("dovi","dolby","dynamic hdr")) for data in src.get("side_data",[])):
            emit("NRINFO",{"message":"增强导出保留静态 HDR 色彩与亮度元数据；不复用原画专属的动态 HDR/Dolby Vision 映射。"})
        desired = nv.output_size(src, args.nr_width, args.nr_height, args.nr_scale, args.nr_fit)
        dimensions = nv.tool_size(src, *desired)
        signature = {"version": PIPELINE_VERSION, "source": source_hash, "application": args.render_key,
                     "pixel_format": raw_pixel, "transfer": transfer, "dimensions": dimensions,
                     "working_primaries":working_primaries(src),
                     "media_tools":media_tools,
                     "segment_seconds": args.segment_seconds, "warmup_frames": args.warmup_frames,
                     "seam_seconds":args.seam_seconds,"seam_policy":"same-source-frame-prediction-crossfade-v1",
                     "range": [args.start, args.duration, args.frames],
                     "model": {k: v for k, v in vars(args).items() if k.startswith("nr_")},
                     "engine_sha256": digest(args.exe)}
        post_fields={"nr_detail","nr_color","nr_stability","nr_protect","nr_protect_skin","nr_protect_highlights","nr_hdr","nr_cache_key"}
        model_controls={key:value for key,value in vars(args).items() if key.startswith("nr_") and key not in post_fields}
        if args.nr_cache_key:
            args._nr_model_key=args.nr_cache_key
        else:
            runtime_dll=Path(args.dll_dir or Path(args.exe).parent)/"nvngx_dlssnr.dll"
            try:
                drivers=checked(["nvidia-smi","--query-gpu=uuid,driver_version","--format=csv,noheader"],timeout=5).stdout.decode("utf-8","replace").strip()
            except (OSError,RuntimeError,subprocess.SubprocessError):
                drivers="unavailable"
            args._nr_model_key=hashlib.sha256(json.dumps({"source":source_hash,"engine":signature["engine_sha256"],
                    "runtime":digest(runtime_dll) if runtime_dll.is_file() else "unavailable","drivers":drivers,
                    "model":model_controls,"pixel_format":raw_pixel,"transfer":transfer,"dimensions":dimensions,
                    "working_primaries":working_primaries(src),"media_tools":media_tools},sort_keys=True).encode()).hexdigest()
        render_key = hashlib.sha256(json.dumps(signature, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        checkpoint_path, timeline_path = work / "checkpoint.json", work / "timeline.json"
        checkpoint = None
        if args.resume and checkpoint_path.exists():
            try:
                candidate = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if candidate.get("render_key") == render_key:
                    checkpoint = candidate
            except (OSError, ValueError):
                pass
        timeline_started = time.monotonic()
        timeline = None
        source_cache=owned_timeline_path(args.inp)
        for candidate in (source_cache,timeline_path):
            if candidate is not None:
                timeline=read_timeline_cache(candidate,source_hash,media_tools["ffprobe"])
                if timeline is not None:break
        if timeline is None:
            timeline = source_timeline(ffprobe, args.inp, src)
        saved=timeline_payload(timeline,source_hash,media_tools["ffprobe"])
        atomic_json(timeline_path,saved)
        if source_cache is not None:
            try:atomic_json(source_cache,saved)
            except OSError:emit("NRINFO",{"message":"源素材时间轴缓存暂时无法保存，本次处理继续使用已校验时间轴。"})
        timeline_ms = (time.monotonic() - timeline_started) * 1000
        plan = plan_segments(timeline, args.start, args.duration, args.segment_seconds, args.warmup_frames, args.frames)
        overlap=min(30,max(2,round(src["fps"]*args.seam_seconds))) if args.seam_seconds else 0
        for segment in plan:
            segment["render_stop"]=min(plan[-1]["stop"],segment["stop"]+overlap)
        if checkpoint is None:
            checkpoint = {"version": 1, "render_key": render_key, "application_render_key": args.render_key,
                          "pipeline": PIPELINE_VERSION, "source": {"sha256": source_hash,"color":nv.colour_tags(src),"hdr_side_data":src.get("side_data",[])},
                          "source_start": plan[0]["source_start"], "duration": plan[-1]["end"],
                          "total_frames": sum(segment["frames"] for segment in plan),
                          "pixel_format": raw_pixel, "transfer": transfer, "dimensions": list(dimensions),
                          "working_primaries":working_primaries(src),
                          "segments": [], "metrics": {}}
        selected_times = timeline["pts"][plan[0]["first"]:plan[-1]["stop"]]
        relative_times = [round(stamp-checkpoint["source_start"], 6) for stamp in selected_times]
        result_times = relative_times if args.timing == "preserve" else [round(round(stamp*src["fps"])/src["fps"],6) for stamp in relative_times]
        atomic_json(work/"frame-map.json", {"version":1, "actual_start":checkpoint["source_start"],
                    "source_pts_origin":timeline["origin"], "source_times":selected_times,
                    "result_times":result_times, "timing":args.timing})
        checkpoint.update(actual_start=checkpoint["source_start"], source_pts_origin=timeline["origin"], timeline_file="frame-map.json")
        existing = {segment["index"]: segment for segment in checkpoint["segments"]}
        completed, reused, render_ms, reused_indexes = [], 0, 0.0, set()
        for segment in plan:
            previous = existing.get(segment["index"])
            valid = (previous and previous.get("completed") and previous.get("frames") == segment["frames"]
                     and previous.get("source_start") == segment["source_start"]
                     and previous.get("source_end") == segment["source_end"])
            if valid:
                path = work / previous["path"]
                valid = path.is_file() and path.resolve().parent == work and digest(path) == previous.get("sha256")
                if valid and previous.get("raw_path"):
                    raw_path=work/previous["raw_path"]
                    valid=raw_path.is_file() and raw_path.resolve().parent==work and digest(raw_path)==previous.get("raw_sha256")
                if valid and previous.get("seam_frames"):
                    predecessor=completed[-1] if completed else None
                    valid=bool(predecessor and previous.get("seam_source_sha256")==predecessor.get("raw_sha256",predecessor.get("sha256")))
            if valid:
                completed.append(previous)
                reused += segment["frames"]
                reused_indexes.add(segment["index"])
                continue
            if args.pause_file and Path(args.pause_file).exists():
                checkpoint["segments"] = completed
                checkpoint["metrics"].update(reused_frames=reused)
                atomic_json(checkpoint_path, checkpoint)
                emit("NRSTATE", {"status": "paused", "completed_frames": sum(s["frames"] for s in completed)})
                return PAUSED
            value = render_segment(args, src, timeline, segment, work, ffmpeg, ffprobe, raw_pixel, transfer, dimensions, plan,completed[-1] if completed else None)
            if args.dry_run:
                return 0
            completed.append(value)
            render_ms += value["metrics"]["wall_ms"]
            checkpoint["segments"] = completed
            checkpoint["metrics"] = {"source_probe_ms": probe_ms, "timeline_probe_ms": timeline_ms,
                                     "render_wall_ms": render_ms, "reused_frames": reused,
                                     "frames": checkpoint["total_frames"]}
            atomic_json(checkpoint_path, checkpoint)
        if args.pause_file and Path(args.pause_file).exists():
            emit("NRSTATE", {"status": "paused", "completed_frames": checkpoint["total_frames"]})
            return PAUSED
        checkpoint["metrics"].update(source_probe_ms=probe_ms, timeline_probe_ms=timeline_ms,
                                       render_wall_ms=render_ms, reused_frames=reused, frames=checkpoint["total_frames"])
        checkpoint["metrics"]["encode_wall_ms"] = export_video(args, src, checkpoint, work, ffmpeg, desired)
        if args.dry_run:
            return 0
        engine_totals, gpu_totals, cached_nr_frames, evaluated_nr_frames, initialization_ms = {}, {}, 0, 0, 0.0
        for segment in completed:
            if segment["index"] in reused_indexes:
                continue
            engine_metrics=segment.get("metrics", {}).get("engine", {})
            cached_nr_frames += engine_metrics.get("cached_nr_frames",0)
            evaluated_nr_frames += max(0,engine_metrics.get("frames",0)-engine_metrics.get("cached_nr_frames",0))
            initialization_ms += engine_metrics.get("initialization_ms",0)
            for name, value in engine_metrics.get("stage_ms", {}).items():
                if isinstance(value, (int, float)):
                    engine_totals[name] = engine_totals.get(name, 0) + value
            for name, value in engine_metrics.get("gpu_stage_ms", {}).items():
                if isinstance(value, (int, float)):
                    gpu_totals[name] = gpu_totals.get(name, 0) + value
        checkpoint["metrics"]["engine_stage_ms"] = engine_totals
        checkpoint["metrics"]["seam_wall_ms"]=sum(segment.get("seam_wall_ms",0) for segment in completed if segment["index"] not in reused_indexes)
        checkpoint["metrics"].update(engine_gpu_stage_ms=gpu_totals,cached_nr_frames=cached_nr_frames,
                nr_evaluated_frames=evaluated_nr_frames,initialization_ms=initialization_ms,
                engine_timing_note="stage_ms includes synchronization; gpu_stage_ms is native timestamp timing. Counts include pre-roll. Reused final segments do not count as this-run NR work.")
        checkpoint["metrics"]["total_wall_ms"] = (time.monotonic() - started) * 1000
        checkpoint["output"] = {"codec": args.codec, "timing": args.timing, "bytes": Path(args.out).stat().st_size}
        checkpoint["output"]["hdr_mode"]="sdr_tonemapped" if transfer!="sdr" and args.codec.startswith("h264_") else "static_hdr" if transfer!="sdr" else "sdr"
        atomic_json(checkpoint_path, checkpoint)
        emit("NRMETRICS", checkpoint["metrics"])
        emit("NRJOB", {"frame": checkpoint["total_frames"], "total_frames": checkpoint["total_frames"],
                       "progress": 100, "phase": "done", "message": "视频导出完成"})
        return 0
    finally:
        if temporary:
            temporary.cleanup()
