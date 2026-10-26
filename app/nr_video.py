"""DLSS video CLI and reusable geometry/codec helpers.

video_pipeline owns source-PTS restoration, high-precision RGB checkpoints,
segment pre-roll, pause/resume and the separate delivery-encoding stage.
SDR 8-bit uses RGBA; HDR/high-bit-depth input uses RGBA64 throughout the NR
interface and lossless intermediate, independently of delivery codec choice.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


# ----------------------------------------------------------------------------- encoders

# name -> (ffmpeg encoder, family). Families: nvenc (GPU), sw (CPU), prores, ffv1 (lossless).
CODECS = {
    "hevc_nvenc": ("hevc_nvenc", "nvenc"),
    "h264_nvenc": ("h264_nvenc", "nvenc"),
    "h264_cpu": ("libopenh264", "openh264"),
    "av1_nvenc": ("av1_nvenc", "nvenc"),
    "av1_svt": ("libsvtav1", "sw"),
    "prores": ("prores_ks", "prores"),
    "ffv1": ("ffv1", "ffv1"),
}
CODEC_ALIASES = {
    "hevc": "hevc_nvenc", "h265": "hevc_nvenc", "h.265": "hevc_nvenc", "x265": "hevc_nvenc",
    "h264": "h264_nvenc", "h.264": "h264_nvenc", "avc": "h264_nvenc", "x264": "h264_nvenc",
    "av1": "av1_nvenc", "libsvtav1": "av1_svt", "svtav1": "av1_svt", "svt-av1": "av1_svt",
    "prores_ks": "prores", "lossless": "ffv1",
    "libopenh264": "h264_cpu", "openh264": "h264_cpu",
}
# Which codecs each container can legally hold (what mainstream players will actually open).
CONTAINERS = {
    ".mp4": {"hevc_nvenc", "h264_nvenc", "h264_cpu", "av1_nvenc", "av1_svt"},
    ".mov": {"hevc_nvenc", "h264_nvenc", "h264_cpu", "prores"},
    ".mkv": set(CODECS),
    ".webm": {"av1_nvenc", "av1_svt"},
}
# Audio codecs a container can carry unchanged (else the track is re-encoded).
COPYABLE_AUDIO = {
    ".mp4": {"aac", "mp3", "ac3", "eac3", "alac", "opus", "flac"},
    ".mov": {"aac", "mp3", "ac3", "eac3", "alac", "pcm_s16le", "pcm_s24le", "pcm_s16be", "pcm_s24be"},
    ".mkv": None,  # anything
    ".webm": {"opus", "vorbis"},
}
PRORES_PROFILES = ["proxy", "lt", "standard", "hq", "4444", "4444xq"]
# NVENC hardware limits (Ada / Blackwell): H.264 stops at 4096x4096, HEVC / AV1 at 8192x8192.
NVENC_MAX = {"h264_nvenc": 4096, "hevc_nvenc": 8192, "av1_nvenc": 8192}
# Chroma / scaling flags for every swscale conversion: proper 4:2:0 <-> RGB resampling instead of
# the default nearest-neighbour chroma, and lanczos for any size change.
SWS = "lanczos+accurate_rnd+full_chroma_int+full_chroma_inp"


def find_tool(name):
    # Prefer a copy bundled next to the tool (out\), then PATH, then a winget install.
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "out", name + ".exe"), os.path.join(here, name + ".exe")):
        if os.path.isfile(cand):
            return cand
    p = shutil.which(name)
    if p:
        return p
    root = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    for hit in glob.glob(os.path.join(root, "Gyan.FFmpeg*", "**", name + ".exe"), recursive=True):
        return hit
    return None


def probe(ffprobe, path):
    """Video geometry / rate / colour tags and the first audio codec, via ffprobe JSON."""
    out = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_streams",
                          "-show_format", "-show_chapters", path], capture_output=True, text=True, check=True,
                         timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    info = json.loads(out or "{}")
    video_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    v = next((s for s in video_streams if not s.get("disposition", {}).get("attached_pic")), None)
    if not v:
        raise ValueError("文件中没有可读取的视频画面。")
    # ffmpeg autorotates decoded frames. Raw RGBA has no geometry metadata, so the
    # renderer, encoder, upload response and size presets must use that same geometry.
    rotation = next((s["rotation"] for s in v.get("side_data_list", []) if "rotation" in s),
                    v.get("tags", {}).get("rotate", 0))
    rotation = float(rotation) % 360
    quarter = round(rotation / 90) % 4
    if abs((rotation - quarter * 90 + 180) % 360 - 180) > 1:
        raise ValueError("视频包含非直角旋转，请先将方向转正后再上传。")
    width, height = int(v["width"]), int(v["height"])
    if quarter % 2:
        width, height = height, width
    audio_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    a = audio_streams[0] if audio_streams else None
    def valid_rate(value):
        try:
            num, den = str(value).split("/")
            return float(num) > 0 and float(den) > 0
        except (ValueError, TypeError):
            return False
    rate = next((value for value in (v.get("avg_frame_rate"), v.get("r_frame_rate")) if valid_rate(value)), "25/1")
    num, den = (rate.split("/") + ["1"])[:2]
    fps = float(num) / float(den or 1)
    try:
        frames = int(v.get("nb_frames") or 0)
    except (ValueError, TypeError):
        frames = 0
    if frames <= 0:  # some containers don't store a frame count; estimate from duration
        try:
            dur = float(v.get("duration") or info.get("format", {}).get("duration") or 0)
            frames = round(dur * fps)
        except ValueError:
            frames = 0
    unk = lambda s: (s or "unknown") if s not in ("", None, "unspecified", "reserved") else "unknown"
    def number(value, default=0):
        try:
            result = float(value)
            return result if result == result and abs(result) != float("inf") else default
        except (TypeError, ValueError):
            return default
    format_start=number(info.get("format", {}).get("start_time"))
    video_start=number(v.get("start_time"),format_start)
    stream_duration=number(v.get("duration"))
    extent=max(0,video_start-format_start)+stream_duration if stream_duration else number(info.get("format", {}).get("duration"),frames/fps if fps else 0)
    return {
        "w": width, "h": height, "rotation": rotation, "fps": fps, "rate": rate, "frames": frames,
        "duration": extent,
        "pix_fmt": v.get("pix_fmt", ""), "range": unk(v.get("color_range")),
        "matrix": unk(v.get("color_space")), "primaries": unk(v.get("color_primaries")),
        "trc": unk(v.get("color_transfer")),
        "audio": a.get("codec_name") if a else None,
        "audio_streams": audio_streams,
        "subtitle_streams": [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"],
        "attachments": [s for s in info.get("streams", []) if s.get("codec_type") == "attachment"],
        "start_time": video_start,
        "format_start_time": format_start,
        "stream_duration": stream_duration,
        "video_index": int(v.get("index",0)),
        "video_ordinal": video_streams.index(v),
        "video_codec": v.get("codec_name", "unknown"),
        "format_name": info.get("format", {}).get("format_name", "unknown"),
        "avg_rate": v.get("avg_frame_rate"),
        "bits": int(number(v.get("bits_per_raw_sample"))),
        "side_data": v.get("side_data_list", []),
        "chapters": info.get("chapters", []),
    }


def colour_tags(src):
    """The (matrix, primaries, trc) the clip is in. Unknown tags default to bt709 (SD: bt601).

    Known HDR tags describe the encoded source and delivery signal. The render
    engine receives a separate transfer identifier and handles its model proxy.
    """
    sd = src["h"] <= 576 and src["w"] <= 1024
    hdr = src.get("trc") in {"smpte2084", "arib-std-b67"}
    # Only Y'CbCr matrices can be carried into a YUV output; an RGB source reports "gbr".
    yuv_matrices = {"bt709", "bt470bg", "smpte170m", "smpte240m", "bt2020nc", "bt2020c", "fcc"}
    m = src["matrix"] if src["matrix"] in yuv_matrices else ("bt2020nc" if hdr else "smpte170m" if sd else "bt709")
    p = src["primaries"] if src["primaries"] != "unknown" else ("bt2020" if hdr else "smpte170m" if sd else "bt709")
    t = src["trc"] if src["trc"] != "unknown" else ("smpte170m" if sd else "bt709")
    return m, p, t


def even(v):
    v = int(round(v))
    return max(2, v - v % 2)


def output_size(src, width, height, scale, fit=""):
    """(outW, outH): --nr-fit fits inside a WxH box (turned to match a portrait source), aspect kept;
    width and height together pin the exact size; one of them keeps the aspect; else scale."""
    w, h = src["w"], src["h"]
    if fit:
        bw, bh = (int(x) for x in fit.lower().split("x"))
        if (h > w) != (bh > bw):
            bw, bh = bh, bw
        f = min(bw / w, bh / h)
        ow, oh = w * f, h * f
    elif width and height:
        ow, oh = width, height
    elif width:
        ow, oh = width, h * width / w
    elif height:
        ow, oh = w * height / h, height
    else:
        ow, oh = w * scale, h * scale
    return even(ow), even(oh)


def tool_size(src, outW, outH):
    """What the tool renders at. DLSS only enlarges, so any shrink happens in ffmpeg afterwards:
    f = the larger of the two ratios; f <= 1 means NR at native size, then ffmpeg downsizes."""
    f = max(outW / src["w"], outH / src["h"])
    if f <= 1.0:
        return src["w"], src["h"]
    if (even(src["w"] * f), even(src["h"] * f)) == (outW, outH):
        return outW, outH
    return even(src["w"] * f), even(src["h"] * f)


def resolve_codec(name):
    key = CODEC_ALIASES.get(name.lower(), name.lower())
    if key not in CODECS:
        sys.exit(f"unknown --codec {name!r}; one of: {', '.join(CODECS)} (aliases: hevc, h264, av1, "
                 f"svtav1, lossless)")
    return key


def audio_args(src, ext, mode, kbps):
    """-map / -c:a for the audio track. auto = copy when the container allows it, else re-encode."""
    if mode == "none" or not src["audio"]:
        return ["-an"]
    args = ["-map", "1:a:0?"]
    if mode == "auto":
        ok = COPYABLE_AUDIO[ext]
        mode = "copy" if (ok is None or src["audio"] in ok) else ("opus" if ext == ".webm" else "aac")
    if mode == "copy":
        return args + ["-c:a", "copy"]
    if mode == "aac":
        return args + ["-c:a", "aac", "-b:a", f"{kbps}k"]
    if mode == "opus":
        return args + ["-c:a", "libopus", "-b:a", f"{kbps}k"]
    if mode == "flac":
        return args + ["-c:a", "flac"]
    if mode == "pcm":
        return args + ["-c:a", "pcm_s24le"]
    sys.exit(f"unknown --audio {mode!r}")


def video_args(codec, args, outW, outH, tags):
    """Encoder flags + the pixel format the filter chain must deliver."""
    enc, family = CODECS[codec]
    ten = args.bit_depth == 10
    matrix, primaries, trc = tags
    colour = ["-colorspace", matrix, "-color_primaries", primaries, "-color_trc", trc,
              "-color_range", "tv"]
    if family == "nvenc":
        lim = NVENC_MAX[enc]
        if outW > lim or outH > lim:
            sys.exit(f"{codec} cannot encode {outW}x{outH} (hardware limit {lim}); use hevc_nvenc / "
                     f"av1_nvenc for above-4K, or a smaller output")
        if enc == "h264_nvenc" and ten:
            print("h264_nvenc is 8-bit only; encoding 8-bit (use hevc_nvenc / av1_nvenc for 10-bit)",
                  file=sys.stderr)
            ten = False
        pix = "p010le" if ten else "yuv420p"
        v = ["-c:v", enc, "-preset", args.enc_preset, "-tune", "hq", "-multipass", args.multipass,
             "-spatial-aq", "1", "-temporal-aq", "1", "-aq-strength", "8", "-rc-lookahead", "32",
             "-bf", "3", "-b_ref_mode", "middle"]
        if args.bitrate > 0:  # explicit target bitrate (VBR, 2x headroom for peaks)
            v += ["-rc", "vbr", "-b:v", f"{args.bitrate}k", "-maxrate", f"{2 * args.bitrate}k",
                  "-bufsize", f"{4 * args.bitrate}k"]
        else:  # constant quality: bits follow the content, the number is the quality target
            v += ["-rc", "vbr", "-cq", str(args.cq), "-b:v", "0"]
        if enc == "hevc_nvenc":
            v += ["-profile:v", "main10" if ten else "main", "-tier", "high"]
        elif enc == "h264_nvenc":
            v += ["-profile:v", "high"]
        return v + colour, pix
    if family == "openh264":
        # The bundled LGPL FFmpeg provides OpenH264; it has no CQ/CRF control.
        bitrate = args.bitrate or (6000 if max(outW, outH) <= 1920 else 9000 if max(outW, outH) <= 2560 else 12000)
        return ["-c:v", enc, "-profile:v", "high", "-rc_mode", "bitrate", "-b:v", f"{bitrate}k",
                "-maxrate", f"{2 * bitrate}k", "-allow_skip_frames", "0"] + colour, "yuv420p"
    if family == "sw":  # libsvtav1
        pix = "yuv420p10le" if ten else "yuv420p"
        v = ["-c:v", enc, "-preset", str(args.sw_preset), "-svtav1-params", "tune=0"]
        v += ["-b:v", f"{args.bitrate}k"] if args.bitrate > 0 else ["-crf", str(args.cq)]
        return v + colour, pix
    if family == "prores":
        prof = args.prores_profile
        pix = "yuv444p10le" if prof.startswith("4444") else "yuv422p10le"
        return ["-c:v", enc, "-profile:v", prof, "-vendor", "apl0"] + colour, pix
    if family == "ffv1":  # mathematically lossless, RGB exactly as the tool produced it
        return ["-c:v", enc, "-level", "3", "-coder", "1", "-context", "1", "-g", "1",
                "-slices", "16", "-slicecrc", "1"], "bgr0"
    raise AssertionError(family)


# ----------------------------------------------------------------------------- progress

def fmt_eta(seconds):
    if seconds < 0 or seconds != seconds:  # negative / NaN
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def render_progress(cur, total, fps):
    """One-line progress bar with running fps and ETA, on stdout."""
    if total > 0:
        frac = min(1.0, cur / total)
        bar_w = 28
        fill = int(bar_w * frac)
        bar = "=" * fill + (">" if fill < bar_w else "") + " " * (bar_w - fill - 1)
        eta = fmt_eta((total - cur) / fps) if fps > 0 else "--:--"
        sys.stdout.write(f"\r  [{bar}] {cur:>5}/{total} ({frac*100:4.0f}%)  {fps:5.1f} fps  ETA {eta}   ")
    else:
        sys.stdout.write(f"\r  {cur:>6} frames  {fps:5.1f} fps   ")
    sys.stdout.flush()


def pump_progress(stderr, total, state):
    """Read the tool's stderr; turn NRPROG lines into a live bar, pass everything else through."""
    prog = re.compile(r"^NRPROG (\d+) ([\d.]+)")
    for raw in iter(stderr.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        m = prog.match(line)
        if m:
            state["frames"] = int(m.group(1))
            state["fps"] = float(m.group(2))
            render_progress(state["frames"], total, state["fps"])
        elif line.startswith("done:"):
            state["done"] = line
        elif line:
            # a real log/error line from the tool - break the bar and show it
            sys.stdout.write("\n")
            sys.stdout.flush()
            print(line, file=sys.stderr)


# ----------------------------------------------------------------------------- main

def build_parser():
    ap = argparse.ArgumentParser(
        description="DLSS Super Resolution + Neural Rendering over a clip (ffmpeg <-> video2dlssnr).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True, help="output file; .mp4 / .mkv / .mov / .webm picks the container")
    ap.add_argument("--exe", default=os.path.join(os.path.dirname(__file__), "out", "video2dlssnr.exe"))
    ap.add_argument("--dll-dir", default="", help="override nvngx dir (default: next to video2dlssnr.exe)")
    ap.add_argument("--adapter", type=int, default=-1, help="DXGI adapter index (default: fastest)")

    g = ap.add_argument_group("output size")
    g.add_argument("--nr-width", type=int, default=0, help="output width (height by aspect)")
    g.add_argument("--nr-height", type=int, default=0, help="output height (width by aspect); with "
                                                            "--nr-width: the exact size")
    g.add_argument("--nr-scale", type=float, default=1.0, help="scale factor when width/height unset")
    g.add_argument("--nr-sr-preset", default="default",
                   help="DLSS Super Resolution model preset for the upscale: default (driver picks "
                        "per mode) or a letter A..O. E = CNN (DLSS 3.7 default), F = CNN (Ultra "
                        "Performance / DLAA), J = DLSS 4 transformer (first), K = DLSS 4 transformer "
                        "(default), L / M = DLSS 4.5 transformer (newest)")
    g.add_argument("--nr-fit", default="", metavar="WxH",
                   help="fit inside this box keeping the aspect, e.g. 3840x2160 (a portrait clip "
                        "gets the box turned); overrides width / height / scale")

    # NR / scale flags are named exactly like video2dlssnr.exe (--nr-*), so the same knobs carry over.
    g = ap.add_argument_group("neural rendering (same flags as video2dlssnr.exe)")
    g.add_argument("--nr-style", type=int, default=0, help="0 default / 1 natural / 2 cinematic")
    g.add_argument("--nr-preset", type=int, default=0)
    g.add_argument("--nr-intensity", type=float, default=1.0, help="DLSSNR.Intensity (0-2)")
    g.add_argument("--nr-local-structure", type=float, default=1.0, help="DLSSNR.LocalStructureStrength (0-2)")
    g.add_argument("--nr-local-tone", type=float, default=1.0, help="DLSSNR.LocalToneStrength (0-2)")
    g.add_argument("--nr-skin", type=float, default=-1.0, help="DLSSNR.SkinStructureStrength (-1 = model default)")
    g.add_argument("--nr-global-tone", type=float, default=-1.0, help="DLSSNR.GlobalToneStrength (<0 = model default)")
    g.add_argument("--nr-detail", type=float, default=1.0, help="composite strength (0 = original, 1 = full NR)")
    g.add_argument("--nr-color", type=float, default=1.0, help="0 = keep original hue, 1 = NR colour")
    g.add_argument("--nr-hdr", action="store_true", help="legacy flag; PQ/HLG video is detected and preserved automatically")
    g.add_argument("--nr-stability", type=float, default=0, help="signed correction temporal stabilization (0-1)")
    g.add_argument("--nr-protect", "--nr-protect-region", default="", help="normalized original-protection rectangle x,y,w,h")
    g.add_argument("--nr-protect-skin", type=float, default=0, help="original skin protection (0-1)")
    g.add_argument("--nr-protect-highlights", type=float, default=0, help="original highlight protection (0-1)")
    g.add_argument("--nr-render-scale", type=float, default=1, help="NR work scale, output geometry remains unchanged (0.5-1)")
    g.add_argument("--nr-cache-key", default="", help="runtime/model identity for bounded pre-composite NR frame reuse")
    g.add_argument("--nr-ui-correction", type=int, default=0, help="DLSSNR.UICorrection (0/1)")
    g.add_argument("--nr-auto-mask", action="store_true", help="enable DLSSNR.UseAutoMask")
    g.add_argument("--nr-motion", type=int, default=1, help="optical-flow motion vectors for NR (0/1)")
    g.add_argument("--nr-motion-engine", choices=["auto", "nvof", "lk"], default="auto",
                   help="flow backend: auto (NVOFA else LK) / nvof (hardware) / lk (Lucas-Kanade)")
    g.add_argument("--nr-motion-vis", action="store_true",
                   help="output the flow visualisation instead of the NR result (debug)")
    g.add_argument("--frames", type=int, default=0, help="cap frames (0 = whole clip)")
    g.add_argument("--start", type=float, default=0, help="start at this source time in seconds")
    g.add_argument("--duration", type=float, default=0, help="duration in seconds (0 = remainder)")

    g = ap.add_argument_group("encoding")
    g.add_argument("--codec", default="hevc_nvenc",
                   help="hevc_nvenc (default) / h264_nvenc / av1_nvenc (GPU); av1_svt (CPU AV1); "
                        "prores (CPU, .mov/.mkv); ffv1 (lossless RGB, .mkv)")
    g.add_argument("--cq", type=int, default=19,
                   help="constant-quality target, lower = better: 15 near-transparent, 19 high "
                        "(default), 23 medium, 28 small. crf for av1_svt; ignored by prores / ffv1")
    g.add_argument("--bitrate", type=int, default=0, help="target bitrate in kbit/s instead of --cq (0 = use --cq)")
    g.add_argument("--bit-depth", type=int, choices=[8, 10], default=10,
                   help="10 (default; less banding, better compression) or 8 (max compatibility). "
                        "h264_nvenc is always 8")
    g.add_argument("--enc-preset", default="p5", help="NVENC preset p1(fast)..p7(quality), default p5")
    g.add_argument("--multipass", choices=["disabled", "qres", "fullres"], default="qres",
                   help="NVENC two-pass mode (default qres)")
    g.add_argument("--sw-preset", type=int, default=6, help="av1_svt speed preset 0(slow)..13(fast), default 6")
    g.add_argument("--prores-profile", choices=PRORES_PROFILES, default="hq")
    g.add_argument("--audio", choices=["auto", "copy", "aac", "opus", "flac", "pcm", "none"], default="auto",
                   help="auto (default) copies the track when the container allows, else re-encodes")
    g.add_argument("--audio-bitrate", type=int, default=192, help="kbit/s for aac / opus (default 192)")
    g.add_argument("--preserve-media", type=int, choices=[0, 1], default=1, help="preserve compatible audio tracks, subtitles, chapters and attachments")
    g.add_argument("--timing", choices=["preserve", "cfr"], default="preserve", help="preserve source presentation times or explicitly convert to constant frame rate")
    g.add_argument("--work-dir", default="", help="persistent rendered-segment cache; omit for temporary workspace")
    g.add_argument("--render-key", default="", help="application render identity, excluding encoding choices")
    g.add_argument("--resume", action="store_true", help="reuse validated completed segments from work directory")
    g.add_argument("--pause-file", default="", help="pause at a completed segment boundary when this file exists (exit 75)")
    g.add_argument("--segment-seconds", type=float, default=10, help="checkpoint segment duration (1-60 seconds)")
    g.add_argument("--warmup-frames", type=int, default=16, help="preceding source frames processed but not written at each segment (0-120)")
    g.add_argument("--seam-seconds", type=float, default=.25, help="same-source-frame prediction overlap at segment joins (0 disables; 0-1 seconds)")
    g.add_argument("--stall-timeout", type=float, default=180, help="fail a render that makes no observable progress for this many seconds")
    g.add_argument("--dry-run", action="store_true", help="print the three commands and exit")
    return ap


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args()
    # The checkpointed runner owns source timestamps and high-precision intermediates.
    # Kept as a separate module so the CLI geometry/codec helpers remain reusable.
    from video_pipeline import run_pipeline
    try:
        return run_pipeline(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
