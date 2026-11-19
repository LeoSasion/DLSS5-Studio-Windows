"""Optional official NVIDIA video effects, isolated from the app's Python environment."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import sys

RUNTIME = Path(__file__).resolve().parent / 'out/nvidia-vfx'
FRUC = Path(__file__).resolve().parent / 'out/nvidia-fruc/studio-fruc.exe'
VFX_BINARIES = ('NVVideoEffects.dll', 'nvVFXVideoSuperRes.dll', 'nvVFXVideoFrameGeneration.dll', 'nvngx_vsr.dll')
FRUC_BINARY = 'NvOFFRUC.dll'


def effect_files(effect):
    if effect == 'fruc':
        return [FRUC, FRUC.with_name(FRUC_BINARY), *sorted(FRUC.parent.glob('cudart64*.dll'))]
    return [Path(__file__), *(RUNTIME / 'nvvfx/libs' / name for name in VFX_BINARIES
                              if effect == 'vfg' or name != VFX_BINARIES[2]),
            *sorted((RUNTIME / 'nvvfx').glob('_ext*.pyd'))]


def capabilities(profile):
    libs = RUNTIME / 'nvvfx/libs'
    installed = (RUNTIME / 'nvvfx/__init__.py').is_file() and (RUNTIME / 'cupy/__init__.py').is_file()
    vsr = bool(installed and profile in ('rtx30', 'rtx40', 'rtx50')
               and all((libs / name).is_file() for name in VFX_BINARIES if 'Generation' not in name))
    return {'upscale': vsr, 'interpolation': (
        'vfg' if vsr and profile in ('rtx40', 'rtx50') and (libs / VFX_BINARIES[2]).is_file()
        else 'fruc' if profile == 'rtx30' and FRUC.is_file() and FRUC.with_name(FRUC_BINARY).is_file()
        else None)}


def cli_adapter(index):
    if not (RUNTIME.is_dir() or FRUC.is_file()):
        return None, ''
    from gpu_runtime import classify, cuda_architectures, enumerate_adapters
    adapter = next((row for row in enumerate_adapters() if not row['software'] and (index < 0 or row['index'] == index)), None)
    return (classify(adapter, cuda_architectures()), adapter['luid']) if adapter else (None, '')


def device(luid):
    from gpu_runtime import cuda_architectures
    devices = cuda_architectures(ordinals=True)
    if luid in devices:
        return devices[luid]
    if not luid and len(devices) == 1:
        return next(iter(devices.values()))
    raise RuntimeError('NVIDIA CUDA 设备和当前渲染显卡不匹配。')


def frames(source, frame_size):
    while True:
        data = source.read(frame_size)
        if not data:
            break
        if len(data) != frame_size:
            raise RuntimeError('NVIDIA 视频处理收到不完整的原始帧。')
        yield data


def run(args):
    sys.path.insert(0, str(RUNTIME))
    import numpy as np
    import cupy as cp
    import nvvfx

    width, height = args.width, args.height
    high = args.pixel == 'rgba64le'
    frame_size = width * height * (8 if high else 4)
    dst = sys.stdout.buffer
    cuda = device(args.luid)
    cp.cuda.Device(cuda).use()

    def transfer(image):
        if high:
            rgb = (np.frombuffer(image, '<u2').reshape(height, width, 4)[:, :, :3].astype(np.uint32) + 32).clip(max=65535) >> 6
            return cp.asarray((3 << 30) | (rgb[:, :, 2] << 20) | (rgb[:, :, 1] << 10) | rgb[:, :, 0])
        rgb = np.frombuffer(image, np.uint8).reshape(height, width, 4)[:, :, :3]
        return cp.asarray(np.ascontiguousarray(rgb.transpose(2, 0, 1), dtype=np.float32) / 255)

    def output(result, ow, oh):
        # The effect owns its DLPack output. Read it before issuing another run.
        cp.cuda.runtime.deviceSynchronize()
        array = cp.fromDlpack(result.image).get()
        rgba = np.empty((oh, ow, 4), dtype='<u2' if high else np.uint8)
        if high:
            for i in range(3):
                channel = (array >> (10 * i)) & 1023
                rgba[:, :, i] = (channel << 6) | (channel >> 4)
            rgba[:, :, 3] = 65535
        else:
            rgba[:, :, :3] = np.rint(array.transpose(1, 2, 0).clip(0, 1) * 255)
            rgba[:, :, 3] = 255
        return rgba.tobytes()

    count = 0
    with ExitStack() as stack:
        if args.effect == 'vsr':
            cls = nvvfx.VideoSuperRes
            effect = stack.enter_context(cls(quality=cls.QualityLevel.HIGH, device=cuda,
                                             image_encoding=cls.ImageEncoding.RGB10A2 if high else cls.ImageEncoding.RGB8))
            effect.output_width, effect.output_height = args.target
            effect.load()
        else:
            cls = nvvfx.VideoFrameGeneration
            effect = stack.enter_context(cls(mode=cls.Mode.HIGH, device=cuda,
                                             image_encoding=cls.ImageEncoding.RGB10A2 if high else cls.ImageEncoding.RGB8))
            effect.input_width, effect.input_height, effect.frame_multiplier = width, height, 2
            effect.load()
        previous, previous_bytes = None, None
        for count, image in enumerate(frames(sys.stdin.buffer, frame_size), 1):
            current = transfer(image)
            if args.effect == 'vsr':
                dst.write(output(effect.run(current), *args.target))
            else:
                if previous is not None:
                    mid = output(effect.run(previous, current, frame_index=1), width, height)
                    # VFG returns the current frame when it detects a cut. Keep the cut at its original PTS.
                    dst.write(previous_bytes if mid == image else mid)
                dst.write(image)
                previous, previous_bytes = current, image
            if count == 1 or count % 8 == 0:
                print(f'VFXPROG {count}', file=sys.stderr, flush=True)
        if args.effect == 'vfg' and previous_bytes is not None:
            dst.write(previous_bytes)  # Hold the last half-frame so clip duration is unchanged.
    if not count:
        raise RuntimeError('NVIDIA 视频处理未收到帧。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('effect', choices=('vsr', 'vfg'))
    parser.add_argument('width', type=int)
    parser.add_argument('height', type=int)
    parser.add_argument('pixel', choices=('rgba', 'rgba64le'))
    parser.add_argument('luid')
    parser.add_argument('target', nargs='*', type=int)
    run(parser.parse_args())
