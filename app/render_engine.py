"""Rendering settings and helpers shared by the Studio API and legacy UI. No UI imports."""
import os
import sys
import subprocess
import time
from nr_video import find_tool

ROOT = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(ROOT, "out", "video2dlssnr.exe")
NRV = os.path.join(ROOT, "nr_video.py")
OUT_DIR = os.path.join(ROOT, "ui_out")
# nvngx_dlssnr.dll is expected next to video2dlssnr.exe; the tool finds it there on its own.

STYLES = {"默认": 0, "自然": 1, "电影": 2}
PRESETS = {"默认": 0, "预设 1": 1, "预设 2": 2, "预设 3": 3}
# DLSS Super Resolution model preset for the upscale stage. Default = the driver picks per mode;
# The Studio exposes only DLSS 5 NR; the internal SR stage stays on driver default.
SR_PRESETS = {"自动选择（推荐）": "default"}

# Output size presets. Fixed sizes fit inside the box keeping the aspect (a portrait clip gets the
# box turned); "自定义" uses the width / height fields (height 0 = keep aspect).
SIZES = {
    "原始尺寸（不放大）": ("scale", 1.0),
    "×1.5": ("scale", 1.5),
    "×2": ("scale", 2.0),
    "×3": ("scale", 3.0),
    "720p (1280×720)": ("fit", "1280x720"),
    "1080p (1920×1080)": ("fit", "1920x1080"),
    "1440p (2560×1440)": ("fit", "2560x1440"),
    "4K (3840×2160)": ("fit", "3840x2160"),
    "5K (5120×2880)": ("fit", "5120x2880"),
    "8K (7680×4320)": ("fit", "7680x4320"),
    "自定义": ("custom", None),
}

# Video codecs: label -> nr_video.py --codec. NVENC = GPU, the rest run on the CPU.
CODECS = {
    "HEVC / H.265 · 显卡编码": "hevc_nvenc",
    "H.264 · 显卡编码": "h264_nvenc",
    "H.264 · CPU 编码": "h264_cpu",
    "AV1 · 显卡编码": "av1_nvenc",
    "AV1 · CPU 编码": "av1_svt",
    "ProRes · CPU 编码": "prores",
    "FFV1 无损 · CPU 编码": "ffv1",
}
CONTAINERS = {  # which containers each codec can go into (first = default)
    "hevc_nvenc": ["mp4", "mkv", "mov"],
    "h264_nvenc": ["mp4", "mkv", "mov"],
    "h264_cpu": ["mp4", "mkv", "mov"],
    "av1_nvenc": ["mp4", "mkv", "webm"],
    "av1_svt": ["mp4", "mkv", "webm"],
    "prores": ["mov", "mkv"],
    "ffv1": ["mkv"],
}
QUALITIES = {  # constant-quality targets (lower = better); "自定义" opens the CQ / bitrate fields
    "极高画质（CQ 15）": 15,
    "高画质（CQ 19）": 19,
    "均衡（CQ 23）": 23,
    "节省空间（CQ 28）": 28,
    "自定义": None,
}
AUDIO = ["auto", "copy", "aac", "opus", "flac", "none"]
PRORES_PROFILES = ["proxy", "lt", "standard", "hq", "4444", "4444xq"]


def windows_build():
    return sys.getwindowsversion().build if sys.platform == "win32" else 0


def default_video_codec(build, available):
    """Keep the OS's default format when hardware encoding is unavailable."""
    hardware, software = ("av1_nvenc", "av1_svt") if build >= 22000 else ("h264_nvenc", "h264_cpu")
    return hardware if available.get(hardware, False) else software


def recommended_bitrate(size, width=1920, height=1080):
    kind, value = SIZES[size]
    long_edge = max(map(int, value.split("x"))) if kind == "fit" else max(width, height)
    return 6000 if long_edge <= 1920 else 9000 if long_edge <= 2560 else 12000


def nr_model_args(style, preset, intensity, local_structure, local_tone, skin, global_tone,
                  detail, color, ui_correction, auto_mask, hdr):
    """The NR model + composite flags shared by both tabs (same names as the CLI)."""
    a = ["--nr-style", str(STYLES[style]), "--nr-preset", str(PRESETS[preset]),
         "--nr-intensity", str(intensity), "--nr-local-structure", str(local_structure),
         "--nr-local-tone", str(local_tone), "--nr-skin", str(skin),
         "--nr-global-tone", str(global_tone), "--nr-detail", str(detail),
         "--nr-color", str(color), "--nr-ui-correction", "1" if ui_correction else "0"]
    if auto_mask:
        a += ["--nr-auto-mask"]
    if hdr:
        a += ["--nr-hdr"]
    return a


def fit_box(w, h, box):
    """Largest even size with the same aspect as w x h that fits in the box (turned for portrait)."""
    bw, bh = (int(x) for x in box.split("x"))
    if (h > w) != (bh > bw):
        bw, bh = bh, bw
    f = min(bw / w, bh / h)
    ow, oh = int(round(w * f)), int(round(h * f))
    return max(2, ow - ow % 2), max(2, oh - oh % 2)


