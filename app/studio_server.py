"""Local Studio API: recoverable jobs, bounded logs and cancellable render pipelines."""
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal, get_args

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, model_validator
from PIL import Image, ImageOps, UnidentifiedImageError
import render_engine as engine
import nr_video
import studio_storage
import studio_cache
import studio_diagnostics
import image_normalization
import studio_previews
from gpu_runtime import GpuRuntime
from process_runner import ProcessGroup, RenderCancelled

ROOT = Path(__file__).parent.resolve()
UPLOADS, OUTPUTS = ROOT / "uploads", ROOT / "ui_out"
STATE_FILE = ROOT / "studio-state.json"
UPLOADS.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)
@asynccontextmanager
async def lifespan(_api):
    shutdown_requested.clear()
    # Queued work is durable. A previously running video is deliberately paused
    # until the user resumes it, so a restart never silently starts a long export.
    with mutex:
        pending = [j for j in jobs.values() if j.get("status") == "queued" and j.get("queue_version") == 1]
        for job in sorted(pending, key=lambda j: (j.get("queued_at") or j.get("created_at") or 0)):
            submit_queued(job["id"])
        for asset in assets.values():
            if asset.get("preview_status") == "preparing":
                enqueue_source_preview(asset["id"])
    yield
    shutdown_requested.set()
    with mutex:
        for event in cancellation.values():
            event.set()
        for event in source_preview_cancellation.values():
            event.set()
        owned = list(runners.values()) + list(source_preview_runners.values())
    for group in owned:
        group.cancel()
    worker.shutdown(wait=False)
    preview_worker.shutdown(wait=False)


api = FastAPI(title="DLSS Studio", docs_url=None, redoc_url=None, lifespan=lifespan)
mutex = threading.RLock()
worker = ThreadPoolExecutor(max_workers=1)
preview_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="source-preview")
runners, cancellation = {}, {}
shutdown_requested = threading.Event()
uploading_ids = set()
cache_leases = set()
source_preview_queued, source_preview_runners, source_preview_cancellation = set(), {}, {}
ACTIVE = studio_storage.ACTIVE
RETAINED = studio_storage.RETAINED
try:
    assets, jobs = studio_storage.load(STATE_FILE, ROOT)
except (OSError, ValueError, KeyError, TypeError):
    logging.exception("Could not read Studio history; existing media files are retained")
    assets, jobs = {}, {}


def persist():
    # Callers hold mutex. Only lifecycle transitions hit disk, never each frame.
    studio_storage.save(STATE_FILE, ROOT, assets, jobs)


def update_job(jid, save=False, **values):
    with mutex:
        jobs[jid].update(values)
        if save:
            try:
                persist()
            except OSError:
                jobs[jid]["storage_warning"] = "任务记录暂时无法保存，请及时下载结果。"
                logging.exception("Could not persist job state")


def public_asset(item):
    return {key: value for key, value in item.items() if key != "path" and not key.endswith("_path")}


def update_asset(aid, save=False, **values):
    with mutex:
        if aid not in assets:
            return
        assets[aid].update(values)
        if save:
            try:
                persist()
            except OSError:
                assets[aid]["storage_warning"] = "显示预览记录暂时无法保存，原片仍保留。"


def source_preview_needed(info, suffix):
    if info.get("trc") in {"smpte2084", "arib-std-b67"}:
        return True
    codec = info.get("video_codec")
    if not codec:
        return False
    playable = (suffix in {".mp4", ".m4v"} and codec in {"h264", "av1"}) or \
               (suffix == ".webm" and codec in {"vp8", "vp9", "av1"})
    return not playable or codec == "h264" and info.get("bits", 8) > 8


def enqueue_source_preview(aid):
    with mutex:
        if aid in source_preview_queued or aid not in assets or shutdown_requested.is_set():
            return
        source_preview_queued.add(aid)
        source_preview_cancellation[aid] = threading.Event()
        try:
            preview_worker.submit(prepare_source_preview, aid)
        except RuntimeError:
            source_preview_queued.discard(aid)
            source_preview_cancellation.pop(aid, None)
            update_asset(aid, save=True, preview_status="failed", preview_url=None,
                         preview_message="显示预览服务已停止，重开工作台后可重试。")


def prepare_source_preview(aid):
    group = None
    try:
        with mutex:
            item = assets.get(aid)
            if not item or shutdown_requested.is_set():
                return
            item = dict(item)
            event = source_preview_cancellation.setdefault(aid, threading.Event())
        group = ProcessGroup(event, priority=0x4000 if os.name == "nt" else 0)
        with mutex:
            source_preview_runners[aid] = group
        update_asset(aid, save=True, preview_status="preparing", preview_progress=0,
                     preview_message="正在准备原片显示预览，仍可开始增强。")
        def progress(value):
            update_asset(aid, preview_progress=value.get("progress", 0),
                         preview_message="正在准备原片显示预览，仍可开始增强。")
        values = studio_previews.build(item, UPLOADS, group, NVENC_AVAILABLE, progress)
        group.check()
        update_asset(aid, save=True, **values)
    except RenderCancelled:
        if shutdown_requested.is_set():
            update_asset(aid, save=True, preview_status="preparing", preview_url=None,
                         preview_message="显示预览准备已暂停，重开后会继续。")
        else:
            update_asset(aid, save=True, preview_status="failed", preview_url=None,
                         preview_message="显示预览准备已取消，可以重试；原片仍保留。")
    except Exception as exc:
        update_asset(aid, save=True, preview_status="failed", preview_url=None,
                     preview_message="显示预览准备失败，原片仍可增强；可以重试。 " + str(exc)[-300:])
    finally:
        if group:
            group.close()
        with mutex:
            source_preview_queued.discard(aid)
            source_preview_runners.pop(aid, None)
            source_preview_cancellation.pop(aid, None)


def public_job(job):
    result = {key: value for key, value in job.items() if not key.endswith("_path")}
    for key, aid in (("asset", job.get("asset_id")), ("source_asset", job.get("source_asset_id"))):
        if aid in assets:
            result[key] = public_asset(assets[aid])
    queued = sorted((j for j in jobs.values() if j.get("status") == "queued"),
                    key=lambda j: (j.get("queued_at") or j.get("created_at") or 0))
    result["queue_position"] = next((i + 1 for i, queued_job in enumerate(queued) if queued_job["id"] == job["id"]), 0)
    result["resumable"] = bool(job.get("phase") != "environment_changed" and
                               (job.get("status") == "paused" or job.get("resume_supported") and
                                job.get("status") in {"error", "cancelled"}))
    if job.get("resume_supported"):
        try:
            result["coverage"] = studio_cache.checkpoint_coverage(studio_cache.owned_path(OUTPUTS, job["id"] + "/work"))["ranges"]
        except ValueError:
            result.update(coverage=[], resumable=False)
    return result


