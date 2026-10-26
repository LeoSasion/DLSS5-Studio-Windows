"""Content identities for rendering, independent of UI releases and install paths."""
import ctypes
import hashlib
import json
import os
import sys
import subprocess
import threading
import time
from pathlib import Path


PIPELINE_VERSION = "studio-render-3-hdr-temporal-pts"


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def windows_file_version(path):
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes
        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                               wintypes.DWORD, ctypes.c_void_p]
        version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                          ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(wintypes.UINT)]
        unused = wintypes.DWORD()
        size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused))
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, data):
            return None
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not version.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None
        fixed = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
        if length.value < 16 or fixed[0] != 0xFEEF04BD:
            return None
        return ".".join(str(n) for n in (fixed[2] >> 16, fixed[2] & 0xffff,
                                         fixed[3] >> 16, fixed[3] & 0xffff))
    except (OSError, ValueError, AttributeError):
        return None


class RuntimeIdentity:
    def __init__(self, root, driver_paths=None, model_paths=None):
        self.root = Path(root).resolve()
        system = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"
        self.driver_paths = list(driver_paths) if driver_paths is not None else [
            system / "nvcuda.dll", system / "nvapi64.dll"]
        self.model_paths = list(model_paths) if model_paths is not None else [
            Path(os.environ.get("ProgramData", "C:/ProgramData")) / "NVIDIA/NGX/models",
            self.root / "out/models"]
        self.lock = threading.RLock()
        self.checked = {}
        self.capability_cache = {}

    def file(self, path):
        path = Path(path)
        try:
            for _ in range(3):
                stat = path.stat()
                if not path.is_file() or path.is_symlink():
                    return {"name": path.name, "missing": True}
                key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
                with self.lock:
                    if key not in self.checked:
                        value = hashlib.sha256()
                        with path.open("rb") as stream:
                            for block in iter(lambda: stream.read(1024 * 1024), b""):
                                value.update(block)
                        after = path.stat()
                        if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            continue
                        self.checked[key] = value.hexdigest()
                    sha = self.checked[key]
                return {"name": path.name, "bytes": stat.st_size, "sha256": sha}
            return {"name": path.name, "unavailable": True, "changing": True}
        except OSError:
            return {"name": path.name, "unavailable": True}

    def models(self):
        stores = []
        for index, path in enumerate(self.model_paths):
            path = Path(path)
            if not path.exists():
                continue
            files = []
            try:
                for file in sorted(path.rglob("*")):
                    if file.is_file() and not file.is_symlink():
                        value = self.file(file)
                        for attempt in range(15):
                            if value.get("bytes") != 0:
                                break
                            # Model weights and selector/config files are not
                            # meaningfully identified by an in-flight truncate.
                            if attempt == 14:
                                value = {"name": file.name, "unavailable": True, "changing": True}
                            else:
                                time.sleep(.02)
                                value = self.file(file)
                        if file.suffix.lower() == ".json":
                            # NGX rewrites its mapping file during feature creation.
                            # A truncated transient file is never a model identity.
                            for attempt in range(15):
                                try:
                                    before = file.stat()
                                    data = json.loads(file.read_text(encoding="utf-8-sig"))
                                    after = file.stat()
                                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                        raise ValueError("Model selection is changing")
                                    value = {"name": file.name, "sha256": digest_json(data)}
                                    break
                                except (OSError, ValueError):
                                    if attempt == 14:
                                        value = {"name": file.name, "unavailable": True, "changing": True}
                                    else:
                                        time.sleep(.02)
                        value["name"] = file.relative_to(path).as_posix()
                        files.append(value)
                stores.append({"store": index, "files": files})
            except OSError:
                stores.append({"store": index, "unavailable": True})
        stable = not any(store.get("unavailable") or any(file.get("unavailable")
                         for file in store.get("files", [])) for store in stores)
        return {"digest": digest_json(stores), "stores": stores, "stable": stable}

    def build(self, selection):
        drivers = []
        for path in self.driver_paths:
            item = self.file(path)
            item["version"] = windows_file_version(path)
            drivers.append(item)
        renderer = [self.file(self.root / "out" / name) for name in (
            "video2dlssnr.exe", "nvngx.dll_dlssnr.dll", "nvngx_dlss.dll")]
        media_tools = [self.file(self.root / "out" / name) for name in ("ffmpeg.exe", "ffprobe.exe")]
        # UI files deliberately do not participate. Pixel/encoding changes do.
        pipeline = [self.file(self.root / name) for name in (
            "nr_video.py", "render_engine.py", "video_pipeline.py", "video_quality.py")
            if (self.root / name).is_file()]
        runtime = self.file(selection.dll_dir / "nvngx_dlssnr.dll")
        identity = {"identity_schema": 1, "pipeline": PIPELINE_VERSION,
                    "renderer": renderer, "media_tools": media_tools, "processing": pipeline,
                    "runtime": runtime, "driver": drivers, "model": self.models(),
                    "gpu": {"luid": selection.luid, "profile": selection.profile,
                            "name": selection.name}}
        identity["cacheable"] = identity["model"]["stable"] and not any(
            item.get("unavailable") for item in renderer + media_tools + drivers + [runtime])
        identity["digest"] = digest_json(identity)
        return identity

    def capabilities(self):
        exe = self.root / "out/video2dlssnr.exe"
        key = self.file(exe).get("sha256")
        with self.lock:
            if key not in self.capability_cache:
                value = {}
                try:
                    process = subprocess.run([str(exe), "--nr-capabilities"], capture_output=True,
                        text=True, encoding="utf-8", timeout=10,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if process.returncode == 0:
                        value = json.loads(process.stdout.strip())
                        if not isinstance(value, dict) or value.get("renderer") != "dlss-studio-native":
                            value = {}
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                self.capability_cache[key] = value
            return dict(self.capability_cache[key])
