"""Prepare pinned NR runtimes and atomically switch all GPU profiles.

This tool only downloads and validates files; it never loads a DLL or starts NR.
The adjacent catalog supplies every version, URL, size and SHA-256.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile


ROOT = Path(__file__).resolve().parent.parent
CATALOG = Path(__file__).resolve().with_name("dlss5-catalog.json")
MAX_BYTES = 512 * 1024 * 1024
PROFILES = ("rtx30", "rtx40", "rtx50")
BLOCK = 1024 * 1024

# This module performs no runtime discovery or GPU initialization.
sys.path.insert(0, str(ROOT / "app"))
from component_store import ComponentStore, atomic_json, regular, safe_path, validate_dll


class HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if urllib.parse.urlsplit(new_url).scheme != "https":
            raise ValueError("Refusing a download redirect without HTTPS")
        return super().redirect_request(request, response, code, message, headers, new_url)


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(BLOCK), b""):
            value.update(block)
    return value.hexdigest()


def read_catalog():
    value = json.loads(CATALOG.read_text(encoding="utf-8-sig"))
    if value.get("version") != 1 or not re.fullmatch(r"\d+(?:\.\d+){2,3}", value.get("release", "")):
        raise ValueError("Unsupported DLSS catalog")
    if set(value.get("profiles", {})) != set(PROFILES) or not isinstance(value.get("runtimes"), dict):
        raise ValueError("Catalog must map every supported GPU profile")
    for name in set(value["profiles"].values()):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]+", name):
            raise ValueError("Invalid runtime name")
        info = value["runtimes"][name]
        filename = info["file"]
        if not isinstance(filename, str) or not filename or Path(filename).name != filename or any(c in filename for c in "/\\:\0"):
            raise ValueError("Catalog asset must be a plain filename")
        if info.get("format") not in {"zip", "dll"}:
            raise ValueError("Only ZIP and DLL assets are supported")
        for field in ("artifact_sha256", "dll_sha256"):
            if not re.fullmatch(r"[a-fA-F0-9]{64}", info.get(field, "")):
                raise ValueError(f"Invalid catalog digest: {field}")
            info[field] = info[field].lower()
        for field in ("artifact_bytes", "dll_bytes"):
            size = info.get(field)
            if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_BYTES:
                raise ValueError(f"Invalid catalog size: {field}")
        for field in ("url", "api_url", "source"):
            parsed = urllib.parse.urlsplit(info[field])
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError(f"Catalog URL must use HTTPS: {field}")
        if not isinstance(info.get("version"), str) or not info["version"].strip():
            raise ValueError("Runtime version is required")
        if info.get("default_nr_preset", 2) != 2:
            raise ValueError("This catalog requires NR preset 2")
        if info.get("architecture", "native") != "native":
            raise ValueError("This catalog must preserve the native GPU architecture")
        if info["format"] == "dll" and (info["artifact_sha256"] != info["dll_sha256"] or info["artifact_bytes"] != info["dll_bytes"]):
            raise ValueError("Direct DLL asset identity must match its DLL identity")
    return value


def matches(path, expected_bytes, digest):
    path = Path(path)
    try:
        return path.is_file() and regular(path) and path.stat().st_size == expected_bytes and sha256(path) == digest
    except OSError:
        return False


def discard(path):
    """Best-effort cleanup of an explicitly owned temporary file."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def download(cache, info):
    target = safe_path(cache, info["file"])
    if matches(target, info["artifact_bytes"], info["artifact_sha256"]):
        return target
    opener = urllib.request.build_opener(
        HttpsRedirects(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    errors = []
    for url in dict.fromkeys((info["api_url"], info["url"])):
        part = safe_path(cache, info["file"] + "." + uuid.uuid4().hex + ".part")
        owned = False
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/octet-stream",
                "User-Agent": "DLSS-Studio-pinned-runtime-installer/1", "Accept-Encoding": "identity"})
            with opener.open(request, timeout=60) as response:
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != info["artifact_bytes"]:
                    raise ValueError("Download length does not match the pinned catalog")
                value, count = hashlib.sha256(), 0
                with part.open("xb") as output:
                    owned = True
                    for block in iter(lambda: response.read(BLOCK), b""):
                        count += len(block)
                        if count > info["artifact_bytes"] or count > MAX_BYTES:
                            raise ValueError("Download exceeded its pinned size")
                        output.write(block)
                        value.update(block)
                    output.flush()
                    os.fsync(output.fileno())
                if count != info["artifact_bytes"] or value.hexdigest() != info["artifact_sha256"]:
                    raise ValueError("Download SHA-256 or length does not match the pinned catalog")
            if matches(target, info["artifact_bytes"], info["artifact_sha256"]):
                return target
            safe_path(cache, info["file"])
            part.replace(target)
            return target
        except (OSError, ValueError) as error:
            errors.append(f"{urllib.parse.urlsplit(url).netloc}: {error}")
        finally:
            if owned:
                discard(part)
    raise RuntimeError("Could not download pinned asset " + info["file"] + ": " + "; ".join(errors))


