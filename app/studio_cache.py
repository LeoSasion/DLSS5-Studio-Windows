"""Content-addressed task identities and private, immutable result reuse.

Every job owns its directory. Hard links save space without coupling directory
lifetimes: removing an old job cannot remove another job's result or segments.
"""
import hashlib
import bisect
import json
import os
import shutil
from pathlib import Path


CACHE_SCHEMA = "studio-render-v3"
ENCODING_FIELDS = {"codec", "container", "cq", "bitrate", "bitrate_mode", "bit_depth",
                   "enc_preset", "audio", "prores_profile", "preserve_media"}
IMAGE_FIELDS = {"size", "style", "preset", "intensity", "local_structure", "local_tone", "skin",
                "global_tone", "detail", "color", "ui_correction", "auto_mask", "hdr",
                "protect_region", "protect_skin", "protect_highlights", "render_scale"}
_timeline_cache = {}


def digest_file(path, check=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            if check:
                check()
            digest.update(block)
    return digest.hexdigest()


def source_fingerprint(item):
    """Rehash only when our private uploaded file's size or timestamp changed."""
    path = Path(item["path"])
    stat = path.stat()
    stamp = [stat.st_size, stat.st_mtime_ns]
    if item.get("fingerprint_stat") != stamp or not item.get("source_sha256"):
        item["source_sha256"] = digest_file(path)
        item["fingerprint_stat"] = stamp
    return item["source_sha256"]


def canonical_settings(settings, item, *, neural=False):
    values = settings.model_dump() if hasattr(settings, "model_dump") else dict(settings)
    for key in ("asset_id", "request_id", "source_asset_id", "frame_time", "diagnostic"):
        values.pop(key, None)
    if item["kind"] == "image":
        values = {key: value for key, value in values.items() if key in IMAGE_FIELDS}
        if neural:
            for key in ("detail", "color", "protect_region", "protect_skin", "protect_highlights"):
                values.pop(key, None)
    else:
        values.pop("hdr", None)  # Video transfer is detected from the source.
        full = values.get("job_kind") != "clip"
        if full:
            values["clip_start"] = 0
            values["clip_duration"] = item.get("duration", 0)
        values.pop("job_kind", None)
        values.pop("bitrate_mode", None)  # The resolved bitrate is the effective setting.
        codec = values.get("codec", "")
        if codec.startswith("h264_"):
            values["bit_depth"] = 8
        if codec in {"prores", "ffv1"}:
            for key in ("cq", "bitrate", "enc_preset", "bit_depth"):
                values.pop(key, None)
        elif values.get("bitrate", 0) > 0:
            values.pop("cq", None)
        if not codec.endswith("_nvenc"):
            values.pop("enc_preset", None)
        if codec != "prores":
            values.pop("prores_profile", None)
        if neural:
            values = {key: value for key, value in values.items() if key not in ENCODING_FIELDS}
    return values


def render_keys(settings, item, runtime):
    source = source_fingerprint(item)
    def key(neural):
        value = {"schema": CACHE_SCHEMA, "source": source, "kind": item["kind"],
                 "runtime": runtime, "settings": canonical_settings(settings, item, neural=neural)}
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return key(False), key(True)


def processing_key(settings, item, runtime):
    """Coverage identity: same controls/environment, independent of selection.

    It is only for displaying processed spans; checkpoint reuse still needs the
    stricter range/temporal identity returned by ``render_keys``.
    """
    values = canonical_settings(settings, item, neural=True)
    if item["kind"] == "video":
        for key in ("clip_start", "clip_duration", "frames"):
            values.pop(key, None)
    value = {"schema": CACHE_SCHEMA, "source": source_fingerprint(item), "kind": item["kind"],
             "runtime": runtime, "settings": values}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def model_stage_key(settings, item, runtime):
    values = canonical_settings(settings, item, neural=True)
    for key in ("detail", "color", "stability", "protect_region", "protect_skin", "protect_highlights"):
        values.pop(key, None)
    value = {"schema": CACHE_SCHEMA, "source": source_fingerprint(item), "kind": item["kind"],
             "runtime": runtime, "settings": values}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def regular(path):
    path = Path(path)
    stat = path.lstat()
    return not path.is_symlink() and not (getattr(stat, "st_file_attributes", 0) & 0x400)


def owned_path(root, relative):
    """A generated cache path may not traverse even an internal junction."""
    root, relative = Path(root).resolve(), Path(relative)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError("任务缓存路径无效。")
    target = root
    for part in relative.parts:
        target = target / part
        if target.exists() or target.is_symlink():
            if not regular(target):
                raise ValueError("任务缓存目录包含符号链接或重解析点。")
    if not target.resolve().is_relative_to(root):
        raise ValueError("任务缓存路径越界。")
    return target


def link_or_copy(source, target):
    source, target = Path(source), Path(target)
    if not source.is_file() or not regular(source):
        raise ValueError("缓存不是普通文件")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def clone_result(job, folder):
    """Return job-owned paths. No cache record ever references a foreign folder."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    copied = {}
    for key in ("result_path", "preview_path", "thumbnail_path"):
        if not job.get(key):
            paths[key] = None
            continue
        source = Path(job[key])
        if not source.is_file():
            if key == "result_path":
                raise ValueError("缓存结果已失效")
            paths[key] = None
            continue
        if source not in copied:
            target = folder / source.name
            link_or_copy(source, target)
            copied[source] = target
        paths[key] = str(copied[source])
    return paths


def valid_result(job):
    path = Path(job.get("result_path", ""))
    try:
        if job.get("status") != "done" or not path.is_file() or not regular(path) or path.stat().st_size == 0:
            return False
        stat = path.stat()
        unchanged = job.get("result_stat") == [stat.st_size, stat.st_mtime_ns]
        if not unchanged and job.get("result_sha256") and digest_file(path) != job["result_sha256"]:
            return False
        return True
    except (OSError, ValueError):
        return False


def checkpoint_coverage(work):
    """Only report committed, still-present segments within their owning work dir."""
    work = Path(work).resolve()
    try:
        value = json.loads((work / "checkpoint.json").read_text(encoding="utf-8"))
        spans = []
        for segment in value.get("segments", []):
            path = (work / segment["path"]).resolve()
            if not path.is_relative_to(work) or not path.is_file() or not regular(path) or not segment.get("completed"):
                continue
            start, end = float(segment["start"]), float(segment["end"])
            if 0 <= start < end:
                spans.append({"start": start, "end": end, "source_start": segment.get("source_start", start),
                              "source_end": segment.get("source_end", end), "frames": segment.get("frames", 0)})
        return {"render_key": value.get("application_render_key") or value.get("render_key"), "ranges": spans,
                "duration": value.get("duration", 0), "total_frames": value.get("total_frames", 0),
                "metrics": value.get("metrics", {})}
    except (OSError, ValueError, KeyError, TypeError):
        return {"ranges": []}


def timeline_window(work, time, count=5):
    """Return a bounded PTS window; full frame lists stay out of polling responses."""
    path = Path(work) / "frame-map.json"
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    value = _timeline_cache.get(key)
    if value is None:
        value = json.loads(path.read_text(encoding="utf-8"))
        _timeline_cache.clear()  # A seek needs one current map, not unbounded history.
        _timeline_cache[key] = value
    source, result = value.get("source_times", []), value.get("result_times", [])
    if not source or not result:
        return {"frames": [], "exact": False}
    at = min(len(source) - 1, bisect.bisect_left(source, time))
    if at and abs(source[at - 1] - time) <= abs(source[at] - time):
        at -= 1
    count = min(21, max(1, count))
    start = max(0, min(at - count // 2, len(source) - count))
    frames = []
    for index in range(start, min(len(source), start + count)):
        if len(source) == len(result):
            mapped = result[index]
        else:
            relative = source[index] - float(value.get("actual_start", source[0]))
            target = min(len(result) - 1, bisect.bisect_left(result, relative))
            if target and abs(result[target - 1] - relative) <= abs(result[target] - relative):
                target -= 1
            mapped = result[target]
        frames.append({"index": index, "source_time": source[index], "result_time": mapped})
    return {"frames": frames, "index": at, "total_frames": len(source),
            "actual_start": value.get("actual_start", source[0]), "timing": value.get("timing", "preserve"),
            "exact": len(source) == len(result) and value.get("timing", "preserve") == "preserve"}


def clone_work(source, target, render_key):
    """Copy a validated checkpoint and immutable payloads, never pause markers."""
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        return False
    try:
        checkpoint = json.loads((source / "checkpoint.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if (checkpoint.get("application_render_key") or checkpoint.get("render_key")) != render_key:
        return False
    committed = []
    for segment in checkpoint.get("segments", []):
        if not segment.get("completed") or not segment.get("sha256"):
            continue
        try:
            payloads = [(segment["path"], segment["sha256"])]
            if segment.get("raw_path"):
                payloads.append((segment["raw_path"], segment.get("raw_sha256")))
            checked = []
            for relative, expected in payloads:
                original = (source / relative).resolve()
                destination = (target / relative).resolve()
                if not original.is_relative_to(source) or not destination.is_relative_to(target) or not regular(original):
                    raise ValueError("分段缓存路径无效")
                if digest_file(original) != expected:
                    raise ValueError("分段缓存校验失败")
                checked.append((original, destination, expected))
            for original, destination, expected in checked:
                if not destination.exists() or digest_file(destination) != expected:
                    link_or_copy(original, destination)
            committed.append(segment)
        except (OSError, ValueError, KeyError):
            continue
    if not committed:
        return False
    checkpoint["segments"] = committed
    target.mkdir(parents=True, exist_ok=True)
    # Timeline maps describe the immutable source, not the chosen encoder.
    for name in ("timeline.json", "frame-map.json"):
        original = source / name
        if original.is_file() and regular(original):
            link_or_copy(original, target / name)
    temp = target / "checkpoint.tmp"
    temp.write_text(json.dumps(checkpoint, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temp.replace(target / "checkpoint.json")
    return True


def clone_neural_cache(source, target):
    """Raw NR caches verify their model key, per-frame input and payload in C++."""
    source, target = Path(source).resolve(), Path(target).resolve()
    original_root = source / "nr"
    if not original_root.is_dir() or not regular(original_root):
        return False
    copied = False
    for base, directories, files in os.walk(original_root, followlinks=False):
        directories[:] = [name for name in directories if regular(Path(base) / name)]
        for name in files:
            original = Path(base) / name
            if not original.is_file() or not regular(original):
                continue
            destination = target / original.relative_to(source)
            try:
                link_or_copy(original, destination)
                copied = True
            except OSError:
                continue
    if copied:
        for name in ("timeline.json", "frame-map.json"):
            original = source / name
            if original.is_file() and regular(original):
                link_or_copy(original, target / name)
    return copied
