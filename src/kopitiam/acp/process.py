"""Own an ACP process tree, including children created before initialization."""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
from ctypes import wintypes

import psutil


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
        ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io", _IOCounters),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
    ]


class ProcessTree:
    def __init__(self, command, *, cwd, stderr, env=None):
        self.job = None
        self.process = None
        self._close_lock = threading.Lock()
        if os.name == "nt":
            kernel = self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.job = kernel.CreateJobObjectW(None, None)
            if not self.job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits),
                                                  ctypes.sizeof(limits)):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                raise error
        try:
            self.process = subprocess.Popen(
                command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=stderr, creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            if self.job:
                if not self.kernel.AssignProcessToJobObject(self.job, int(self.process._handle)):
                    raise ctypes.WinError(ctypes.get_last_error())
                # NOTE: CREATE_SUSPENDED closes the spawn/assignment race. psutil's
                # supported resume operation resumes this process only, after job binding.
                psutil.Process(self.process.pid).resume()
        except BaseException:
            self.close()
            raise

    def close(self):
        with self._close_lock:
            self._close()

    def _close(self):
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None
        process = self.process
        if process is None:
            return
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()  # Also handles failure before job assignment.
        process.wait(timeout=10)
