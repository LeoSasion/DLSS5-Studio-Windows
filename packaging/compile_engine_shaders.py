"""Compile embedded HLSL without creating a GPU device or executing shaders."""
import ctypes
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def compile_shaders():
    compiler = ctypes.WinDLL("d3dcompiler_47.dll")
    compiler.D3DCompile.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
        ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
    compiler.D3DCompile.restype = ctypes.c_long

    def method(blob, index, result, *args):
        table = ctypes.cast(blob, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *args)(table[index])

    for name in ("nr_gpu_post_shader.h", "optflow_nvof.cpp"):
        text = (ROOT / "source/src" / name).read_text(encoding="utf-8")
        for number, match in enumerate(re.finditer(r'R"([^()]*)\((.*?)\)\1"', text, re.S)):
            code = match[2].encode("utf-8")
            entries = ("Prepare", "Correction", "Stabilize", "Compose") if name.endswith(".h") else ("CSMain",)
            for entry in entries:
                shader, errors = ctypes.c_void_p(), ctypes.c_void_p()
                try:
                    hr = compiler.D3DCompile(code, len(code), name.encode(), None, None,
                        entry.encode(), b"cs_5_0", 1 << 15, 0, ctypes.byref(shader), ctypes.byref(errors))
                    if hr < 0:
                        message = "HLSL compile failed"
                        if errors.value:
                            pointer = method(errors, 3, ctypes.c_void_p)(errors)
                            message = ctypes.string_at(pointer).decode("utf-8", "replace")
                        raise RuntimeError(f"{name}:{entry}: {message}")
                    print(f"Compiled HLSL {name}:{number}:{entry}", flush=True)
                finally:
                    for blob in (shader, errors):
                        if blob.value:
                            method(blob, 2, ctypes.c_ulong)(blob)


if __name__ == "__main__":
    compile_shaders()
