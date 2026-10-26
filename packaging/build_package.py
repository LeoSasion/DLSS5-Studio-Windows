"""Rebuild the offline Windows x64 ZIP from the verified workspace dependencies."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import urllib.request
import zipfile
import argparse

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "packaging" / "build"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--package", type=Path, default=BUILD / "DLSS-Studio-Portable")
PACKAGE = parser.parse_args().package.resolve()
CACHE = ROOT / "packaging" / "cache"
ARTIFACTS = ROOT / "outputs"
for path in (BUILD, CACHE, ARTIFACTS):
    path.mkdir(parents=True, exist_ok=True)
if PACKAGE.exists() and any(PACKAGE.iterdir()):
    raise SystemExit(f"Staging already exists; choose a fresh build directory: {PACKAGE}")
PACKAGE.mkdir(exist_ok=True)

embedded = CACHE / "python-3.10.11-embed-amd64.zip"
if not embedded.exists():
    urllib.request.urlretrieve("https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip", embedded)
if hashlib.md5(embedded.read_bytes()).hexdigest() != "f1c0538b060e03cbb697ab3581cb73bc":
    raise SystemExit("Python archive differs from the official published checksum")
runtime = PACKAGE / "runtime"
with zipfile.ZipFile(embedded) as archive:
    archive.extractall(runtime)
(runtime / "python310._pth").write_text("python310.zip\n.\nLib\nLib/site-packages\n../app\nimport site\n", encoding="utf-8")
ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
shutil.copytree(ROOT / "app/.venv/Lib/site-packages", runtime / "Lib/site-packages", ignore=ignore)
# Python's embedded distribution omits distutils, used by some installed packages.
base = Path(json.loads(subprocess.check_output([str(ROOT / "app/.venv/Scripts/python.exe"), "-c", "import sys,json;print(json.dumps(sys.base_prefix))"], text=True)))
shutil.copytree(base / "Lib/distutils", runtime / "Lib/distutils", ignore=ignore)
app = PACKAGE / "app"
app.mkdir()
for file in (ROOT / "app").glob("*.py"):
    if not file.name.startswith("test_") and not file.name.endswith(".original.py"):
        shutil.copy2(file, app / file.name)
# One snapshot drives both the staged manifest and its selected runtime payloads.
components = json.loads((ROOT / "app/dlss5-components.json").read_text(encoding="utf-8"))
(app / "dlss5-components.json").write_text(json.dumps(components, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
shutil.copy2(ROOT / "app/requirements-installed.txt", app / "requirements-installed.txt")
shutil.copytree(ROOT / "app/web", app / "web", ignore=ignore)
# Ship only the runtimes selected by this build. Local component history and
# superseded DLLs stay available for rollback without inflating every release.
shutil.copytree(ROOT / "app/out", app / "out",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "dlss5", "nvngx_dlssnr.dll"))
for relative in sorted({entry["path"] for entry in components["profiles"].values()}):
    source = (ROOT / "app" / relative).resolve()
    if not source.is_relative_to((ROOT / "app/out/dlss5").resolve()) or source.name != "nvngx_dlssnr.dll":
        raise ValueError("Runtime manifest contains an invalid path")
    entry = next(value for value in components["profiles"].values() if value["path"] == relative)
    if source.stat().st_size != entry["bytes"] or hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError("Runtime manifest integrity check failed")
    target = app / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
for name in ("uploads", "ui_out"):
    (app / name).mkdir()
(PACKAGE / "logs").mkdir()
licenses = PACKAGE / "licenses"
licenses.mkdir()
for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
    shutil.copy2(ROOT / name, PACKAGE / name)
shutil.copy2(ROOT / "app/README.md", licenses / "video2dlssnr-README.md")
shutil.copy2(ROOT / "source/THIRD-PARTY-engine-LICENSE.txt", licenses / "engine-MIT-LICENSE.txt")
shutil.copy2(ROOT / "packaging/README-package.txt", PACKAGE / "使用说明.txt")
tools = PACKAGE / "tools"
tools.mkdir()
for name in ("incremental_update.py", "components.py"):
    shutil.copy2(ROOT / "packaging" / name, tools / name)
(licenses / "THIRD-PARTY.txt").write_text("""Bundled third-party components retain their respective notices and terms.
Engine attribution: https://github.com/DaniilSokolyuk/video2dlssnr; DLSS Studio modifications are built from the included source.
NVIDIA NGX / DLSS binaries retain their vendor terms; component identities are recorded in app/dlss5-components.json.
Python 3.10.11 x64: https://www.python.org/downloads/release/python-31011/
Python license: ../runtime/LICENSE.txt
Python package licenses: ../runtime/Lib/site-packages/*dist-info/
Phosphor icons: ../app/web/assets/icons/LICENSE
FFmpeg license and build options: ffmpeg-license.txt
FFmpeg source project: https://ffmpeg.org/
This ZIP packages the existing project and its verified dependencies; it does not grant additional rights to third-party components.
""", encoding="utf-8")
result = subprocess.run([str(app / "out/ffmpeg.exe"), "-L"], capture_output=True, text=True, errors="replace")
(licenses / "ffmpeg-license.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
from build_launchers import build_launchers
build_launchers(PACKAGE, ROOT)
(PACKAGE / "启动工作台.bat").write_bytes('@echo off\r\ncd /d "%~dp0"\r\nstart "" "DLSS Studio.exe"\r\n'.encode("ascii"))
manifest = {}
for file in sorted(PACKAGE.rglob("*")):
    if file.is_file():
        manifest[file.relative_to(PACKAGE).as_posix()] = {"bytes":file.stat().st_size,"sha256":hashlib.sha256(file.read_bytes()).hexdigest()}
(PACKAGE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"STAGED={PACKAGE}", flush=True)
print(f"FILES={len(manifest)} SIZE_MIB={sum(x['bytes'] for x in manifest.values())/1024**2:.1f}", flush=True)