def check_nvenc(codec):
    try:
        return subprocess.run([engine.find_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
            "color=s=128x128:d=0.04", "-frames:v", "1", "-c:v", codec, "-f", "null", "-"],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


ENCODER_AVAILABLE = {codec: check_nvenc(codec) for codec in engine.CONTAINERS if codec.endswith("_nvenc")}
NVENC_AVAILABLE = ENCODER_AVAILABLE["h264_nvenc"]
WINDOWS_BUILD = engine.windows_build()
DEFAULT_CODEC = engine.default_video_codec(WINDOWS_BUILD, ENCODER_AVAILABLE)
GPU = GpuRuntime(ROOT)


@api.get("/api/health")
def health():
    with mutex:
        return {"app": "dlss-studio", "instance": hashlib.sha256(str(ROOT).lower().encode()).hexdigest(),
                "busy": any(j.get("status") in ACTIVE for j in jobs.values()),
                "queued": sum(j.get("status") == "queued" for j in jobs.values()),
                "paused": sum(j.get("status") == "paused" for j in jobs.values())}


OutputSize = Literal["原始尺寸（不放大）", "720p (1280×720)", "1080p (1920×1080)", "1440p (2560×1440)", "4K (3840×2160)"]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    asset_id: str
    request_id: str | None = Field(None, max_length=64)
    job_kind: Literal["full", "frame", "clip"] = "full"
    clip_start: float = Field(0, ge=0)
    clip_duration: float = Field(3, gt=0)
    source_asset_id: str | None = None
    frame_time: float | None = Field(None, ge=0)
    size: OutputSize = "原始尺寸（不放大）"
    style: str = "默认"
    preset: str = "默认"
    intensity: float = Field(1, ge=0, le=2)
    local_structure: float = Field(1, ge=0, le=2)
    local_tone: float = Field(1, ge=0, le=2)
    skin: float = Field(1, ge=0, le=2)
    global_tone: float = Field(1, ge=0, le=2)
    detail: float = Field(1, ge=0, le=2)
    color: float = Field(1, ge=0, le=1)
    ui_correction: bool = False
    auto_mask: bool = False
    hdr: bool = False
    stability: float = Field(0, ge=0, le=1)
    protect_region: list[float] = Field(default_factory=list, max_length=4)
    protect_skin: float = Field(0, ge=0, le=1)
    protect_highlights: float = Field(0, ge=0, le=1)
    render_scale: float = Field(1, ge=.5, le=1)
    motion: bool = True
    motion_vis: bool = False
    motion_engine: str = "auto"
    codec: str = DEFAULT_CODEC
    container: str = "mp4"
    cq: int = Field(19, ge=0, le=51)
    bitrate: int = Field(0, ge=0, le=1000000)
    bitrate_mode: Literal["auto", "manual"] = "auto"
    bit_depth: int = 8 if DEFAULT_CODEC.startswith("h264_") else 10
    enc_preset: str = "p5"
    audio: str = "auto"
    prores_profile: str = "hq"
    frames: int = Field(0, ge=0)
    segment_seconds: float = Field(10, ge=1, le=60)
    warmup_frames: int = Field(16, ge=0, le=120)
    preserve_media: bool = True
    timing_mode: Literal["preserve", "cfr"] = "preserve"
    diagnostic: bool = False

    @model_validator(mode="before")
    @classmethod
    def preserve_explicit_bitrate(cls, values):
        # Earlier saved versions and API clients have no bitrate_mode field.
        if isinstance(values, dict) and "bitrate" in values and "bitrate_mode" not in values:
            values = {**values, "bitrate_mode": "manual"}
        return values


def validate_settings(s):
    if s.protect_region:
        if len(s.protect_region) != 4 or any(not 0 <= v <= 1 for v in s.protect_region):
            raise HTTPException(400, "保护区域需要归一化的 x、y、宽度和高度。")
        x, y, width, height = s.protect_region
        if width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
            raise HTTPException(400, "保护区域超出画面范围。")
    for value, allowed in [(s.size, engine.SIZES), (s.style, engine.STYLES),
                           (s.preset, engine.PRESETS),
                           (s.codec, engine.CONTAINERS), (s.motion_engine, ["auto", "nvof", "lk"]),
                           (s.audio, engine.AUDIO), (s.prores_profile, engine.PRORES_PROFILES),
                           (s.enc_preset, [f"p{i}" for i in range(1, 8)]), (s.bit_depth, [8, 10])]:
        if value not in allowed:
            raise HTTPException(400, "参数无效，请重新选择设置。")
    if s.container not in engine.CONTAINERS[s.codec]:
        raise HTTPException(400, "所选编码与文件格式不兼容。")


def model_args(s, for_video=False, selection=None):
    args = engine.nr_model_args(s.style, s.preset, s.intensity, s.local_structure, s.local_tone,
        s.skin, s.global_tone, s.detail, s.color, s.ui_correction, s.auto_mask, s.hdr if not for_video else False)
    # The reviewed runtime selects the lighter B network for "Default" without
    # adding a UI control. Explicit legacy/API presets remain explicit.
    if engine.PRESETS[s.preset] == 0 and getattr(selection, "nr_preset", 0):
        args[args.index("--nr-preset") + 1] = str(selection.nr_preset)
    args += ["--nr-stability", str(s.stability), "--nr-protect-skin", str(s.protect_skin),
             "--nr-protect-highlights", str(s.protect_highlights), "--nr-render-scale", str(s.render_scale)]
    if s.protect_region:
        args += ["--nr-protect", ",".join(str(value) for value in s.protect_region)]
    return args


def build_command(s, item, folder, selection, render_key=None, stage_key=None):
    sr = selection.args()
    if item["kind"] == "image":
        return [engine.EXE, "--nr-run", "--in", item["path"], "--out", str(folder),
                "--nr-cache-dir", str(folder / "nr-cache")] + sr + model_args(s, selection=selection) + \
            engine.size_args_image(item["path"], s.size, 0, 0, 1)
    out = folder / ("result." + s.container)
    bitrate = engine.recommended_bitrate(s.size, item["width"], item["height"]) if s.bitrate_mode == "auto" else s.bitrate
    args = [os.sys.executable, engine.NRV, "--in", item["path"], "--out", str(out),
            "--nr-motion-engine", s.motion_engine, "--nr-motion", "1" if s.motion else "0",
            "--codec", s.codec, "--enc-preset", s.enc_preset, "--bit-depth", str(s.bit_depth),
            "--prores-profile", s.prores_profile, "--audio", s.audio, "--cq", str(s.cq),
            "--bitrate", str(bitrate)] + sr + model_args(s, for_video=True, selection=selection) + engine.size_args_video(s.size, 0, 0, 1)
    if s.motion_vis:
        args.append("--nr-motion-vis")
    if s.frames:
        args += ["--frames", str(s.frames)]
    if s.job_kind == "clip":
        args += ["--start", str(s.clip_start), "--duration", str(s.clip_duration)]
    args += ["--work-dir", str(folder / "work"), "--resume", "--pause-file", str(folder / "pause.request"),
             "--segment-seconds", str(s.segment_seconds), "--warmup-frames", str(s.warmup_frames),
             "--preserve-media", "1" if s.preserve_media else "0", "--timing", s.timing_mode]
    if render_key:
        args += ["--render-key", render_key]
    if stage_key:
        args += ["--nr-cache-key", stage_key]
    return args


@api.get("/api/options")
def options():
    return {"sizes": list(get_args(OutputSize)), "hardware": GPU.public(),
            "codecs": engine.CODECS, "containers": engine.CONTAINERS,
            "nvenc_available": NVENC_AVAILABLE, "encoder_available": ENCODER_AVAILABLE,
            "video_defaults": {"codec": DEFAULT_CODEC, "container": "mp4", "bitrate_mode": "auto",
                "windows_build": WINDOWS_BUILD,
                "bitrates": {size: engine.recommended_bitrate(size) for size in get_args(OutputSize)}}, "version": 2}


def remove_folder(path, parent):
    # Only direct, generated child directories are eligible; never follow a junction.
    path, parent = Path(path), Path(parent).resolve()
    resolved = path.resolve()
    if resolved.parent != parent or path.is_symlink() or (path.lstat().st_file_attributes & 0x400 if os.name == "nt" else False):
        raise ValueError("拒绝清理缓存目录之外的文件。")
    shutil.rmtree(path)


@api.post("/api/upload")
def upload(file: UploadFile = File(...)):
    # A sync route runs in FastAPI's thread pool. Copying, PIL and ffprobe must not
    # block the event loop serving status, cancellation and video range requests.
    name = Path(file.filename or "素材").name
    suffix = Path(name).suffix.lower()
    image_ext = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
    video_ext = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
    aid = uuid.uuid4().hex
    folder = UPLOADS / aid
    try:
        if suffix not in image_ext | video_ext:
            raise HTTPException(400, "暂不支持此格式，请上传常见图片或视频文件。")
        with mutex:
            uploading_ids.add(aid)
            folder.mkdir()
        path = folder / ("source" + suffix)
        with path.open("wb") as target:
            shutil.copyfileobj(file.file, target, 1024 * 1024)
        image_metadata = {}
        if suffix in image_ext:
            normalized = folder / "normalized.png"
            image_metadata = image_normalization.normalize(path, normalized, engine.find_tool("ffmpeg"))
            w, h = image_metadata["width"], image_metadata["height"]
            target = folder / "source.png"
            normalized.replace(target)
            if path != target:
                path.unlink()
            path, kind = target, "image"
        else:
            info = nr_video.probe(engine.find_tool("ffprobe"), str(path))
            w, h, kind = info["w"], info["h"], "video"
        item = {"id": aid, "name": name, "kind": kind, "width": w, "height": h,
                "bytes": path.stat().st_size, "path": str(path), "url": f"/api/assets/{aid}",
                "created_at": time.time()}
        item.update(image_metadata)
        if kind == "video":
            item.update(duration=info.get("duration", 0), fps=info["fps"],
                        transfer=info.get("trc", "unknown"), color_primaries=info.get("primaries", "unknown"),
                        variable_frame_rate=info.get("vfr", False), video_codec=info.get("video_codec"),
                        frame_count=info.get("frames", 0),
                        preview_status="preparing" if source_preview_needed(info, suffix) else "not_needed",
                        preview_url=None)
        studio_cache.source_fingerprint(item)
        with mutex:
            assets[aid] = item
            try:
                persist()
            except Exception:
                assets.pop(aid, None)
                raise
            if item.get("preview_status") == "preparing":
                enqueue_source_preview(aid)
            return public_asset(item)
    except HTTPException:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, "图片无法读取或尺寸过大，请检查文件或缩小尺寸后上传。") from exc
    except OSError as exc:
        raise HTTPException(507, "素材无法保存，请检查磁盘空间和文件夹写入权限。") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc) or "无法读取这个文件，请检查文件是否完整。") from exc
    except (KeyError, IndexError, subprocess.SubprocessError) as exc:
        raise HTTPException(400, "无法读取这个文件，请检查文件是否完整或先转换格式。") from exc
    finally:
        file.file.close()
        with mutex:
            uploading_ids.discard(aid)
            if aid not in assets and folder.exists():
                try:
                    remove_folder(folder, UPLOADS)
                except OSError:
                    logging.exception("Could not remove failed upload")


