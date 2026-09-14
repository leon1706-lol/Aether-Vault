"""Process RSS probes for `/api/metrics`. A deliberately slim twin of `av_cli.sysres` --
the server package has never depended on `av_cli` (see `server.py`'s lifespan comment) and
this is not the place to start; `tests/test_sysres.py` pins the two copies to each other.
"""
from __future__ import annotations

import os
import sys

_MIB = 1024 * 1024


def _win_memory() -> tuple[int, int] | None:
    import ctypes
    from ctypes import wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    # Pointer-sized argtypes are mandatory: the pseudo-handle is all bits set and gets
    # zero-extended (→ ERROR_INVALID_HANDLE) without them.
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD,
    ]
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)


def _proc_status_bytes(key: str) -> int | None:
    with open("/proc/self/status", encoding="ascii") as f:
        for line in f:
            if line.startswith(key + ":"):
                return int(line.split()[1]) * 1024
    return None


def current_rss_bytes() -> int | None:
    try:
        if sys.platform == "win32":
            mem = _win_memory()
            return mem[0] if mem else None
        if sys.platform == "darwin":
            import resource

            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        with open("/proc/self/statm", encoding="ascii") as f:
            resident_pages = int(f.read().split()[1])
        page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        return resident_pages * page_size
    except Exception:
        return None


def peak_rss_bytes() -> int | None:
    try:
        if sys.platform == "win32":
            mem = _win_memory()
            return mem[1] if mem else None
        if sys.platform == "darwin":
            import resource

            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return _proc_status_bytes("VmHWM")
    except Exception:
        return None
