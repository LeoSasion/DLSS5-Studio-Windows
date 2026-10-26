"""Create, verify and transactionally apply a small application-only update."""
import argparse
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
_locks, _locks_guard = {}, threading.Lock()


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def regular(path):
    path = Path(path)
    value = path.lstat()
    return not path.is_symlink() and not (getattr(value, "st_file_attributes", 0) & 0x400)


def path_parts(name):
    if not isinstance(name, str) or not name or len(name) > 1024 or "\\" in name or ":" in name or "\0" in name:
        raise ValueError("更新路径无效。")
    path = Path(name)
    parts = name.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if path.is_absolute() or path.drive or any(not part or part in {".", ".."} or part.rstrip(" .") != part or
            part.split(".")[0].upper() in reserved for part in parts):
        raise ValueError("更新路径含有不安全的文件名。")
    return path


def allowed(name):
    try:
        path = path_parts(name)
    except ValueError:
        return False
    return ((len(path.parts) == 2 and path.parts[0] == "app" and path.suffix == ".py" and not path.name.startswith("test_")) or
            (len(path.parts) >= 3 and path.parts[:2] == ("app", "web")) or
            name in ("app/out/video2dlssnr.exe", "app/out/nvngx.dll_dlssnr.dll", "DLSS Studio.exe"))


def safe_target(root, name):
    path = path_parts(name)
    target = root
    for part in path.parts:
        target = target / part
        if target.exists() or target.is_symlink():
            if not regular(target):
                raise ValueError("更新路径包含符号链接或重解析点。")
    if not target.resolve().is_relative_to(root):
        raise ValueError("更新目标越界。")
    return target


