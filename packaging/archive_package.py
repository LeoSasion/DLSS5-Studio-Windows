from pathlib import Path
import hashlib
import json
import zipfile
import argparse

root = Path(__file__).resolve().parent.parent
parser=argparse.ArgumentParser()
parser.add_argument("--package",type=Path,default=root/"packaging/build/DLSS-Studio-Portable")
parser.add_argument("--output",type=Path,default=root/"outputs/DLSS-Studio-Windows-x64.zip")
args=parser.parse_args()
package=args.package.resolve()
manifest = {}
for file in sorted(package.rglob("*")):
    if file.is_file() and file.name != "manifest.json":
        name = file.relative_to(package).as_posix()
        if any(part in {"__pycache__", "uploads", "ui_out", "logs", "desktop-profile", "desktop-smoke-profile", "updates"} for part in file.relative_to(package).parts):
            continue
        if file.name in {"studio-state.json", "studio-state.json.tmp"} or file.name.endswith((".previous", ".bak", ".log", ".pid")):
            continue
        if file.parent==package and (file.name.startswith(("desktop-","smoke-")) or file.name=="launcher-preview.png"):
            continue
        if name.startswith("app/web/test-preview-4k-"):
            continue
        manifest[name] = {"bytes":file.stat().st_size,"sha256":hashlib.sha256(file.read_bytes()).hexdigest()}
(package / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
target = args.output.resolve()
target.parent.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(target,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
    for name in sorted([*manifest,"manifest.json"]):
        archive.write(package/name,Path(package.name)/name)
digest=hashlib.sha256(target.read_bytes()).hexdigest()
(target.parent / (target.name+".sha256")).write_text(digest+"  "+target.name+"\n",encoding="ascii")
print(json.dumps({"zip":str(target),"bytes":target.stat().st_size,"sha256":digest,"payload_files":len(manifest)}),flush=True)
