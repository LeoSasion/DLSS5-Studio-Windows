"""Build the actual NR engine, including from an extracted portable MSVC toolchain."""
import argparse
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def portable_environment():
    base = ROOT / "packaging/cache/portable-msvc"
    candidates = sorted((base / "msvc/VC/Tools/MSVC").glob("*"), reverse=True)
    sdk_versions = sorted((base / "sdk/c/Include").glob("*"), reverse=True)
    if not candidates or not sdk_versions:
        raise RuntimeError("缺少便携 MSVC/Windows SDK。先运行 packaging/bootstrap_compiler.py。")
    vc, sdk = candidates[0], base / "sdk/c"
    version = sdk_versions[0].name
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(vc / "bin/Hostx64/x64"),
                                    str(sdk / "bin" / version / "x64"), env.get("PATH", "")])
    env["INCLUDE"] = os.pathsep.join(str(p) for p in [vc / "include"] +
                                     [sdk / "Include" / version / name for name in ("ucrt", "shared", "um", "winrt")])
    env["LIB"] = os.pathsep.join(str(p) for p in (
        vc / "lib/x64", vc / "lib/onecore/x64", sdk / "ucrt/x64", sdk / "um/x64"))
    return str(vc / "bin/Hostx64/x64/cl.exe"), env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "source/out")
    args = parser.parse_args()
    compiler, env = portable_environment()
    source = ROOT / "source"
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    build = source / "build/portable"
    build.mkdir(parents=True, exist_ok=True)
    includes = [source / p for p in ("third_party/nvngx/include", "third_party/stb",
                                     "third_party/nvof", "src", "forwarder")]
    flags = ["/nologo", "/std:c++17", "/EHsc", "/W3", "/O2", "/MD", "/DNDEBUG",
             "/utf-8", "/D_CRT_SECURE_NO_WARNINGS"]
    include_flags = ["/I" + str(path) for path in includes]
    shared = [source / "src" / (name + ".cpp") for name in (
        "common", "image", "gpu", "dlss", "cli", "pipeline", "nr", "optflow", "optflow_nvof", "slprobe")]
    ngx = source / "third_party/nvngx/lib/nvsdk_ngx_d.lib"
    if not ngx.is_file():
        raise RuntimeError("缺少 NVIDIA NGX import library。先运行 packaging/bootstrap_compiler.py。")
    libraries = [str(ngx), "d3d12.lib", "dxgi.lib", "d3dcompiler.lib", "dxguid.lib",
                 "advapi32.lib", "user32.lib", "version.lib", "shell32.lib"]

    def compile_target(name, sources, extra=None, link=None):
        obj = build / name
        obj.mkdir(exist_ok=True)
        command = [compiler] + flags + include_flags + (extra or []) + [
            "/Fo" + str(obj) + os.sep, "/Fe" + str(out / name)] + [str(p) for p in sources] + ["/link"] + (link or [])
        print("Building " + name, flush=True)
        subprocess.run(command, cwd=source, env=env, check=True)

    compile_target("video2dlssnr.exe", shared + [source / "src/main.cpp"], link=libraries)
    compile_target("nvngx.dll_dlssnr.dll", [source / "forwarder/nvngx_fwd.cpp"],
                   extra=["/LD"], link=["kernel32.lib"])
    if args.tests:
        post_test = source / "tests/nr_post_tests.cpp"
        if post_test.is_file():
            compile_target("nr_post_tests.exe", [post_test], link=[])
            subprocess.run([str(out / "nr_post_tests.exe")], cwd=source, check=True, env=env)
        compile_target("video2dlssnr_tests.exe", shared + [source / "tests/test_main.cpp"], link=libraries)
        subprocess.run([str(out / "video2dlssnr_tests.exe")], cwd=source, check=True, env=env)
    print("Built engine and forwarder in " + str(out), flush=True)


if __name__ == "__main__":
    main()