def atomic_bytes(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with Path(source).open("rb") as incoming, temporary.open("xb") as output:
            shutil.copyfileobj(incoming, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def journal(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def package_transaction(function):
    @functools.wraps(function)
    def wrapped(package, *args, **kwargs):
        root = Path(package).resolve()
        if not safe_target(root, "app/serve_local.py").is_file():
            raise ValueError("目标不是工作台目录。")
        with _locks_guard:
            lock = _locks.setdefault(os.path.normcase(str(root)), threading.RLock())
        with lock:
            path = safe_target(root, "updates/.update.lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+b") as stream:
                if os.fstat(stream.fileno()).st_nlink > 1:
                    raise ValueError("更新锁文件不是独立常规文件。")
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
                                raise RuntimeError("另一个进程正在更新应用，请稍后重试。")
                            time.sleep(.1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    return function(root, *args, **kwargs)
                finally:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return wrapped


def create(package, output):
    package = Path(package).resolve()
    safe_target(package, "app/serve_local.py")
    safe_target(package, "app/web")
    files = {}
    for base, directories, names in os.walk(package, followlinks=False):
        directories[:] = [name for name in directories if regular(Path(base) / name)]
        for filename in names:
            path = Path(base) / filename
            name = path.relative_to(package).as_posix()
            if name.startswith("app/web/test-preview-4k-"):
                continue
            if allowed(name) and regular(path) and path.is_file() and "__pycache__" not in path.parts and not name.endswith(".pyc"):
                safe_target(package, name)
                files[name] = {"sha256": digest(path), "bytes": path.stat().st_size}
    if "app/serve_local.py" not in files:
        raise ValueError("不是完整的工作台目录。")
    manifest = {"format": "dlss-studio-update", "version": 1, "created_at": time.time(), "files": files}
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("update.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for name in files:
            archive.write(safe_target(package, name), name)
    with zipfile.ZipFile(output) as archive:
        verify(archive)
    return manifest


def verify(archive):
    try:
        if archive.getinfo("update.json").file_size > 4 * 1024 * 1024:
            raise ValueError("更新清单过大。")
        manifest = json.loads(archive.read("update.json"))
        files = manifest["files"]
        if manifest.get("format") != "dlss-studio-update" or manifest.get("version") != 1 or not isinstance(files, dict) or not 1 <= len(files) <= 20000:
            raise ValueError("更新格式无效。")
        names = archive.namelist()
        if len(names) != len({name.casefold() for name in names}) or set(names) != {"update.json", *files}:
            raise ValueError("更新文件列表不匹配或大小写冲突。")
        total = 0
        for name, info in files.items():
            if not allowed(name) or not isinstance(info, dict) or type(info.get("bytes")) is not int or not 0 <= info["bytes"] <= 128 * 1024 * 1024 \
                    or not re.fullmatch(r"[a-f0-9]{64}", info.get("sha256", "")):
                raise ValueError("更新含有受保护或无效路径。")
            entry = archive.getinfo(name)
            if entry.is_dir() or (entry.external_attr >> 16) & 0o170000 == 0o120000 or entry.file_size != info["bytes"]:
                raise ValueError("更新文件类型或大小无效。")
            total += info["bytes"]
            if total > 512 * 1024 * 1024:
                raise ValueError("应用增量包超过 512 MiB。")
            value = hashlib.sha256()
            with archive.open(name) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
            if value.hexdigest() != info["sha256"]:
                raise ValueError("更新校验失败。")
        return manifest
    except (KeyError, TypeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise ValueError("更新清单或文件无效。") from exc


def package_manifest_data(root, changes):
    path = safe_target(root, "manifest.json")
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("安装包校验清单无效。")
    for name, info in changes.items():
        value[name] = {"bytes": info["bytes"], "sha256": info["sha256"]}
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def prepare_operation(root, backup, name, after, internal=False):
    target = safe_target(root, name)
    if target.exists() and not target.is_file():
        raise ValueError("更新目标不是普通文件。")
    existed = target.is_file()
    item = {"path": name, "existed": existed, "after": after, "internal_manifest": internal}
    if existed:
        copy = safe_target(backup, "before/" + name)
        atomic_copy(target, copy)
        item["before"] = digest(copy)
        if digest(target) != item["before"]:
            raise ValueError("应用文件正在变化，更新已取消。")
    return item


def restore_operations(root, backup, operations):
    errors = []
    for item in reversed(operations):
        try:
            target = safe_target(root, item["path"])
            if item.get("after") and (not target.is_file() or digest(target) != item["after"]):
                raise ValueError("应用文件在回退过程中被修改，保留用户修改。")
            if item["existed"]:
                source = safe_target(backup, "before/" + item["path"])
                if digest(source) != item["before"]:
                    raise ValueError("回退备份校验失败。")
                atomic_copy(source, target)
            else:
                target.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError("回退未能全部完成；完整备份仍保留在更新历史中。" + "; ".join(errors))


@package_transaction
def apply_update(package, update):
    root = Path(package).resolve()
    if not safe_target(root, "app/serve_local.py").is_file():
        raise ValueError("目标不是工作台目录。")
    revision = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    backup = safe_target(root, "updates/history/" + revision)
    with zipfile.ZipFile(update) as archive:
        manifest = verify(archive)
        # Check every destination and parse the installation manifest before any
        # application mutation. Internal junctions could otherwise redirect web
        # updates into uploads even when they remain beneath the package root.
        for name in manifest["files"]:
            safe_target(root, name)
        manifest_data = package_manifest_data(root, manifest["files"])
        backup.mkdir(parents=True, exist_ok=False)
        operations = []
        for name, info in manifest["files"].items():
            incoming = safe_target(backup, "incoming/" + name)
            incoming.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as stream, incoming.open("xb") as target:
                shutil.copyfileobj(stream, target, 1024 * 1024)
            if digest(incoming) != info["sha256"]:
                raise ValueError("暂存的更新文件校验失败。")
            operations.append(prepare_operation(root, backup, name, info["sha256"]))
        if manifest_data is not None:
            incoming = safe_target(backup, "incoming/manifest.json")
            atomic_bytes(incoming, manifest_data)
            operations.append(prepare_operation(root, backup, "manifest.json", hashlib.sha256(manifest_data).hexdigest(), True))
        record = {"format": "dlss-studio-rollback", "version": 2, "revision": revision, "status": "prepared", "files": operations}
        record_path = safe_target(backup, "rollback.json")
        journal(record_path, record)
        changed = []
        try:
            for item in operations:
                target = safe_target(root, item["path"])
                if item["existed"]:
                    if not target.is_file() or digest(target) != item["before"]:
                        raise ValueError("更新期间应用文件发生变化，已取消。")
                elif target.exists():
                    raise ValueError("更新目标已被其他操作创建，已取消。")
                atomic_copy(safe_target(backup, "incoming/" + item["path"]), target)
                changed.append(item)
            record["status"] = "applied"
            journal(record_path, record)
        except Exception:
            try:
                restore_operations(root, backup, changed)
                record["status"] = "failed_restored"
                journal(record_path, record)
            except Exception as restore_error:
                raise RuntimeError("更新失败且需要手动恢复，备份保留在 " + str(backup)) from restore_error
            raise
    return revision


def update_package_manifest(root, changed):
    values = {}
    for name in changed:
        target = safe_target(root, name)
        if target.is_file():
            values[name] = {"bytes": target.stat().st_size, "sha256": digest(target)}
    data = package_manifest_data(root, values)
    if data is not None:
        atomic_bytes(safe_target(root, "manifest.json"), data)


@package_transaction
def rollback(package, revision):
    if not re.fullmatch(r"\d+-[a-f0-9]{8}", revision):
        raise ValueError("回退标识无效。")
    root = Path(package).resolve()
    backup = safe_target(root, "updates/history/" + revision)
    record_path = safe_target(backup, "rollback.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("format") != "dlss-studio-rollback" or record.get("version") != 2 or record.get("status") != "applied":
        raise ValueError("回退记录无效或已回退。")
    files = record.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 20001:
        raise ValueError("回退记录文件列表无效。")
    seen = set()
    for item in files:
        name = item.get("path")
        if not (allowed(name) or name == "manifest.json" and item.get("internal_manifest")) or name.casefold() in seen:
            raise ValueError("回退记录包含受保护路径。")
        seen.add(name.casefold())
        target = safe_target(root, name)
        if not target.is_file() or digest(target) != item["after"]:
            raise ValueError("更新后文件已变化，拒绝覆盖用户修改。")
        if item["existed"]:
            source = safe_target(backup, "before/" + name)
            if not source.is_file() or digest(source) != item.get("before"):
                raise ValueError("回退备份已变化或损坏。")
    current = safe_target(backup, "rollback-current-" + uuid.uuid4().hex)
    current.mkdir()
    for item in files:
        atomic_copy(safe_target(root, item["path"]), safe_target(current, "before/" + item["path"]))
    reverted = []
    try:
        for item in reversed(files):
            restore_operations(root, backup, [item])
            reverted.append({"path": item["path"], "existed": True, "before": item["after"]})
        record["status"] = "rolled_back"
        journal(record_path, record)
    except Exception:
        restore_operations(root, current, reverted)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    make = sub.add_parser("create")
    make.add_argument("package", type=Path)
    make.add_argument("output", type=Path)
    install = sub.add_parser("apply")
    install.add_argument("package", type=Path)
    install.add_argument("update", type=Path)
    undo = sub.add_parser("rollback")
    undo.add_argument("package", type=Path)
    undo.add_argument("revision")
    args = parser.parse_args()
    if args.action == "create":
        manifest = create(args.package, args.output)
        print(json.dumps({"files": len(manifest["files"]), "bytes": args.output.stat().st_size, "sha256": digest(args.output)}))
    elif args.action == "apply":
        print(apply_update(args.package, args.update))
    else:
        rollback(args.package, args.revision)
        print("Restored application files; media, settings and runtime components preserved.")


if __name__ == "__main__":
    main()
