"""Local diagnostics and a small, reproducible continuous-render fixture."""
import subprocess
import time
from pathlib import Path

import studio_cache


def create_self_test_asset(folder, ffmpeg, probe):
    folder = Path(folder)
    folder.mkdir(parents=True)
    source = folder / "source.mp4"
    command = [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
               "testsrc2=size=320x180:rate=12:duration=1", "-f", "lavfi", "-i",
               "sine=frequency=440:duration=1", "-map", "0:v:0", "-map", "1:a:0",
               "-c:v", "libopenh264", "-b:v", "1000k", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", "-shortest", str(source)]
    result = subprocess.run(command, capture_output=True, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not source.is_file():
        detail = result.stderr.decode("utf-8", "replace")[-1000:]
        raise RuntimeError("无法生成连续视频自检素材：" + detail)
    info = probe(source)
    item = {"id": folder.name, "name": "连续视频自检.mp4", "kind": "video", "width": info["w"], "height": info["h"],
            "duration": info.get("duration", 1), "fps": info["fps"], "frame_count": info.get("frames", 12),
            "path": str(source), "bytes": source.stat().st_size, "url": f"/api/assets/{folder.name}",
            "created_at": time.time(), "diagnostic": True}
    studio_cache.source_fingerprint(item)
    return item


def snapshot(hardware, identity, jobs):
    recent = sorted(jobs.values(), key=lambda j: (j.get("created_at") or 0), reverse=True)[:12]
    return {"version": 1, "hardware": hardware, "runtime": identity,
            "queue": {"waiting": sum(j.get("status") == "queued" for j in jobs.values()),
                      "running": sum(j.get("status") in {"running", "pausing", "cancelling"} for j in jobs.values()),
                      "paused": sum(j.get("status") == "paused" for j in jobs.values())},
            "recent_jobs": [{"id": j["id"], "status": j.get("status"), "phase": j.get("phase"),
                             "cache_hit": j.get("cache_hit", False), "cache_mode": j.get("cache_mode"),
                             "metrics": j.get("metrics", {}), "validation": j.get("validation", {}),
                             "runtime_digest": j.get("runtime", {}).get("digest"),
                             "diagnostic": j.get("diagnostic", False)} for j in recent]}
