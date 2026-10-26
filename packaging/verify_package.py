"""Extract the delivered ZIP to a new Unicode path and exercise its EXE and engine."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import socket
import time
import zipfile
import httpx
import argparse

ROOT = Path(__file__).resolve().parent.parent
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--archive",type=Path,default=ROOT/"outputs/DLSS-Studio-Windows-x64.zip")
parser.add_argument("--report",type=Path,default=ROOT/"outputs/package-verification.json")
args=parser.parse_args()
archive = args.archive.resolve()
validation = ROOT / ("packaging/validation/解压 测试 " + time.strftime("%H%M%S"))
validation.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(archive) as z:
    roots={Path(info.filename).parts[0] for info in z.infolist() if info.filename}
    assert len(roots)==1,"Archive must contain one application directory"
    package_name=next(iter(roots))
    for info in z.infolist():
        assert (validation/info.filename).resolve().is_relative_to(validation.resolve()),info.filename
    z.extractall(validation)
package = validation / package_name
manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
for name, entry in manifest.items():
    data = (package / name).read_bytes()
    assert len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"], name
print("PASS: extracted ZIP and checked every payload checksum", flush=True)
# QA fixtures are added only after validating the delivered manifest.
for shape in ("landscape","portrait"):
    import shutil
    shutil.copy2(ROOT/f"tests/fixtures/preview-4k-{shape}.mp4",package/f"app/web/test-preview-4k-{shape}.mp4")
env = os.environ.copy()
env["DLSS_STUDIO_TEST_VIDEO"] = str(ROOT / "tests/fixtures/video-compare.mp4")
env["DLSS_STUDIO_TEST_WORKBENCH"] = str(ROOT / "tests/workbench-workflow.js")
for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
    env.pop(key, None)
env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
runtime_result = subprocess.check_output([str(package / "runtime/python.exe"), "-B", "-c",
    "import sys,json;import PIL,numpy,uvicorn,studio_server;"
    "assert not any(m=='gradio' or m.startswith('gradio.') for m in sys.modules);"
    "print(json.dumps({'paths':sys.path,'python':sys.executable,'gradio_loaded':False}))"],
    env=env, cwd=validation, text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
runtime = json.loads(runtime_result.strip().splitlines()[-1])
assert all(Path(p).is_relative_to(package) for p in runtime["paths"]), runtime
print("PASS: bundled Python imports dependencies without host Python or PATH", flush=True)
# Occupy the default port to verify that this copy selects another port safely.
reservation = socket.socket()
try:
    reservation.bind(("127.0.0.1",7860))
    reservation.listen(1)
except OSError:
    reservation.close()
    reservation=None
exe = subprocess.Popen([str(package / "DLSS Studio.exe"), "--smoke-test", "--hold"],
    cwd=validation, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
report = {"runtime": runtime, "payload_files":len(manifest)}
try:
    ready = package / "smoke-ready.txt"
    for _ in range(150):
        if ready.exists():
            break
        if exe.poll() is not None:
            raise RuntimeError((package / "smoke-result.txt").read_text(encoding="utf-8"))
        time.sleep(1)
    else:
        raise TimeoutError("EXE did not start its service")
    url = ready.read_text(encoding="utf-8-sig").strip()
    assert ":7860/" not in url, "Default-port collision was not avoided"
    print("PASS: actual EXE launched portable service at " + url, flush=True)
    client = httpx.Client(base_url=url, timeout=60, trust_env=False)
    health = client.get("api/health").json()
    assert health["app"] == "dlss-studio"
    assert client.get("").status_code == 200
    for name in ("studio.css", "dark-polish.css", "glass-mode.css", "studio.js", "history.js",
                 "preview-zoom.js", "video-player.js", "workbench.css", "assets/sample-lake.png"):
        assert client.get(name).status_code == 200, name
    options = client.get("api/options").json()
    assert "sr_presets" not in options
    assert options["hardware"]["ready"] and options["hardware"]["model"] == "DLSS 5", options
    report["hardware"] = options["hardware"]
    report["url"] = url
    report["nvenc_available"] = options["nvenc_available"]
    # Real renderer jobs, not mocked responses.
    def run_job(file, mime, kind, extra):
        with file.open("rb") as f:
            response = client.post("api/upload", files={"file":(file.name,f,mime)})
        response.raise_for_status()
        item=response.json()
        assert item["kind"] == kind
        response=client.post("api/jobs", json={"asset_id":item["id"],"size":"1080p (1920×1080)",**extra})
        response.raise_for_status()
        jid=response.json()["id"]
        for _ in range(100):
            job=client.get("api/jobs/"+jid).json()
            if job["status"] not in ("queued","running"):
                break
            time.sleep(1)
        assert job["status"]=="done", job
        assert job["hardware"]["status"] == "verified", job
        assert options["hardware"]["profile"] in job["log"], job
        result=client.get(job["download"])
        assert result.status_code==200 and len(result.content)>100
        if job.get("preview"):
            assert client.get(job["preview"]).status_code==200
        print("PASS: real " + kind + " enhancement and download", flush=True)
        return {"job":jid,"download_bytes":len(result.content),"status":job["status"]}
    report["image"]=run_job(package / "app/web/assets/sample-lake.png", "image/png", "image", {})
    report["video"]=run_job(ROOT / "tests/fixtures/test-video.mp4", "video/mp4", "video", {"codec":"prores","container":"mov"})
    assert not client.get("api/health").json()["busy"]
    client.close()
finally:
    if reservation is not None:
        reservation.close()
    (package / "smoke-release.txt").write_text("release",encoding="ascii")
    exe.wait(timeout=30)
    if exe.returncode != 0:
        raise RuntimeError((package / "smoke-result.txt").read_text(encoding="utf-8"))
report["launcher"]=(package / "smoke-result.txt").read_text(encoding="utf-8-sig")
assert not (package / "WebUI启动器.exe").exists()
desktop = subprocess.Popen([str(package / "DLSS Studio.exe"), "--desktop-smoke-test"],
    cwd=validation, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
desktop.wait(timeout=420)
desktop_result=(package / "desktop-smoke-result.txt").read_text(encoding="utf-8-sig")
assert desktop.returncode==0 and desktop_result.startswith("PASS:"),desktop_result
import re
desktop_port=int(re.search(r"service_port=(\d+)",desktop_result).group(1))
try:
    response=httpx.get(f"http://127.0.0.1:{desktop_port}/api/health",timeout=1,trust_env=False)
except httpx.RequestError:
    pass
else:
    raise AssertionError("Desktop service still running after window closed")
report["desktop"]=desktop_result+"; verified service stopped after close"
report["desktop_preview"]=str(package / "desktop-result.png")
report["desktop_video"] = json.loads((package / "desktop-video-diagnostics.json").read_text(encoding="utf-8-sig"))
report["desktop_workbench"] = json.loads((package / "desktop-workbench-diagnostics.json").read_text(encoding="utf-8-sig"))
assert report["desktop_workbench"]["done"] and not report["desktop_workbench"]["error"], report["desktop_workbench"]
print(report["desktop"],flush=True)
args.report.resolve().write_text(json.dumps(report, ensure_ascii=False, indent=2),encoding="utf-8")
print(report["launcher"],flush=True)
