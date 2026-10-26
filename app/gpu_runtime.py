"""Select an NR DLL and the exact DXGI adapter without changing shared DLLs."""
import ctypes as C
import hashlib
import json
import re
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from runtime_identity import RuntimeIdentity


def enumerate_adapters():
    """Match video2dlssnr's IDXGIFactory6 HIGH_PERFORMANCE enumeration order."""
    if sys.platform != 'win32':
        raise RuntimeError('自动显卡识别需要 Windows 10/11。')

    class Guid(C.Structure):
        _fields_ = [('data', C.c_ubyte * 16)]

    class Luid(C.Structure):
        _fields_ = [('low', C.c_uint32), ('high', C.c_int32)]

    class Desc(C.Structure):
        _fields_ = [('name', C.c_wchar * 128), ('vendor', C.c_uint32), ('device', C.c_uint32),
                    ('subsystem', C.c_uint32), ('revision', C.c_uint32),
                    ('vram', C.c_size_t), ('system', C.c_size_t), ('shared', C.c_size_t),
                    ('luid', Luid), ('flags', C.c_uint32)]

    def guid(value):
        return Guid.from_buffer_copy(uuid.UUID(value).bytes_le)

    def method(obj, slot, result, *args):
        table = C.cast(obj, C.POINTER(C.POINTER(C.c_void_p))).contents
        return C.WINFUNCTYPE(result, C.c_void_p, *args)(table[slot])

    dxgi = C.WinDLL('dxgi.dll', winmode=0x800)  # System32 only
    create = dxgi.CreateDXGIFactory1
    create.argtypes = [C.POINTER(Guid), C.POINTER(C.c_void_p)]
    create.restype = C.c_int32
    factory = C.c_void_p()
    iid = guid('c1b6694f-ff09-44a9-b03c-77900a0a1d17')  # IDXGIFactory6
    if create(C.byref(iid), C.byref(factory)) < 0:
        raise RuntimeError('无法读取 DXGI 显卡列表，请检查显卡驱动。')
    result = []
    adapter_iid = guid('29038f61-3839-4626-91fd-086879011a05')
    try:
        enum = method(factory, 29, C.c_int32, C.c_uint32, C.c_uint32, C.POINTER(Guid), C.POINTER(C.c_void_p))
        for index in range(64):
            adapter = C.c_void_p()
            hr = enum(factory, index, 2, C.byref(adapter_iid), C.byref(adapter))
            if hr & 0xffffffff == 0x887a0002:  # DXGI_ERROR_NOT_FOUND
                break
            if hr < 0:
                raise RuntimeError('DXGI 显卡枚举失败。')
            try:
                desc = Desc()
                if method(adapter, 10, C.c_int32, C.POINTER(Desc))(adapter, C.byref(desc)) < 0:
                    raise RuntimeError('无法读取显卡信息。')
                result.append({'index': index, 'name': desc.name, 'vendor': desc.vendor,
                               'device': desc.device, 'software': bool(desc.flags & 2),
                               'vram_mb': desc.vram // 1024**2, 'luid': bytes(desc.luid).hex()})
            finally:
                method(adapter, 2, C.c_uint32)(adapter)
    finally:
        method(factory, 2, C.c_uint32)(factory)
    return result


def cuda_architectures():
    """Use adapter LUID, never confuse CUDA / nvidia-smi indices with DXGI indices."""
    result = {}
    try:
        cuda = C.WinDLL('nvcuda.dll', winmode=0x800)
        def bind(name, args):
            fn = getattr(cuda, name); fn.argtypes = args; fn.restype = C.c_int
            return fn
        init = bind('cuInit', [C.c_uint])
        count_fn = bind('cuDeviceGetCount', [C.POINTER(C.c_int)])
        device_fn = bind('cuDeviceGet', [C.POINTER(C.c_int), C.c_int])
        luid_fn = bind('cuDeviceGetLuid', [C.c_void_p, C.POINTER(C.c_uint), C.c_int])
        attr = bind('cuDeviceGetAttribute', [C.POINTER(C.c_int), C.c_int, C.c_int])
        count = C.c_int()
        if init(0) or count_fn(C.byref(count)):
            return result
        for index in range(count.value):
            device, major, minor, mask = C.c_int(), C.c_int(), C.c_int(), C.c_uint()
            luid = C.create_string_buffer(8)
            if (device_fn(C.byref(device), index) or luid_fn(luid, C.byref(mask), device.value)
                    or attr(C.byref(major), 75, device.value) or attr(C.byref(minor), 76, device.value)):
                continue
            result[luid.raw.hex()] = (major.value, minor.value)
    except (OSError, AttributeError):
        pass
    return result


def classify(adapter, architectures):
    if adapter.get('software') or adapter['vendor'] != 0x10de:
        return None
    cc = architectures.get(adapter['luid'])
    if cc is not None:
        # Desktop RTX generations. Do not assign data-center / future chips by major version.
        return {(8, 6): 'rtx30', (8, 9): 'rtx40', (12, 0): 'rtx50'}.get(tuple(cc))
    name = adapter['name']
    match = re.search(r'\b(?:GeForce\s+)?RTX\s+(30|40|50)\d{2}\b', name, re.I)
    if match:
        return 'rtx' + match.group(1)
    if re.search(r'\bRTX\b.*\bBlackwell\b', name, re.I):
        return 'rtx50'
    if re.search(r'\bRTX\b.*\bAda\b', name, re.I):
        return 'rtx40'
    if re.search(r'\bRTX\s+A(?:2000|4000|4500|5000|5500|6000)\b', name, re.I):
        return 'rtx30'
    return None


@dataclass(frozen=True)
class Selection:
    adapter: int
    name: str
    luid: str
    profile: str
    dll_dir: Path
    sha256: str
    nr_preset: int = 0

    def args(self):
        return ['--adapter', str(self.adapter), '--dll-dir', str(self.dll_dir), '--nr-sr-preset', 'default']


class GpuRuntime:
    def __init__(self, root, enumerate_fn=enumerate_adapters, architecture_fn=cuda_architectures):
        self.root = Path(root).resolve()
        self.enumerate_fn = enumerate_fn
        self.architecture_fn = architecture_fn
        self.lock = threading.RLock()
        self.selection = None
        self.verified = set()
        self.continuous_verified = set()
        self.checked = {}
        self.error = '正在识别显卡…'
        self.identities = RuntimeIdentity(self.root)
        self.refresh()

    def refresh(self):
        with self.lock:
            try:
                adapters = self.enumerate_fn()
                architectures = self.architecture_fn()
                candidates = [(a, classify(a, architectures)) for a in adapters]
                chosen = next(((a, p) for a, p in candidates if p), None)
                if not chosen:
                    names = '、'.join(a['name'] for a in adapters if not a.get('software')) or '未检测到显卡'
                    raise RuntimeError('未找到可匹配的 RTX 30/40/50 系显卡：' + names)
                adapter, profile = chosen
                manifest = json.loads((self.root / 'dlss5-components.json').read_text(encoding='utf-8'))
                info = manifest['profiles'][profile]
                dll = (self.root / info['path']).resolve()
                if not dll.is_relative_to(self.root / 'out/dlss5') or dll.name != 'nvngx_dlssnr.dll':
                    raise RuntimeError('DLSS 5 组件清单路径无效。')
                stat = dll.stat()
                key = (str(dll), stat.st_size, stat.st_mtime_ns, info['sha256'])
                if key not in self.checked:
                    if stat.st_size != info['bytes'] or hashlib.sha256(dll.read_bytes()).hexdigest() != info['sha256']:
                        raise RuntimeError('DLSS 5 组件校验失败，请重新解压完整部署包。')
                    self.checked[key] = True
                nr_preset = info.get('default_nr_preset', 0)
                if type(nr_preset) is not int or nr_preset not in (0, 1, 2):
                    raise RuntimeError('DLSS 5 组件默认网络配置无效。')
                self.selection = Selection(adapter['index'], adapter['name'], adapter['luid'], profile, dll.parent, info['sha256'], nr_preset)
                self.error = None
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                self.selection = None
                self.error = str(exc) if isinstance(exc, RuntimeError) else '显卡组件缺失或配置无效，请重新解压完整部署包。'
            return self.public()

    def prepare(self):
        # Refresh before every job: hotplug can change DXGI preference indices.
        with self.lock:
            self.refresh()
            if not self.selection:
                raise RuntimeError(self.error)
            return self.selection

    def mark_verified(self, selection):
        with self.lock:
            self.verified.add(self.identity(selection)['digest'])

    def mark_continuous_verified(self, selection):
        with self.lock:
            digest = self.identity(selection)['digest']
            self.verified.add(digest)
            self.continuous_verified.add(digest)

    def identity(self, selection=None):
        with self.lock:
            selected = selection or self.selection
            if not selected:
                return {'identity_schema': 1, 'available': False, 'reason': self.error}
            return self.identities.build(selected)

    def public(self):
        with self.lock:
            selected = self.selection
            identity = self.identity(selected) if selected else None
            verified = bool(identity and identity.get('digest') in self.verified)
            return {'model': 'DLSS 5', 'ready': bool(selected),
                    'gpu_name': selected.name if selected else None,
                    'profile': selected.profile if selected else None,
                    'runtime_digest': identity.get('digest') if identity else None,
                    'driver_version': next((d.get('version') for d in identity.get('driver', []) if d.get('version')), None) if identity else None,
                    'capabilities': {**self.identities.capabilities(), 'continuous_validation': bool(identity and identity.get('digest') in self.continuous_verified), 'identity_schema': 1},
                    'status': 'verified' if verified else 'matched' if selected else 'unavailable',
                    'message': '本次启动已成功渲染' if verified else '已自动匹配，等待首次处理验证' if selected else self.error}
