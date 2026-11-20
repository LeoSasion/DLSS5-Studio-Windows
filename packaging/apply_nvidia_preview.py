"""Apply the Windows preview update inside a fresh v1.4.0 portable directory."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg-archive', type=Path, help='Use an already downloaded pinned FFmpeg ZIP')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    patch = root / 'preview-update'
    spec = json.loads((patch / 'patch.json').read_text(encoding='utf-8'))
    for name, expected in spec['base'].items():
        original = root / name
        if (not original.resolve().is_relative_to(root) or not original.is_file()
                or digest(original) not in (expected, spec['files'].get(name))):
            raise SystemExit(f'Expected a v1.4.0 portable copy or a previous run of this update: {name}')
    with tempfile.TemporaryDirectory(prefix='stage-', dir=patch) as directory:
        stage = Path(directory)
        archive = args.ffmpeg_archive
        if archive is None:
            archive = stage / 'ffmpeg.zip'
            print('Downloading the fixed LGPL FFmpeg build...', flush=True)
            with urllib.request.urlopen(spec['ffmpeg']['url'], timeout=60) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
        if digest(archive) != spec['ffmpeg']['sha256']:
            raise SystemExit('FFmpeg archive SHA-256 does not match. The portable files were not changed.')
        media = spec['ffmpeg']['paths']
        with zipfile.ZipFile(archive) as z:
            for name, member in media.items():
                with z.open(member) as source, (stage / Path(name).name).open('wb') as output:
                    shutil.copyfileobj(source, output)
        replacements = {}
        for name, expected in spec['files'].items():
            source = stage / Path(name).name if name in media else patch / 'files' / name
            target = root / name
            if (not source.resolve().is_relative_to(patch.resolve()) or not target.resolve().is_relative_to(root)
                    or not source.is_file() or digest(source) != expected):
                raise SystemExit(f'Update file did not pass verification: {name}')
            replacements[target] = source
        for target, source in replacements.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            # A failed copy or locked target must leave the previous file readable.
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                staged = Path(stream.name)
            try:
                shutil.copy2(source, staged)
                staged.replace(target)
            except PermissionError as exc:
                raise SystemExit(f'Cannot replace {target.name}. Close DLSS Studio and rerun the update.') from exc
            finally:
                staged.unlink(missing_ok=True)
    print('Preview update installed. Open DLSS Studio.exe.', flush=True)


if __name__ == '__main__':
    main()
