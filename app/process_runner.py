"""Own a render pipeline and all of its child processes, including preview encoders."""
import os
import queue
import signal
import subprocess
import threading
import time


class RenderCancelled(Exception):
    pass


class ProcessGroup:
    def __init__(self, cancelled=None, priority=0):
        self.cancelled = cancelled or threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.handle = None
        self.priority = priority
        if os.name == "nt":
            self._create_windows_job()

    def _create_windows_job(self):
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [("user", c.c_int64), ("job_user", c.c_int64), ("flags", w.DWORD),
                        ("min_ws", c.c_size_t), ("max_ws", c.c_size_t), ("active", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ("read", "write", "other", "rb", "wb", "ob")]
        class Limits(c.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("pm", c.c_size_t), ("jm", c.c_size_t),
                        ("peak_pm", c.c_size_t), ("peak_jm", c.c_size_t)]
        k = c.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        k.CloseHandle.argtypes = [w.HANDLE]
        self.kernel = k
        self.handle = k.CreateJobObjectW(None, None)
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not k.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            if self.handle:
                k.CloseHandle(self.handle)
                self.handle = None
            raise OSError("无法创建可取消的渲染进程。")

    def check(self):
        if self.cancelled.is_set():
            raise RenderCancelled()

    def start(self, args, **kwargs):
        with self.lock:
            self.check()
            kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0) | self.priority)
            if os.name != "nt":
                kwargs["start_new_session"] = True
            process = subprocess.Popen(args, **kwargs)
            self.process = process
            if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
                process.kill()
                process.wait()
                raise OSError("无法管理渲染进程，请重新启动服务。")
            return process

    def run(self, args, **kwargs):
        process = self.start(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        stdout, stderr = process.communicate()
        self.check()
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

    def stream_lines(self, process, idle_timeout=900):
        """Read progress without letting a silent broken pipeline block forever.

        The reader is bounded, and the watchdog measures received bytes rather
        than completed lines (some encoders emit CR-only progress records).
        """
        messages = queue.Queue(maxsize=256)
        end = object()
        state = {"last_output": time.monotonic()}

        def put(value):
            while not self.cancelled.is_set():
                try:
                    messages.put(value, timeout=.2)
                    return
                except queue.Full:
                    continue

        def reader():
            pending = ""
            try:
                while not self.cancelled.is_set():
                    char = process.stdout.read(1)
                    if not char:
                        break
                    state["last_output"] = time.monotonic()
                    if char in "\r\n":
                        if pending.strip():
                            put(pending)
                        pending = ""
                    else:
                        pending = (pending + char)[-8192:]
                if pending.strip():
                    put(pending)
            finally:
                put(end)

        thread = threading.Thread(target=reader, daemon=True, name="render-progress")
        thread.start()
        while True:
            self.check()
            try:
                value = messages.get(timeout=.2)
            except queue.Empty:
                if idle_timeout and time.monotonic() - state["last_output"] > idle_timeout:
                    self.cancel()
                    raise TimeoutError("渲染长时间没有进度，任务已停止；已完成的视频片段仍可继续。")
                continue
            if value is end:
                break
            yield value
        thread.join(timeout=1)
        try:
            process.wait(timeout=idle_timeout or None)
        except subprocess.TimeoutExpired as exc:
            self.cancel()
            raise TimeoutError("渲染输出已关闭但进程未退出，任务已停止；已完成片段仍保留。") from exc

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            if self.handle:
                self.kernel.TerminateJobObject(self.handle, 1)
            elif self.process is not None and self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGKILL)

    def close(self):
        with self.lock:
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
            if self.process is not None:
                if self.process.poll() is None:
                    if os.name == "nt":
                        self.process.kill()
                    else:
                        os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
                for stream in (self.process.stdout, self.process.stderr):
                    if stream:
                        stream.close()
