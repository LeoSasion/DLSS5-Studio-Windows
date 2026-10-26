"""Atomic, portable job metadata. Media remains in the existing workspace folders."""
import json
import os
from pathlib import Path


ACTIVE = {"queued", "running", "pausing", "cancelling"}
RETAINED = ACTIVE | {"paused"}


def save(path, root, assets, jobs):
    def portable(record):
        item = dict(record)
        for key in ("path", "result_path", "preview_path", "thumbnail_path", "work_path", "stage_path", "source_preview_path"):
            if item.get(key):
                item[key] = Path(item[key]).resolve().relative_to(root.resolve()).as_posix()
        return item
    value = {"version": 2, "assets": [portable(a) for a in assets.values()],
             "jobs": [portable(j) for j in jobs.values()]}
    temp = path.with_suffix(".tmp")
    try:
        with temp.open("w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def load(path, root):
    assets, jobs = {}, {}
    if not path.exists():
        return assets, jobs
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") not in {1, 2}:
        raise ValueError("不支持的任务记录版本")
    for kind, target in (("assets", assets), ("jobs", jobs)):
        for saved in value.get(kind, []):
            item = dict(saved)
            for key in ("path", "result_path", "preview_path", "thumbnail_path", "work_path", "stage_path", "source_preview_path"):
                if item.get(key):
                    resolved = (root / item[key]).resolve()
                    allowed = root / ("uploads" if key in {"path", "source_preview_path"} else "ui_out")
                    if not resolved.is_relative_to(allowed.resolve()):
                        raise ValueError("任务记录路径无效")
                    item[key] = str(resolved)
            if kind == "assets" and not Path(item["path"]).is_file():
                continue
            if kind == "assets" and item.get("source_preview_path") and not Path(item["source_preview_path"]).is_file():
                item.update(preview_status="preparing", preview_url=None)
                item.pop("source_preview_path", None)
            if item.get("status") in ACTIVE:
                if item.get("queue_version") == 1 and item["status"] == "queued":
                    item.update(message="已恢复等待队列。", phase="queued")
                elif item.get("resume_supported"):
                    item.update(status="paused", message="上次处理已中断；继续后会复用已完成片段。", phase="paused", resumable=True)
                elif item.get("queue_version") == 1 and item["status"] != "cancelling":
                    item.update(status="queued", message="上次图片处理已中断，已重新排队。", phase="queued")
                else:
                    item.update(status="error", message="上次处理因服务退出而中断，请重新开始。", phase="interrupted")
            if item.get("status") == "done" and not Path(item.get("result_path", "")).is_file():
                item.update(status="error", message="结果文件已被移动或删除，请重新处理。", phase="missing")
                item.pop("download", None)
                item.pop("preview", None)
            target[item["id"]] = item
    return assets, jobs
