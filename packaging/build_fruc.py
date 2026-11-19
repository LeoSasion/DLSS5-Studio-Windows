"""Build the local NVIDIA Optical Flow SDK 5.0.7 adapter; never downloads or bundles the SDK."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

from build_engine import ROOT, portable_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, required=True, help='official, extracted NVIDIA Optical Flow SDK directory')
    parser.add_argument('--out', type=Path, default=ROOT / 'app/out/nvidia-fruc')
    args = parser.parse_args()
    sdk = args.sdk.resolve()
    header = next(sdk.rglob('NvOFFRUC.h'), None)
    runtime = next((p for p in sdk.rglob('NvOFFRUC.dll') if 'win64' in str(p.parent).lower()), None)
    if not header or not runtime:
        parser.error('缺少 NVIDIA 官方 5.0.7 SDK 的 NvOFFRUC.h 或 win64/NvOFFRUC.dll')
    env = {**os.environ, 'STUDIO_FRUC_DLL': str(runtime)}
    subprocess.run(['powershell.exe', '-NoProfile', '-Command',
        "$s=Get-AuthenticodeSignature -LiteralPath $env:STUDIO_FRUC_DLL; if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'NVIDIA') {exit 1}"],
        env=env, check=True)
    target = args.out.resolve()
    target.mkdir(parents=True, exist_ok=True)
    cc, env = portable_environment()
    subprocess.run([cc, '/nologo', '/EHsc', '/std:c++17', '/W3', '/O2', '/utf-8',
                    '/I' + str(header.parent), '/Fo' + str(target / 'studio-fruc.obj'),
                    '/Fe' + str(target / 'studio-fruc.exe'), str(ROOT / 'source/src/studio_fruc.cpp')],
                   cwd=target, env=env, check=True)
    shutil.copy2(runtime, target / runtime.name)
    for dll in runtime.parent.glob('cudart64*.dll'):
        shutil.copy2(dll, target / dll.name)


if __name__ == '__main__':
    main()