@api.get("/api/assets/{aid}/info")
def asset_info(aid: str):
    with mutex:
        if aid not in assets:
            raise HTTPException(404, "素材已失效，请重新上传。")
        item = assets[aid]
        legacy_video = item["kind"] == "video" and "preview_status" not in item
        path = item["path"]
    if legacy_video:
        try:
            info = nr_video.probe(engine.find_tool("ffprobe"), path)
            needed = source_preview_needed(info, Path(path).suffix.lower())
            update_asset(aid, save=True, transfer=info.get("trc", "unknown"), video_codec=info.get("video_codec"),
                         preview_status="preparing" if needed else "not_needed", preview_url=None)
            if needed:
                enqueue_source_preview(aid)
        except Exception:
            update_asset(aid, save=True, preview_status="failed", preview_url=None,
                         preview_message="无法检查原片显示格式，可重试准备显示预览。")
    with mutex:
        if aid not in assets:
            raise HTTPException(404, "素材已失效，请重新上传。")
        return public_asset(assets[aid])


@api.get("/api/assets/{aid}/preview")
def source_preview_file(aid: str):
    with mutex:
        item = assets.get(aid)
        if not item or item.get("preview_status") != "ready":
            raise HTTPException(404, "原片显示预览尚未准备好。")
        try:
            path = studio_cache.owned_path(UPLOADS, aid + "/source_preview.mp4")
        except ValueError as exc:
            raise HTTPException(404, "原片显示预览已失效。") from exc
        if not path.is_file():
            raise HTTPException(404, "原片显示预览已失效。")
        return FileResponse(path, media_type="video/mp4")


@api.post("/api/assets/{aid}/preview")
def retry_source_preview(aid: str):
    with mutex:
        item = assets.get(aid)
        if not item or item.get("kind") != "video" or not Path(item["path"]).is_file():
            raise HTTPException(404, "视频素材已失效，请重新上传。")
        if aid not in source_preview_queued:
            update_asset(aid, save=True, preview_status="preparing", preview_url=None, preview_progress=0,
                         preview_message="已加入原片显示预览准备队列。")
            enqueue_source_preview(aid)
        return public_asset(item)


@api.get("/api/assets/{aid}")
def asset_file(aid: str):
    with mutex:
        item = assets.get(aid)
        if not item or not Path(item["path"]).is_file():
            raise HTTPException(404, "素材已失效，请重新上传。")
        return FileResponse(item["path"])


def progress_values(line):
    if line.startswith("NRINFO "):
        try:
            value = json.loads(line[7:])
            if value.get("phase") == "source_hash":
                done, total = max(0, int(value.get("bytes") or 0)), max(0, int(value.get("total_bytes") or 0))
                amount = f"{done / 1048576:.1f} / {total / 1048576:.1f} MiB" if total else f"{done / 1048576:.1f} MiB"
                return {"phase": "analyzing", "progress": None, "message": "正在校验源视频 · " + amount,
                        "source_bytes_checked": done, "source_total_bytes": total, "eta": None}
            if value.get("phase") == "timeline_scan":
                frames = max(0, int(value.get("frames_scanned") or 0))
                return {"phase": "analyzing", "progress": None,
                        "message": f"正在读取源帧时间戳 · 已扫描 {frames} 帧", "source_frames_scanned": frames, "eta": None}
        except (ValueError, TypeError, OverflowError):
            return {}
    if line.startswith("NRJOB "):
        try:
            value = json.loads(line[6:])
            frame, total = int(value.get("frame", 0)), int(value.get("total_frames", 0))
            return {"phase": "encoding" if value.get("phase") == "encoding" else "render",
                    "message": str(value.get("message") or "正在增强视频")[:160],
                    "frame": max(0, frame), "total_frames": max(0, total),
                    "progress": max(0, min(99, int(value.get("progress", frame * 100 / total if total else 0)))),
                    "fps": max(0, float(value.get("fps", 0))), "eta": str(value.get("eta") or "--:--")[:24]}
        except (ValueError, TypeError, OverflowError):
            return {}
    match = re.search(r"(\d+)\s*/\s*(\d+)\s*\(\s*(\d+)%\)\s*([\d.]+) fps\s+ETA ([\d:]+|--:--)", line)
    if match:
        frame, total, percent, fps, eta = match.groups()
        return dict(phase="render", message="正在增强视频", progress=min(99, int(percent)),
                    frame=int(frame), total_frames=int(total), fps=float(fps), eta=eta)
    return {}


