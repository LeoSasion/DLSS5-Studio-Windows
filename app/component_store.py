"""Transactional, opt-in local runtime imports and rollback; never downloads a DLL."""
import hashlib
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import time
import threading
import uuid


_locks, _locks_guard = {}, threading.Lock()


def regular(path):
    path = Path(path)
    stat = path.lstat()
    return not path.is_symlink() and not (getattr(stat, "st_file_attributes", 0) & 0x400)


def safe_path(root, relative):
    relative = Path(relative)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if relative.is_absolute() or relative.drive or ".." in relative.parts or any(
            not part or ":" in part or "\0" in part or part.rstrip(" .") != part or part.split(".")[0].upper() in reserved
            for part in relative.parts):
        raise ValueError("组件路径无效。")
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if not regular(current):
                raise ValueError("组件目录包含符号链接或重解析点。")
    if not current.resolve().is_relative_to(root):
        raise ValueError("组件路径越界。")
    return current


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def validate_dll(path, expected_sha256):
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
        raise ValueError("必须提供组件发布方的 SHA-256。")
    value = hashlib.sha256()
    if not Path(path).is_file() or not regular(path) or Path(path).stat().st_size > 512 * 1024 * 1024:
        raise ValueError("组件必须是不超过 512 MiB 的本地常规 DLL 文件。")
    with Path(path).open("rb") as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise ValueError("不是有效的 Windows DLL。")
        pe = struct.unpack_from("<I", header, 60)[0]
        stream.seek(pe)
        coff = stream.read(24)
        if len(coff) < 24 or coff[:4] != b"PE\0\0" or struct.unpack_from("<H", coff, 4)[0] != 0x8664 or not (struct.unpack_from("<H", coff, 22)[0] & 0x2000):
            raise ValueError("组件必须是 x64 DLL。")
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    if value.hexdigest() != expected_sha256.lower():
        raise ValueError("组件 SHA-256 与发布方校验值不符。")
    return value.hexdigest()


class ComponentStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifest = self.root / "dlss5-components.json"
        self.store = self.root / "out/dlss5"
        self.history = self.store / "component-history"

    def read(self):
        return json.loads(safe_path(self.root, "dlss5-components.json").read_text(encoding="utf-8"))

    @contextlib.contextmanager
    def transaction(self):
        with _locks_guard:
            lock = _locks.setdefault(str(self.root), threading.RLock())
        with lock:
            lock_path = safe_path(self.root, "out/dlss5/.component.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as stream:
                if os.fstat(stream.fileno()).st_nlink > 1:
                    raise ValueError("组件锁文件不是独立常规文件。")
                if stream.tell() == 0:
                    stream.write(b"\0"); stream.flush()
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    until = time.monotonic() + 10
                    while True:
                        try:
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            if time.monotonic() >= until:
                                raise RuntimeError("另一个窗口正在更新组件，请稍后重试。")
                            time.sleep(.1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def validate_manifest(self, value, verify_files=True):
        if not isinstance(value.get("profiles"), dict) or not value["profiles"]:
            raise ValueError("组件清单无效。")
        for info in value["profiles"].values():
            dll = safe_path(self.root, info["path"])
            if not dll.is_relative_to(self.store) or dll.name != "nvngx_dlssnr.dll":
                raise ValueError("组件清单路径无效。")
            if not verify_files:
                continue
            if dll.stat().st_size != info["bytes"]:
                raise ValueError("组件缺失或已损坏。")
            validate_dll(dll, info["sha256"])

    def import_dll(self, path, profile, sha256, version, source):
        if profile not in ("rtx30", "rtx40", "rtx50"):
            raise ValueError("组件系列无效。")
        if not version.strip() or not source.strip():
            raise ValueError("需要记录组件版本和来源。")
        digest = validate_dll(path, sha256)
        with self.transaction():
            previous = self.read()
            self.validate_manifest(previous, verify_files=False)
            target = safe_path(self.root, f"out/dlss5/{profile}/{digest}/nvngx_dlssnr.dll")
            target.parent.mkdir(parents=True, exist_ok=True)
            created = not target.exists()
            revision = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
            history = safe_path(self.root, "out/dlss5/component-history")
            history.mkdir(parents=True, exist_ok=True)
            record = history / (revision + ".json")
            staged = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                if target.exists():
                    validate_dll(target, digest)  # Immutable digest directories are never overwritten.
                else:
                    shutil.copyfile(path, staged)
                    validate_dll(staged, digest)
                    staged.replace(target)
                encoded = json.dumps(previous, sort_keys=True, separators=(",", ":")).encode("utf-8")
                atomic_json(record, {"format": "dlss-component-history", "version": 1, "manifest": previous,
                                     "sha256": hashlib.sha256(encoded).hexdigest()})
                current = json.loads(json.dumps(previous))
                current["profiles"][profile] = {"path": target.relative_to(self.root).as_posix(),
                    "sha256": digest, "bytes": target.stat().st_size, "version": version.strip(),
                    "source": source.strip(), "imported_at": time.time(), "previous_revision": revision,
                    "validation": "matched; continuous rendering self-test required"}
                atomic_json(safe_path(self.root, "dlss5-components.json"), current)
            except Exception:
                if created:
                    target.unlink(missing_ok=True)
                record.unlink(missing_ok=True)
                raise
            finally:
                staged.unlink(missing_ok=True)
            return {"profile": profile, "sha256": digest, "rollback_revision": revision}

    def rollback(self, revision):
        if not re.fullmatch(r"\d+-[a-f0-9]{8}", revision):
            raise ValueError("回退版本标识无效。")
        with self.transaction():
            path = safe_path(self.root, f"out/dlss5/component-history/{revision}.json")
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("format") == "dlss-component-history":
                value = record["manifest"]
                encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if record.get("sha256") != hashlib.sha256(encoded).hexdigest():
                    raise ValueError("回退记录已损坏。")
            else:
                value = record  # Earlier locally-created history remains usable.
            self.validate_manifest(value)
            atomic_json(safe_path(self.root, "dlss5-components.json"), value)
            return value