def custom_size_args(width, height, scale):
    """Custom: width / height win (one side = aspect kept, both = exact size); else the scale."""
    a = []
    if int(width or 0) > 0:
        a += ["--nr-width", str(int(width))]
    if int(height or 0) > 0:
        a += ["--nr-height", str(int(height))]
    if not a and float(scale or 1.0) != 1.0:
        a += ["--nr-scale", str(float(scale))]
    return a


def size_args_image(image, size, width, height, scale):
    """--nr-width/--nr-height/--nr-scale for the CLI, from the size preset (the CLI has no --nr-fit)."""
    kind, val = SIZES[size]
    if kind == "scale":
        return ["--nr-scale", str(val)] if val != 1.0 else []
    if kind == "fit":
        from PIL import Image
        with Image.open(image) as im:
            w, h = im.size
        ow, oh = fit_box(w, h, val)
        return ["--nr-width", str(ow), "--nr-height", str(oh)]
    return custom_size_args(width, height, scale)


def size_args_video(size, width, height, scale):
    kind, val = SIZES[size]
    if kind == "scale":
        return ["--nr-scale", str(val)] if val != 1.0 else []
    if kind == "fit":
        return ["--nr-fit", val]
    return custom_size_args(width, height, scale)


BROWSER_PLAYABLE = {("mp4", "h264_nvenc"), ("mp4", "h264_cpu"), ("mp4", "av1_nvenc"), ("mp4", "av1_svt"),
                    ("webm", "av1_nvenc"), ("webm", "av1_svt")}


def make_preview(out, runner=None, nvenc=True, progress=None, destination=None):
    """An explicitly SDR H.264 display proxy; the high-precision master is untouched."""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return None
    prev = str(destination) if destination is not None else os.path.splitext(out)[0] + "_preview.mp4"
    from nr_video import probe, colour_tags
    from video_pipeline import sdr_proxy_filter
    info = probe(find_tool("ffprobe"), out)
    tone_map = sdr_proxy_filter(info, rgb_input=info.get("matrix") == "gbr")
    matrix, primaries, transfer = colour_tags(info)
    conversion = tone_map or f"zscale=pin={primaries}:tin={transfer}:min={'gbr' if info.get('matrix') == 'gbr' else matrix}:rin={'full' if info.get('range') == 'pc' else 'limited'}:p=bt709:t=bt709:m=bt709:r=limited"
    filters = [conversion, "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:flags=bicubic", "format=yuv420p", "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"]
    common = [ffmpeg, "-y", "-v", "error", "-i", out, "-map", f"0:v:{info.get('video_ordinal',0)}", "-map", "0:a:0?",
              "-vf", ",".join(filters), "-fps_mode", "passthrough", "-enc_time_base", "1/1000000",
              "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    tail = ["-c:a", "aac", "-b:a", "128k", "-video_track_timescale", "1000000", "-movflags", "+faststart", prev]
    encoders = ([["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "24", "-b:v", "0"]] if nvenc else [])
    encoders.append(["-c:v", "libopenh264", "-b:v", "8M", "-allow_skip_frames", "0"])
    for venc in encoders:
        command=common + venc + tail
        if runner and hasattr(runner,"stream_lines"):
            command=command[:1]+["-stats_period","1","-progress","pipe:1","-nostats"]+command[1:]
            process=runner.start(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",errors="replace",bufsize=1)
            values={};observed=[0,0,0];last_advance=time.monotonic()
            for line in runner.stream_lines(process,idle_timeout=900):
                if "=" in line:
                    key,value=line.split("=",1);values[key]=value
                    if key=="progress":
                        def integer(name):
                            try:return max(0,int(values.get(name,0)))
                            except (TypeError,ValueError):return 0
                        current=[integer("frame"),integer("out_time_us"),integer("total_size")]
                        if any(now>before for now,before in zip(current,observed)):
                            observed=[max(now,before) for now,before in zip(current,observed)]
                            last_advance=time.monotonic()
                            if progress:
                                percent=min(99,int(100*current[1]/1000000/max(.000001,info["duration"])))
                                progress({"phase":"preview","progress":percent,"message":f"正在生成浏览器预览… {percent}%",
                                          "preview_seconds":current[1]/1000000,"preview_frame":current[0]})
                if time.monotonic()-last_advance>900:
                    runner.cancel()
                    raise TimeoutError("浏览器预览长时间没有编码进展；增强母版与完成片段仍保留。")
            runner.check()
            code=process.wait()
        else:
            code=subprocess.run(command,capture_output=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)).returncode
        if code == 0:
            return prev
    return None


def _stream(proc):
    """Yield output lines, splitting on both \\r (progress bar) and \\n for live updates."""
    buf = ""
    while True:
        ch = proc.stdout.read(1)
        if not ch:
            break
        if ch in "\r\n":
            if buf.strip():
                yield buf
            buf = ""
        else:
            buf += ch
    if buf.strip():
        yield buf