def runtime_identity(selection):
    identity = getattr(GPU, "identity", None)
    if callable(identity):
        value = identity(selection)
        for _ in range(4):
            if value.get("cacheable", True):
                break
            time.sleep(.15)
            value = identity(selection)
        return value
    # Transitional or older runtime adapters still get a conservative identity.
    return {"schema": 1, "gpu": {"luid": selection.luid, "profile": selection.profile},
            "runtime": {"sha256": selection.sha256}, "pipeline": studio_cache.CACHE_SCHEMA,
            "renderer": studio_cache.digest_file(engine.EXE) if Path(engine.EXE).is_file() else "missing"}


def submit_queued(jid):
    """Caller owns mutex; the executor is serial and each durable job is checked again."""
    cancellation[jid] = threading.Event()
    try:
        worker.submit(run_queued, jid)
    except RuntimeError as exc:
        cancellation.pop(jid, None)
        update_job(jid, save=True, status="error", phase="error", message="任务服务已停止，请重新打开程序。")
        raise HTTPException(503, "任务服务已停止，请重新打开程序。") from exc


def run_queued(jid):
    with mutex:
        job = jobs.get(jid)
        if not job or job.get("status") not in {"queued", "cancelling"} or shutdown_requested.is_set():
            return
        item = assets.get(job.get("asset_id"))
        if not item or not Path(item["path"]).is_file():
            update_job(jid, save=True, status="error", phase="missing", message="源素材已失效，无法继续任务。")
            return
        settings = Settings(**job["settings"])
        item = dict(item)
    try:
        selection = GPU.prepare()
        identity = runtime_identity(selection)
        key, nr_key = studio_cache.render_keys(settings, item, identity)
        with mutex:
            # Resuming under different weights/drivers must not mix old temporal
            # segments with new ones. Keep the old checkpoint available to inspect.
            old_key = jobs[jid].get("nr_key")
            checkpoint = OUTPUTS / jid / "work" / "checkpoint.json"
            if checkpoint.exists() and not identity.get("cacheable", True):
                update_job(jid, save=True, status="paused", phase="runtime_unstable",
                           message="模型状态正在更新，已保留片段；状态稳定后可继续。", resumable=True)
                return
            if old_key and old_key != nr_key and checkpoint.exists():
                if not jobs[jid].get("runtime", {}).get("cacheable", True):
                    # The pipeline's signed checkpoint will reject the unknown
                    # old key and recompute it; never combine it with new frames.
                    update_job(jid, message="模型身份已稳定，无法确认的旧片段将重新计算。")
                else:
                    update_job(jid, save=True, status="paused", phase="environment_changed",
                               message="运行库、驱动或素材已变化；请创建新任务，旧片段仍保留。", resumable=False)
                    return
            jobs[jid].update(runtime=identity, cache_key=key, nr_key=nr_key)
            jobs[jid]["coverage_key"] = studio_cache.processing_key(settings, item, identity)
            jobs[jid]["stage_key"] = studio_cache.model_stage_key(settings, item, identity)
        render(jid, settings, item, selection)
    except Exception as exc:
        update_job(jid, save=True, status="error", phase="error", message=str(exc), finished_at=time.time())


def completed_match(key, exclude=None):
    for candidate in sorted(jobs.values(), key=lambda j: (j.get("finished_at") or 0), reverse=True):
        if candidate["id"] != exclude and candidate.get("runtime", {}).get("cacheable", True) \
                and candidate.get("cache_key") == key and studio_cache.valid_result(candidate):
            return candidate
    return None


def image_stage_match(nr_key, exclude=None):
    for candidate in sorted(jobs.values(), key=lambda j: (j.get("finished_at") or 0), reverse=True):
        stage = Path(candidate.get("stage_path") or "")
        try:
            if candidate["id"] != exclude and candidate.get("runtime", {}).get("cacheable", True) \
                    and candidate.get("status") == "done" and candidate.get("nr_key") == nr_key \
                    and stage.is_file() and studio_cache.regular(stage) and stage.stat().st_size <= 256 * 1024 * 1024 \
                    and studio_cache.digest_file(stage) == candidate.get("stage_sha256"):
                return candidate
        except OSError:
            continue
    return None


def trim_stage_cache():
    """Intermediate cache is expendable; finished result files never are."""
    stages = []
    for job in jobs.values():
        path = Path(job.get("stage_path") or "")
        if path.is_file() and path.resolve().is_relative_to(OUTPUTS.resolve()):
            stages.append((job, path, path.stat().st_size))
    total = sum(entry[2] for entry in stages)
    for job, path, size in sorted(stages, key=lambda entry: (bool(entry[0].get("favorite")), (entry[0].get("finished_at") or 0))):
        if total <= 1024 * 1024 * 1024:
            break
        if job.get("status") in RETAINED:
            continue
        try:
            path.unlink()
            job.pop("stage_path", None)
            job.pop("stage_sha256", None)
            total -= size
        except OSError:
            pass


def trim_video_model_cache():
    # Raw predictions are optional acceleration, separate from the committed
    # lossless segments required to resume a long export.
    entries = []
    for job in jobs.values():
        if job.get("status") in ACTIVE or job["id"] in cache_leases:
            continue
        try:
            root = studio_cache.owned_path(OUTPUTS, job["id"] + "/work/nr")
        except ValueError:
            continue
        if not root.is_dir() or not studio_cache.regular(root):
            continue
        for folder in root.iterdir():
            if folder.is_dir() and studio_cache.regular(folder) and folder.resolve().parent == root.resolve():
                entries.append((job, folder, folder_bytes(folder)))
    total = sum(value[2] for value in entries)
    for job, folder, size in sorted(entries, key=lambda value: (bool(value[0].get("favorite")), (value[0].get("created_at") or 0))):
        if total <= 2 * 1024 * 1024 * 1024:
            break
        try:
            expected = (OUTPUTS / job["id"] / "work" / "nr").resolve()
            if folder.resolve().parent != expected:
                continue
            shutil.rmtree(folder)
            total -= size
        except OSError:
            pass


