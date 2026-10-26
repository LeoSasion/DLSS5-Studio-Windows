"""Download and extract build-only Microsoft/NVIDIA files inside this workspace.

No installer, registry edit, administrator elevation, or system PATH change is used.
Vendor terms remain applicable. The toolchain is not part of the application package.
"""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "packaging/cache/portable-msvc"


def read_url(url):
    request = urllib.request.Request(url, headers={"User-Agent": "DLSS-Studio-Build/1"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def fetch(payload):
    name = payload["fileName"]
    if Path(name).name != name:
        raise ValueError("Invalid package name")
    target = CACHE / name
    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != payload["sha256"]:
        data = read_url(payload["url"])
        if hashlib.sha256(data).hexdigest() != payload["sha256"]:
            raise ValueError("Package hash mismatch: " + name)
        target.write_bytes(data)
    destination = CACHE / ("sdk" if target.suffix == ".nupkg" else "msvc")
    with zipfile.ZipFile(target) as archive:
        for entry in archive.infolist():
            relative = entry.filename
            if target.suffix == ".vsix":
                if not relative.startswith("Contents/"):
                    continue
                relative = relative[len("Contents/"):]
            output = (destination / relative).resolve()
            if not output.is_relative_to(destination.resolve()):
                raise ValueError("Unsafe archive path")
            if entry.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(archive.read(entry))
    return {"file": name, "sha256": payload["sha256"], "bytes": target.stat().st_size, "url": payload["url"]}


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    manifest_path = CACHE / "vsmanifest.json"
    if not manifest_path.exists():
        channel = json.loads(read_url("https://aka.ms/vs/17/release/channel"))
        item = next(item for item in channel["channelItems"] if item["id"] == "Microsoft.VisualStudio.Manifests.VisualStudio")
        manifest_path.write_bytes(read_url(item["payloads"][0]["url"]))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    suffixes = ("CRT.Headers.base", "CRT.x64.Desktop.base", "CRT.x64.OneCore.Desktop.base",
                "Tools.HostX64.TargetX64.base", "Tools.HostX64.TargetX64.Res.base")
    packages = []
    for suffix in suffixes:
        name = "Microsoft.VC.14.44.17.14." + suffix
        item = next(item for item in manifest["packages"] if item["id"] == name and item.get("language", "en-US") == "en-US")
        packages.extend(item["payloads"])
    for name, sha in (
        ("microsoft.windows.sdk.cpp", "21dfb5db3397425ee428e0fd54cedc51af52ddd10c82433a7cdb483ecbb284ad"),
        ("microsoft.windows.sdk.cpp.x64", "ce31d6d47c1333c79501814538ad5f9dbdfb78ac0123fb0ec7978fccbc6a7e82")):
        version = "10.0.26100.3916"
        file = name + "." + version + ".nupkg"
        packages.append({"fileName": file, "sha256": sha,
                         "url": f"https://api.nuget.org/v3-flatcontainer/{name}/{version}/{file}"})
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        records = list(pool.map(fetch, packages))
    (CACHE / "download-manifest.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    ngx = ROOT / "source/third_party/nvngx/lib/nvsdk_ngx_d.lib"
    sha = "bc43df9ec032004fde681d3e3afd01cde239e03a0125b822e88674c84287f99c"
    if not ngx.exists() or hashlib.sha256(ngx.read_bytes()).hexdigest() != sha:
        data = read_url("https://raw.githubusercontent.com/NVIDIA/DLSS/v310.9.16/lib/Windows_x86_64/x64/nvsdk_ngx_d.lib")
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("NVIDIA NGX library hash mismatch")
        ngx.parent.mkdir(parents=True, exist_ok=True)
        ngx.write_bytes(data)
    print("Portable build dependencies verified. Run packaging/build_engine.py --tests.")


if __name__ == "__main__":
    main()