def prepare_dll(cache, name, info):
    artifact = download(cache, info)
    if info["format"] == "dll":
        validate_dll(artifact, info["dll_sha256"])
        return artifact
    # Reuse a previously extracted DLL, including a manually seeded verified cache.
    for candidate in cache.glob("*.dll"):
        if matches(candidate, info["dll_bytes"], info["dll_sha256"]):
            validate_dll(candidate, info["dll_sha256"])
            return candidate
    target = safe_path(cache, f"{name}-{info['dll_sha256']}.dll")
    part = target.with_name(target.name + "." + uuid.uuid4().hex + ".part")
    owned = False
    try:
        with zipfile.ZipFile(artifact) as archive:
            candidates = [entry for entry in archive.infolist() if not entry.is_dir()
                          and entry.filename.replace("\\", "/").split("/")[-1].lower() == "nvngx_dlssnr.dll"]
            if len(candidates) != 1:
                raise ValueError("ZIP must contain exactly one nvngx_dlssnr.dll")
            entry = candidates[0]
            mode = (entry.external_attr >> 16) & 0xffff
            if stat.S_ISLNK(mode) or entry.file_size != info["dll_bytes"] or entry.flag_bits & 1:
                raise ValueError("ZIP DLL metadata does not match the pinned catalog")
            with archive.open(entry) as source, part.open("xb") as output:
                owned, count = True, 0
                for block in iter(lambda: source.read(BLOCK), b""):
                    count += len(block)
                    if count > info["dll_bytes"] or count > MAX_BYTES:
                        raise ValueError("Extracted DLL exceeded its pinned size")
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if count != info["dll_bytes"]:
                raise ValueError("Extracted DLL size does not match the pinned catalog")
        validate_dll(part, info["dll_sha256"])
        if target.exists():
            validate_dll(target, info["dll_sha256"])
        else:
            part.replace(target)
        return target
    finally:
        if owned:
            discard(part)


def copy_new(source, staged, temporary):
    with staged.open("xb") as output:
        temporary.append(staged)
        with source.open("rb") as stream:
            shutil.copyfileobj(stream, output, BLOCK)
        output.flush()
        os.fsync(output.fileno())