def render(jid, s, item, selection):
    with mutex:
        if jobs[jid].get("status") in {"paused", "done", "cancelled"}:
            return
        if jobs[jid].get("status") == "queued":
            update_job(jid, save=True, status="running", phase="initializing", message="正在准备处理…")
    group = None
    event = cancellation.setdefault(jid, threading.Event())
    started = time.monotonic()
    folder = OUTPUTS / jid
    try:
        folder = studio_cache.owned_path(OUTPUTS, jid)
        studio_cache.owned_path(OUTPUTS, jid + "/work")
        studio_cache.owned_path(OUTPUTS, jid + "/nr-cache")
        if event.is_set():
            raise RenderCancelled()
        folder.mkdir(exist_ok=True)
        identity = jobs[jid].get("runtime") or runtime_identity(selection)
        cache_key, nr_key = studio_cache.render_keys(s, item, identity)
        stage_key = studio_cache.model_stage_key(s, item, identity)
        cache_safe = identity.get("cacheable", True)
        update_job(jid, runtime=identity, cache_key=cache_key, nr_key=nr_key,
                   coverage_key=studio_cache.processing_key(s, item, identity),
                   stage_key=stage_key,
                   cache_safe=cache_safe,
                   started_at=time.time(), work_path=str(folder / "work") if item["kind"] == "video" else None)
        with mutex:
            cached = None if s.diagnostic or not cache_safe else completed_match(cache_key, jid)
            if cached:
                paths = studio_cache.clone_result(cached, folder)
                stat = Path(paths["result_path"]).stat()
                timing = {}
                if item["kind"] == "video":
                    actual_start = next((value for value in (cached.get("actual_start"),
                        cached.get("clip_start"), jobs[jid].get("clip_start")) if value is not None), 0)
                    preview_offset = cached.get("preview_offset")
                    timing = {"actual_start": actual_start,
                              "preview_offset": actual_start if preview_offset is None else preview_offset}
                    studio_cache.clone_timeline(OUTPUTS, cached["id"], jid)
                update_job(jid, save=True, status="done", phase="done", progress=100, eta=None,
                    message="已复用相同设置的结果。", cache_hit=True, cache_mode="exact", reused_from=cached["id"],
                    **paths, **timing, result_sha256=cached.get("result_sha256") or studio_cache.digest_file(paths["result_path"]),
                    result_stat=[stat.st_size, stat.st_mtime_ns], width=cached.get("width"), height=cached.get("height"),
                    output_profile=cached.get("output_profile", {}),
                    finished_at=time.time(), download=f"/api/jobs/{jid}/download",
                    preview=f"/api/jobs/{jid}/preview" if paths.get("preview_path") else None,
                    thumbnail=f"/api/jobs/{jid}/thumbnail" if paths.get("thumbnail_path") else None,
                    metrics={"cache_lookup_ms": round((time.monotonic() - started) * 1000, 3), "nr_evaluated_frames": 0}, hardware=GPU.public())
                return
        stage_source, stage_target = None, None
        if item["kind"] == "image" and not s.diagnostic and cache_safe:
            with mutex:
                stage_source = image_stage_match(nr_key, jid)
                if stage_source:
                    try:
                        stage_target = folder / "nr-cache" / Path(stage_source["stage_path"]).name
                        studio_cache.link_or_copy(stage_source["stage_path"], stage_target)
                    except (OSError, ValueError):
                        stage_source, stage_target = None, None
        if item["kind"] == "video" and not s.diagnostic and cache_safe and not (folder / "work" / "checkpoint.json").exists():
            with mutex:
                candidates = [j for j in jobs.values() if j["id"] != jid and j.get("nr_key") == nr_key and j.get("cache_safe", True)]
            for previous in sorted(candidates, key=lambda j: (j.get("created_at") or 0), reverse=True):
                with mutex:
                    if previous["id"] not in jobs:
                        continue
                    cache_leases.add(previous["id"])
                try:
                    if studio_cache.clone_work(OUTPUTS / previous["id"] / "work", folder / "work", nr_key):
                        update_job(jid, reused_segments_from=previous["id"])
                        break
                finally:
                    with mutex:
                        cache_leases.discard(previous["id"])
            if not (folder / "work" / "nr").exists():
                with mutex:
                    neural_candidates = [j for j in jobs.values() if j["id"] != jid and j.get("stage_key") == stage_key and j.get("cache_safe", True)]
                for previous in sorted(neural_candidates, key=lambda j: (j.get("created_at") or 0), reverse=True):
                    with mutex:
                        if previous["id"] not in jobs:
                            continue
                        cache_leases.add(previous["id"])
                    try:
                        if studio_cache.clone_neural_cache(OUTPUTS / previous["id"] / "work", folder / "work"):
                            update_job(jid, reused_model_from=previous["id"])
                            break
                    finally:
                        with mutex:
                            cache_leases.discard(previous["id"])
        with mutex:
            pausing = jobs[jid].get("status") == "pausing"
            update_job(jid, save=True, status="pausing" if pausing else "running",
                       phase="pausing" if pausing else "initializing", progress=None,
                       message="正在完成当前片段并暂停…" if pausing else
                               "正在复用增强结果并合成…" if stage_source else "正在初始化渲染引擎…")
        group = ProcessGroup(event)
        with mutex:
            runners[jid] = group
        if stage_source:
            command = [engine.EXE, "--nr-recompose", str(stage_target), "--out", str(folder)] + model_args(s)
            update_job(jid, stage_cache_hit=True, cache_mode="composite", reused_from=stage_source["id"])
        else:
            command = build_command(s, item, folder, selection, nr_key, stage_key)
        render_started = time.monotonic()
        proc = group.start(command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace", bufsize=1)
        lines = deque([f"DLSS 5 · {selection.name} · {selection.profile} · DXGI {selection.adapter}",
                       f"NR DLL SHA-256: {selection.sha256}", f"Runtime identity: {identity.get('digest', nr_key)}"], maxlen=60)
        stream = group.stream_lines(proc, idle_timeout=900) if hasattr(group, "stream_lines") else engine._stream(proc)
        for line in stream:
            group.check()
            lines.append(line)
            values = progress_values(line)
            # A pause request remains visible while the current segment commits.
            if jobs[jid].get("status") == "pausing":
                values.pop("message", None)
            update_job(jid, log="\n".join(lines)[-6000:], **values)
        code = proc.wait()
        group.check()
        if code == 75 and item["kind"] == "video":
            coverage = studio_cache.checkpoint_coverage(folder / "work")
            total = int(coverage.get("total_frames") or jobs[jid].get("total_frames") or 0)
            committed = sum(int(span.get("frames") or 0) for span in coverage["ranges"])
            paused_progress = max(0, min(99, int(committed * 100 / total))) if total else 0
            update_job(jid, save=True, status="paused", phase="paused", message="已暂停，继续后会复用已完成片段。",
                       resumable=True, progress=paused_progress, paused_at=time.time(), metrics=coverage.get("metrics", {}))
            return
        if code:
            log = jobs[jid].get("log", "")
            if any(marker in log for marker in ("OutOfDate", "0xBAD0000C", "BAD0000C")):
                raise RuntimeError("显卡驱动与 DLSS 5 组件未能完成初始化，请检查驱动和处理日志。")
            if "required nvenc API version" in log or "minimum required Nvidia driver" in log:
                raise RuntimeError("当前驱动不支持此编码，请选择 CPU 编码后重试。")
            raise RuntimeError("处理未完成，请展开处理日志查看原因。")
        files = list(folder.glob("*_nr.png")) if item["kind"] == "image" else [folder / ("result." + s.container)]
        if not files or not files[0].is_file() or files[0].stat().st_size == 0:
            raise RuntimeError("渲染引擎未生成结果文件，请查看处理日志。")
        render_seconds = time.monotonic() - render_started
        result = preview = files[0]
        preview_started = time.monotonic()
        stage_values = {}
        metrics = {}
        if item["kind"] == "image":
            with Image.open(result) as image:
                width, height = image.size
            stage = next(iter((folder / "nr-cache").glob("*.nrstage")), None)
            if stage and stage.stat().st_size <= 256 * 1024 * 1024:
                stage_values = {"stage_path": str(stage), "stage_sha256": studio_cache.digest_file(stage)}
            elif stage:
                stage.unlink()
        else:
            info = nr_video.probe(engine.find_tool("ffprobe"), str(result))
            width, height = info["w"], info["h"]
            metrics = studio_cache.checkpoint_coverage(folder / "work").get("metrics", {})
            if s.diagnostic:
                actual_frames = int(info.get("frames", 0) or metrics.get("frames", 0))
                evaluated_frames = int(metrics.get("nr_evaluated_frames") or 0)
                if actual_frames < 2 or evaluated_frames < 2 or s.motion_vis:
                    raise RuntimeError("连续视频自检未完成足够的实际 NR 评估，请重新运行自检。")
                stage_values["validation"] = {"continuous_render": True, "frames": actual_frames,
                                               "nr_evaluated_frames": evaluated_frames,
                                               "width": width, "height": height, "passed": True}
            try:
                checkpoint = json.loads((folder / "work" / "checkpoint.json").read_text(encoding="utf-8"))
                stage_values.update(actual_start=checkpoint.get("actual_start", checkpoint.get("source_start", jobs[jid].get("clip_start", 0))),
                                    preview_offset=checkpoint.get("actual_start", checkpoint.get("source_start", jobs[jid].get("clip_start", 0))),
                                    output_profile=checkpoint.get("output", {}))
            except (OSError, ValueError):
                pass
            if (s.container, s.codec) not in engine.BROWSER_PLAYABLE or info.get("trc") in {"smpte2084", "arib-std-b67"}:
                update_job(jid, phase="preview", progress=None, message="增强已完成，正在生成预览…", eta=None)
                preview = engine.make_preview(str(result), runner=group, nvenc=NVENC_AVAILABLE,
                                              progress=lambda values: update_job(jid, **values))
        thumbnail = result if item["kind"] == "image" else None
        if item["kind"] == "video":
            thumbnail = folder / "thumbnail.jpg"
            thumb_proc = group.start([engine.find_tool("ffmpeg"), "-v", "error", "-i", str(preview or result),
                "-frames:v", "1", "-vf", "scale=160:-2", str(thumbnail)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            thumb_proc.wait()
            group.check()
            if not thumbnail.is_file():
                thumbnail = None
        stat = result.stat()
        metrics.setdefault("render_wall_ms", round(render_seconds * 1000, 3))
        metrics.update(pipeline_wall_ms=round(render_seconds * 1000, 3),
                       preview_wall_ms=round((time.monotonic() - preview_started) * 1000, 3),
                       task_wall_ms=round((time.monotonic() - started) * 1000, 3))
        integrity_started = time.monotonic()
        result_hash = studio_cache.digest_file(result, check=group.check)
        metrics["integrity_ms"] = round((time.monotonic() - integrity_started) * 1000, 3)
        metrics["task_wall_ms"] = round((time.monotonic() - started) * 1000, 3)
        with mutex:
            group.check()
            evaluated = int(metrics.get("nr_evaluated_frames") or 0)
            if item["kind"] == "image" and not stage_source or item["kind"] == "video" and evaluated > 0 and not s.motion_vis:
                GPU.mark_verified(selection)
            if item["kind"] == "video" and evaluated >= 2 and not s.motion_vis:
                continuous = getattr(GPU, "mark_continuous_verified", None)
                if callable(continuous):
                    continuous(selection)
            update_job(jid, save=True, status="done", phase="done", progress=100, eta=None,
                message="已复用增强阶段并生成新结果。" if stage_source else "处理完成，可以查看并下载结果。",
                result_path=str(result), width=width, height=height, **stage_values,
                result_sha256=result_hash, result_stat=[stat.st_size, stat.st_mtime_ns],
                preview_path=str(preview) if preview else None, finished_at=time.time(), metrics=metrics,
                thumbnail_path=str(thumbnail) if thumbnail else None,
                thumbnail=f"/api/jobs/{jid}/thumbnail" if thumbnail else None,
                download=f"/api/jobs/{jid}/download", preview=f"/api/jobs/{jid}/preview" if preview else None,
                hardware=GPU.public())
            trim_stage_cache()
            trim_video_model_cache()
    except RenderCancelled:
        if shutdown_requested.is_set() and jobs[jid].get("resume_supported"):
            update_job(jid, save=True, status="paused", phase="paused", message="服务已退出，继续后会复用已完成片段。", resumable=True)
        else:
            update_job(jid, save=True, status="cancelled", phase="cancelled", message="已取消处理。", finished_at=time.time())
    except Exception as exc:
        message = str(exc) if isinstance(exc, TimeoutError) else \
                  "无法写入处理文件，请检查磁盘空间和文件夹权限。" if isinstance(exc, OSError) else str(exc)
        update_job(jid, save=True, status="error", phase="error", message=message, finished_at=time.time())
    finally:
        try:
            if group:
                group.close()
        finally:
            with mutex:
                runners.pop(jid, None)
                cancellation.pop(jid, None)


@api.post("/api/jobs")
def start_job(s: Settings):
    validate_settings(s)
    with mutex:
        if shutdown_requested.is_set():
            raise HTTPException(503, "任务服务正在退出，请重新打开程序。")
        item = assets.get(s.asset_id)
        if item and item["kind"] == "video" and s.bitrate_mode == "auto":
            s.bitrate = engine.recommended_bitrate(s.size, item["width"], item["height"])
        if s.request_id:
            previous = next((j for j in jobs.values() if j.get("request_id") == s.request_id), None)
            if previous:
                if previous.get("settings") != s.model_dump():
                    raise HTTPException(409, "重复请求的参数不一致，请重新提交。")
                return {"id": previous["id"]}
        if sum(j.get("status") == "queued" for j in jobs.values()) >= 64:
            raise HTTPException(429, "等待队列已满，请等待部分任务完成后再添加。")
        item = assets.get(s.asset_id)
        if not item or not Path(item["path"]).is_file():
            raise HTTPException(400, "请先上传素材。")
        if s.job_kind == "frame":
            source = assets.get(s.source_asset_id)
            if item["kind"] != "image" or not source or source["kind"] != "video" or s.frame_time is None:
                raise HTTPException(400, "单帧测试缺少原视频或时间点，请重新测试。")
        if s.job_kind == "clip":
            if item["kind"] != "video" or s.frames:
                raise HTTPException(400, "短片段需要视频素材，不能同时限制帧数。")
            duration = item.get("duration") or nr_video.probe(engine.find_tool("ffprobe"), item["path"])["duration"]
            if s.clip_start >= duration or s.clip_start + s.clip_duration > duration + .001:
                raise HTTPException(400, "片段超出视频范围，请调整起点或时长。")
        try:
            selection = GPU.prepare()
            identity = runtime_identity(selection)
            cache_key, nr_key = studio_cache.render_keys(s, item, identity)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        jid = uuid.uuid4().hex
        jobs[jid] = {"id": jid, "status": "queued", "phase": "queued", "message": "准备开始处理…", "log": "",
            "request_id": s.request_id, "asset_id": s.asset_id, "source_asset_id": s.source_asset_id,
            "job_kind": s.job_kind, "frame_time": s.frame_time, "clip_start": s.clip_start if s.job_kind == "clip" else 0,
            "settings": s.model_dump(), "created_at": time.time(), "queued_at": time.time(),
            "queue_version": 1, "resume_supported": item["kind"] == "video", "runtime": identity,
            "cache_key": cache_key, "nr_key": nr_key, "cache_hit": False,
            "coverage_key": studio_cache.processing_key(s, item, identity),
            "stage_key": studio_cache.model_stage_key(s, item, identity),
            "diagnostic": s.diagnostic, "name": "连续视频自检" if s.diagnostic else "",
            "work_path": str(OUTPUTS / jid / "work") if item["kind"] == "video" else None}
        try:
            persist()
        except OSError as exc:
            jobs.pop(jid)
            raise HTTPException(507, "任务无法保存，请检查磁盘空间和文件夹权限。") from exc
        submit_queued(jid)
        return {"id": jid}


@api.get("/api/diagnostics")
def diagnostics():
    try:
        selection = GPU.prepare()
        identity = runtime_identity(selection)
    except RuntimeError:
        identity = None
    with mutex:
        return studio_diagnostics.snapshot(GPU.public(), identity, jobs)


@api.post("/api/diagnostics/self-test")
def diagnostic_self_test():
    aid = uuid.uuid4().hex
    folder = UPLOADS / aid
    with mutex:
        uploading_ids.add(aid)
    try:
        GPU.prepare()
        item = studio_diagnostics.create_self_test_asset(folder, engine.find_tool("ffmpeg"),
            lambda path: nr_video.probe(engine.find_tool("ffprobe"), str(path)))
        with mutex:
            assets[aid] = item
            persist()
        return start_job(Settings(asset_id=aid, diagnostic=True, codec="h264_cpu", container="mp4", bit_depth=8,
                                  bitrate_mode="manual", bitrate=1500, warmup_frames=0))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise HTTPException(503, str(exc)) from exc
    finally:
        with mutex:
            uploading_ids.discard(aid)
            if aid not in assets and folder.exists():
                remove_folder(folder, UPLOADS)


@api.get("/api/jobs")
def recent_jobs(offset: int = 0, limit: int = 20, source: str | None = None):
    with mutex:
        ordered = sorted(jobs.values(), key=lambda j: (j.get("created_at") or 0), reverse=True)
        if source:
            ordered = [j for j in ordered if (j.get("source_asset_id") or j.get("asset_id")) == source]
        return {"jobs": [public_job(j) for j in ordered[max(0, offset):max(0, offset)+max(1, min(limit, 50))]],
                "total": len(ordered), "active": [public_job(j) for j in ordered if j["status"] in ACTIVE]}


@api.get("/api/history/sources")
def history_sources():
    with mutex:
        groups = {}
        for job in sorted(jobs.values(), key=lambda j: (j.get("created_at") or 0), reverse=True):
            aid = job.get("source_asset_id") or job.get("asset_id")
            if aid not in groups:
                groups[aid] = {"id": aid, "name": assets.get(aid, {}).get("name", "素材已清理"), "count": 0}
            groups[aid]["count"] += 1
        return {"sources": list(groups.values())}


class JobLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=80)
    favorite: bool


@api.patch("/api/jobs/{jid}")
def label_job(jid: str, label: JobLabel):
    with mutex:
        if jid not in jobs:
            raise HTTPException(404, "任务不存在，可能已被清理。")
        previous = dict(jobs[jid])
        jobs[jid].update(name=label.name.strip(), favorite=label.favorite)
        try:
            persist()
        except OSError as exc:
            jobs[jid] = previous
            raise HTTPException(507, "版本标记无法保存，请检查磁盘空间。") from exc
        return public_job(jobs[jid])


@api.get("/api/jobs/{jid}")
def job_status(jid: str):
    with mutex:
        if jid not in jobs:
            raise HTTPException(404, "任务不存在，可能已被清理。")
        return public_job(jobs[jid])


@api.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    with mutex:
        if jid not in jobs:
            raise HTTPException(404, "任务不存在。")
        if jobs[jid]["status"] not in RETAINED:
            return public_job(jobs[jid])
        cancellation.setdefault(jid, threading.Event()).set()
        immediate = jobs[jid]["status"] in {"queued", "paused"}
        update_job(jid, save=True, status="cancelled" if immediate else "cancelling",
                   phase="cancelled" if immediate else "cancelling",
                   message="已取消处理。" if immediate else "正在取消处理…",
                   **({"finished_at": time.time()} if immediate else {}))
        group = runners.get(jid)
    if group:
        group.cancel()
    return job_status(jid)


@api.post("/api/jobs/{jid}/pause")
def pause_job(jid: str):
    with mutex:
        job = jobs.get(jid)
        if not job:
            raise HTTPException(404, "任务不存在。")
        if job["status"] not in {"queued", "running", "pausing"}:
            return public_job(job)
        if job["status"] == "queued":
            update_job(jid, save=True, status="paused", phase="paused", message="已暂停等待任务。", paused_at=time.time())
            return public_job(job)
        if not job.get("resume_supported"):
            raise HTTPException(409, "图片任务不能在处理中暂停，可取消后重新处理。")
        try:
            folder = studio_cache.owned_path(OUTPUTS, jid)
            folder.mkdir(exist_ok=True)
            (folder / "pause.request").write_text("pause", encoding="ascii")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(507, "暂停请求无法保存，请检查磁盘空间。") from exc
        update_job(jid, save=True, status="pausing", phase="pausing", message="正在完成当前片段并暂停…")
        return public_job(job)


@api.post("/api/jobs/{jid}/resume")
def resume_job(jid: str):
    with mutex:
        job = jobs.get(jid)
        if not job:
            raise HTTPException(404, "任务不存在。")
        if job.get("phase") == "environment_changed":
            raise HTTPException(409, job.get("message"))
        if job["status"] in ACTIVE or job["status"] == "done":
            return public_job(job)
        if job["status"] != "paused" and not job.get("resume_supported"):
            raise HTTPException(409, "此任务需要重新开始。")
        if job.get("asset_id") not in assets or not Path(assets[job["asset_id"]]["path"]).is_file():
            raise HTTPException(400, "源素材已失效，请重新上传。")
        if sum(j.get("status") == "queued" for j in jobs.values()) >= 64:
            raise HTTPException(429, "等待队列已满，请稍后继续。")
        try:
            marker = studio_cache.owned_path(OUTPUTS, jid + "/pause.request")
            studio_cache.owned_path(OUTPUTS, jid + "/work")
            marker.unlink(missing_ok=True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        update_job(jid, save=True, status="queued", phase="queued", message="已加入队列，继续时会复用已完成片段。",
                   queued_at=time.time(), finished_at=None)
        submit_queued(jid)
        return public_job(job)


def coverage_for(item, nr_key=None, coverage_key=None):
    source = studio_cache.source_fingerprint(item)
    ranges = []
    for job in jobs.values():
        candidate = assets.get(job.get("asset_id"))
        if not candidate or candidate.get("source_sha256") != source or nr_key and job.get("nr_key") != nr_key \
                or coverage_key and job.get("coverage_key") != coverage_key:
            continue
        try:
            coverage = studio_cache.checkpoint_coverage(studio_cache.owned_path(OUTPUTS, job["id"] + "/work"))
        except ValueError:
            continue
        spans = coverage["ranges"]
        if not spans and job.get("status") == "done" and studio_cache.valid_result(job) and item["kind"] == "video":
            start = job.get("clip_start", 0)
            duration = job.get("settings", {}).get("clip_duration", 0) if job.get("job_kind") == "clip" else item.get("duration", 0)
            spans = [{"start": 0, "end": duration, "source_start": start, "source_end": start + duration}]
        for span in spans:
            ranges.append({**span, "job_id": job["id"], "nr_key": job.get("nr_key"),
                           "coverage_key": job.get("coverage_key"), "status": job.get("status")})
    return ranges


@api.get("/api/coverage")
def source_coverage(asset_id: str, nr_key: str | None = None, coverage_key: str | None = None):
    with mutex:
        item = assets.get(asset_id)
        if not item:
            raise HTTPException(404, "素材已失效，请重新上传。")
        return {"asset_id": asset_id, "ranges": coverage_for(item, nr_key, coverage_key)}


@api.post("/api/cache/lookup")
def cache_lookup(settings: Settings):
    validate_settings(settings)
    with mutex:
        item = assets.get(settings.asset_id)
        if not item:
            raise HTTPException(404, "素材已失效，请重新上传。")
        if item["kind"] == "video" and settings.bitrate_mode == "auto":
            settings.bitrate = engine.recommended_bitrate(settings.size, item["width"], item["height"])
        try:
            selection = GPU.prepare()
            identity = runtime_identity(selection)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        key, nr_key = studio_cache.render_keys(settings, item, identity)
        coverage_key = studio_cache.processing_key(settings, item, identity)
        stage_key = studio_cache.model_stage_key(settings, item, identity)
        safe = identity.get("cacheable", True)
        cached = completed_match(key) if safe else None
        stage = image_stage_match(nr_key) if item["kind"] == "image" and safe else None
        video_stage = safe and item["kind"] == "video" and any(j.get("stage_key") == stage_key and j.get("cache_safe", True) and
            (OUTPUTS / j["id"] / "work" / "nr").is_dir() for j in jobs.values())
        return {"cache_key": key, "nr_key": nr_key, "stage_key": stage_key, "coverage_key": coverage_key, "cacheable": safe, "exact": bool(cached),
                "job_id": cached["id"] if cached else None, "stage_reusable": bool(stage or video_stage),
                "ranges": coverage_for(item, coverage_key=coverage_key)}


@api.get("/api/jobs/{jid}/timeline")
def job_timeline(jid: str, time: float = 0, count: int = 5):
    if not math.isfinite(time) or time < 0:
        raise HTTPException(400, "时间点无效。")
    with mutex:
        job = jobs.get(jid)
        if not job:
            raise HTTPException(404, "任务不存在。")
        if not job.get("resume_supported"):
            return {"frames": [], "exact": False}
    try:
        return studio_cache.timeline_window(studio_cache.owned_path(OUTPUTS, jid + "/work"), time, count)
    except (OSError, ValueError, TypeError, IndexError):
        return {"frames": [], "exact": False}


@api.get("/api/jobs/{jid}/{action}")
def result_file(jid: str, action: str):
    with mutex:
        job = jobs.get(jid, {})
        if job.get("status") != "done" or action not in {"download", "preview", "thumbnail"}:
            raise HTTPException(404, "结果尚未准备好。")
        path = job.get({"download": "result_path", "preview": "preview_path", "thumbnail": "thumbnail_path"}[action])
        if not path or not Path(path).is_file():
            raise HTTPException(404, "此预览或结果已失效，请重新处理。")
        return FileResponse(path, filename=Path(path).name if action == "download" else None)


class Cleanup(BaseModel):
    keep_asset_ids: list[str] = Field(default_factory=list, max_length=100)
    keep_job_ids: list[str] = Field(default_factory=list, max_length=100)
    include_results: bool = False
    preview_token: str | None = None


def folder_files(folder):
    def regular(path):
        stat = path.lstat()
        return not path.is_symlink() and not (getattr(stat, "st_file_attributes", 0) & 0x400)
    if not regular(folder):
        return
    for base, directories, files in os.walk(folder, followlinks=False):
        directories[:] = [name for name in directories if regular(Path(base) / name)]
        for name in files:
            path = Path(base) / name
            if regular(path):
                yield path


def folder_bytes(folder):
    return sum(path.stat().st_size for path in folder_files(folder))


def reclaimable_bytes(folders):
    """Count the last hard link only, so reuse never overstates freed space."""
    inodes = {}
    for folder in folders:
        for path in folder_files(folder):
            stat = path.stat()
            key = (stat.st_dev, stat.st_ino)
            value = inodes.setdefault(key, {"count": 0, "links": stat.st_nlink, "bytes": stat.st_size})
            value["count"] += 1
    return sum(value["bytes"] for value in inodes.values() if value["count"] >= value["links"])


@api.get("/api/cache")
def cache_stats():
    with mutex:
        stages = sum(Path(j["stage_path"]).stat().st_size for j in jobs.values()
                     if j.get("stage_path") and Path(j["stage_path"]).is_file())
        return {"bytes": folder_bytes(UPLOADS) + folder_bytes(OUTPUTS), "assets": len(assets), "jobs": len(jobs),
                "reusable_results": sum(j.get("status") == "done" and bool(j.get("cache_key")) for j in jobs.values()),
                "stage_bytes": stages, "stage_budget": 1024 * 1024 * 1024,
                "free_disk_bytes": shutil.disk_usage(OUTPUTS).free}


def cleanup_plan(request):
    keep_jobs = set(request.keep_job_ids) | cache_leases | {j["id"] for j in jobs.values() if j["status"] in RETAINED or j.get("favorite")}
    if not request.include_results:
        keep_jobs |= {j["id"] for j in jobs.values() if j["status"] == "done"}
    keep_assets = set(request.keep_asset_ids) | uploading_ids | source_preview_queued | {
        a["id"] for a in assets.values() if a.get("preview_status") == "preparing"}
    for jid in keep_jobs:
        job = jobs.get(jid, {})
        keep_assets.update(a for a in (job.get("asset_id"), job.get("source_asset_id")) if a)
    keep_assets |= {a["id"] for a in assets.values() if time.time() - (a.get("created_at") or 0) < 3600}
    candidates = []
    for parent, keep, records in ((OUTPUTS, keep_jobs, jobs), (UPLOADS, keep_assets, assets)):
        for folder in sorted(parent.iterdir()):
            if not folder.is_dir() or folder.name in keep or not re.fullmatch(r"[0-9a-f]{32}", folder.name):
                continue
            if folder.resolve().parent != parent.resolve() or folder.is_symlink() or getattr(folder.lstat(), "st_file_attributes", 0) & 0x400:
                continue
            if parent == OUTPUTS and folder.name not in jobs and not request.include_results:
                if any(f.is_file() and f.stat().st_size for f in folder.iterdir()):
                    continue
            record = records.get(folder.name, {})
            asset = assets.get(record.get("source_asset_id") or record.get("asset_id"), {})
            candidates.append({"id": folder.name, "kind": "asset" if parent == UPLOADS else "result" if record.get("status") == "done" else "temporary",
                "name": record.get("name") or asset.get("name") or "未完成或旧版文件", "bytes": folder_bytes(folder)})
    # Distribute actual reclaimable bytes across entries while accounting for
    # duplicate immutable payloads in the same cleanup plan.
    files = {}
    for entry in candidates:
        parent = UPLOADS if entry["kind"] == "asset" else OUTPUTS
        entry["logical_bytes"] = entry["bytes"]
        entry["bytes"] = 0
        for path in folder_files(parent / entry["id"]):
            stat = path.stat()
            key = (stat.st_dev, stat.st_ino)
            record = files.setdefault(key, {"count": 0, "links": stat.st_nlink, "bytes": stat.st_size})
            record.update(count=record["count"] + 1, entry=entry)
    for record in files.values():
        if record["count"] >= record["links"]:
            record["entry"]["bytes"] += record["bytes"]
    token = hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).hexdigest()
    return candidates, keep_jobs, token


@api.post("/api/cache/preview")
def preview_cleanup(request: Cleanup):
    with mutex:
        candidates, _, token = cleanup_plan(request)
        counts = {kind: sum(i["kind"] == kind for i in candidates) for kind in ("temporary", "asset", "result")}
        return {"items": candidates, "bytes": sum(i["bytes"] for i in candidates), "counts": counts, "preview_token": token}


@api.post("/api/cache/cleanup")
def cleanup_cache(request: Cleanup):
    removed, skipped, freed = 0, 0, 0
    with mutex:
        candidates, keep_jobs, token = cleanup_plan(request)
        if request.preview_token is not None and request.preview_token != token:
            raise HTTPException(409, "清理范围已变化，请刷新预览后重试。")
        for entry in candidates:
            parent, records = (UPLOADS, assets) if entry["kind"] == "asset" else (OUTPUTS, jobs)
            try:
                reclaimable = reclaimable_bytes([parent / entry["id"]])
                remove_folder(parent / entry["id"], parent)
                records.pop(entry["id"], None)
                removed += 1
                freed += reclaimable
            except (OSError, ValueError):
                skipped += 1
        for jid in list(jobs):
            if jid not in keep_jobs and not (OUTPUTS / jid).exists():
                jobs.pop(jid)
        try:
            persist()
        except OSError as exc:
            raise HTTPException(507, "文件已清理，但记录未能保存，请检查磁盘空间。") from exc
    return {"removed": removed, "freed_bytes": freed, "skipped": skipped}


api.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="studio")
