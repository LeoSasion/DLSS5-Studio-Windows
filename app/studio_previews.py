"""Build a validated SDR display proxy without modifying the HDR source."""
import uuid
from pathlib import Path

import nr_video
import render_engine as engine
import studio_cache


def build(item, uploads, runner, nvenc=False, progress=None):
    folder = studio_cache.owned_path(uploads, item["id"])
    final = studio_cache.owned_path(uploads, item["id"] + "/source_preview.mp4")
    temporary = studio_cache.owned_path(uploads, item["id"] + "/source_preview." + uuid.uuid4().hex + ".partial.mp4")
    try:
        generated = engine.make_preview(item["path"], runner=runner, nvenc=nvenc,
                                        progress=progress, destination=str(temporary))
        runner.check()
        if not generated or not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("无法生成原片的 SDR 显示预览；高精度原片仍保留，可重试。")
        info = nr_video.probe(engine.find_tool("ffprobe"), str(temporary))
        if info["w"] < 1 or info["h"] < 1 or info.get("trc") in {"smpte2084", "arib-std-b67"}:
            raise RuntimeError("原片显示预览未通过 SDR 格式验证。")
        tolerance = max(.25, 2 / max(float(item.get("fps") or 1), 1))
        if item.get("duration") and abs(info.get("duration", 0) - item["duration"]) > tolerance:
            raise RuntimeError("原片显示预览与源视频时长不一致，原片仍保留。")
        runner.check()
        temporary.replace(final)
        return {"preview_status": "ready", "source_preview_path": str(final),
                "preview_url": f"/api/assets/{item['id']}/preview", "preview_message": "SDR 显示预览已准备好。",
                "preview_width": info["w"], "preview_height": info["h"], "preview_progress": 100}
    finally:
        temporary.unlink(missing_ok=True)