def install(app_root, catalog, prepared):
    store = ComponentStore(app_root)
    with store.transaction():
        previous = store.read() if store.manifest.exists() else None
        if previous is not None:
            store.validate_manifest(previous, verify_files=False)
        revision = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        created, temporary, directories, installed = [], [], [], {}
        record, switched = None, False
        try:
            # Prepare each content-addressed DLL once, before changing any profile.
            for name, source in prepared.items():
                info = catalog["runtimes"][name]
                digest = info["dll_sha256"]
                if digest in installed:
                    continue
                target = safe_path(store.root, f"out/dlss5/runtimes/{digest}/nvngx_dlssnr.dll")
                if target.exists():
                    if target.stat().st_size != info["dll_bytes"]:
                        raise ValueError("An existing immutable runtime has an unexpected size")
                    validate_dll(target, digest)
                else:
                    if not target.parent.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        directories.append(target.parent)
                    staged = target.with_name(target.name + "." + uuid.uuid4().hex + ".part")
                    copy_new(source, staged, temporary)
                    if staged.stat().st_size != info["dll_bytes"]:
                        raise ValueError("Staged runtime size does not match the pinned catalog")
                    validate_dll(staged, digest)
                    # Windows rename refuses an existing target; no immutable DLL is overwritten.
                    if os.name == "nt":
                        staged.rename(target)
                    else:
                        os.link(staged, target)
                        staged.unlink()
                    created.append(target)
                installed[digest] = target
            profiles = {}
            for profile in PROFILES:
                name = catalog["profiles"][profile]
                info = catalog["runtimes"][name]
                target = installed[info["dll_sha256"]]
                profiles[profile] = {"path": target.relative_to(store.root).as_posix(),
                    "sha256": info["dll_sha256"], "bytes": info["dll_bytes"], "version": info["version"],
                    "source": info["source"], "runtime": name, "kind": info.get("kind", "community"),
                    "architecture": "native", "default_nr_preset": 2,
                    "validation": "matched-not-render-tested", "installed_at": time.time(),
                    "asset": info["file"], "asset_sha256": info["artifact_sha256"],
                    "asset_bytes": info["artifact_bytes"], "url": info["url"], "api_url": info["api_url"],
                    "asset_format": info["format"]}
                for field in ("identity_source", "signed_original", "note"):
                    if field in info:
                        profiles[profile][field] = info[field]
                if previous is not None:
                    profiles[profile]["previous_revision"] = revision
            current = dict(previous or {})
            current.update(version=1, release=catalog["release"], tag=catalog["release"],
                           profiles=profiles, source=catalog.get("source", CATALOG.name),
                           note="Pinned x64 runtimes; file identity matched, rendering has not been tested.")
            current["catalog"] = {"file": CATALOG.name, "version": catalog["version"],
                                  "sha256": sha256(CATALOG), "release": catalog["release"]}
            for field in ("reviewed_at", "preset_source"):
                if field in catalog:
                    current["catalog"][field] = catalog[field]
            store.validate_manifest(current)
            if previous is not None:
                history = safe_path(store.root, "out/dlss5/component-history")
                history.mkdir(parents=True, exist_ok=True)
                record = safe_path(store.root, f"out/dlss5/component-history/{revision}.json")
                encoded = json.dumps(previous, sort_keys=True, separators=(",", ":")).encode("utf-8")
                atomic_json(record, {"format": "dlss-component-history", "version": 1,
                                     "manifest": previous, "sha256": hashlib.sha256(encoded).hexdigest()})
            atomic_json(safe_path(store.root, "dlss5-components.json"), current)
            switched = True
            return {"release": catalog["release"], "profiles": profiles,
                    "rollback_revision": revision if previous is not None else None}
        finally:
            for path in temporary:
                discard(path)
            if not switched:
                for path in reversed(created):
                    discard(path)
                if record is not None:
                    discard(record)
                for directory in reversed(directories):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass


def prepare(root=ROOT, download_only=False):
    root = Path(root).resolve()
    catalog = read_catalog()
    release_directory = catalog["release"].removesuffix(".0")
    cache = safe_path(root, f"packaging/cache/dlssnr-{release_directory}")
    cache.mkdir(parents=True, exist_ok=True)
    prepared = {}
    for name in dict.fromkeys(catalog["profiles"][profile] for profile in PROFILES):
        prepared[name] = prepare_dll(cache, name, catalog["runtimes"][name])
    if download_only:
        result = {"release": catalog["release"], "download_only": True,
                  "runtimes": {name: str(path) for name, path in prepared.items()}}
    else:
        app_root = safe_path(root, "app")
        if not app_root.is_dir():
            raise ValueError("--root must contain the application app directory")
        result = install(app_root, catalog, prepared)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository or portable package root containing app/")
    parser.add_argument("--download-only", action="store_true", help="Prepare and verify the pinned cache without changing installed profiles")
    arguments = parser.parse_args()
    try:
        prepare(arguments.root, arguments.download_only)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Runtime preparation failed: {error}\n")


if __name__ == "__main__":
    main()
