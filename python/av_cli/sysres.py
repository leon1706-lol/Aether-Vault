"""Dependency-free (no psutil) process/system memory probes shared by `av daemon status`,
`av doctor --resources`, the staging worker cap, the benchmarks, and the RSS scoreboard.

Module scope imports only `os`/`sys` on purpose: `daemon.py` and `core.py` pull this in on
hot paths, and `tests/test_import_graph.py` pins what those paths may load.

Every probe returns None (never raises, never guesses) when the platform path isn't
available, so callers degrade to "unknown" instead of a wrong number.
"""
from __future__ import annotations

import os
import sys

_MIB = 1024 * 1024


# ---------------------------------------------------------------------------
# Windows helpers (ctypes bound lazily; all argtypes explicit -- see Probleme.md #170)
# ---------------------------------------------------------------------------

def _win_memory_counters_struct():
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

    return _ProcessMemoryCounters


def _win_process_memory(handle) -> tuple[float, float] | None:
    """(current_mb, peak_mb) for an open process handle, or None."""
    import ctypes
    from ctypes import wintypes

    struct = _win_memory_counters_struct()
    psapi = ctypes.windll.psapi
    # Without explicit pointer-sized argtypes the pseudo-handle from GetCurrentProcess()
    # (all bits set) is zero-extended instead of sign-extended and every call fails with
    # ERROR_INVALID_HANDLE -- a real bug this once shipped with.
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(struct), wintypes.DWORD]
    counters = struct()
    counters.cb = ctypes.sizeof(struct)
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return None
    return counters.WorkingSetSize / _MIB, counters.PeakWorkingSetSize / _MIB


def _win_self_memory() -> tuple[float, float] | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    return _win_process_memory(kernel32.GetCurrentProcess())


def _win_open_process(pid: int):
    """PROCESS_QUERY_LIMITED_INFORMATION handle for `pid`, or None. The caller must
    CloseHandle it; while it is open, the peak working set stays readable after exit."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    return handle or None


def _win_close_handle(handle) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle(handle)


def _win_descendants(root_pid: int) -> list[int]:
    """Every live descendant of `root_pid` (Toolhelp32 snapshot walk). The pip console
    script and the native launcher both re-exec the real Python CLI as a child, so a
    parent-only probe would report a 4 MB stub and miss the process that matters."""
    import ctypes
    from ctypes import wintypes

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32First.restype = wintypes.BOOL
    kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    kernel32.Process32Next.restype = wintypes.BOOL
    kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    parents: dict[int, int] = {}
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        ok = kernel32.Process32First(snap, ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32Next(snap, ctypes.byref(entry))
    finally:
        _win_close_handle(snap)
    out: list[int] = []
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, ppid in parents.items():
            if ppid == parent and pid != parent and pid not in out:
                out.append(pid)
                frontier.append(pid)
    return out


def _posix_descendants(root_pid: int) -> list[int]:
    parents: dict[int, int] = {}
    if sys.platform == "darwin":
        import subprocess

        result = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                parents[int(parts[0])] = int(parts[1])
    else:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", encoding="ascii") as f:
                    stat = f.read()
                ppid = int(stat[stat.rindex(")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            parents[int(name)] = ppid
    out: list[int] = []
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, ppid in parents.items():
            if ppid == parent and pid != parent and pid not in out:
                out.append(pid)
                frontier.append(pid)
    return out


def _posix_current_rss_mb(pid: int) -> float | None:
    if sys.platform == "darwin":
        return _darwin_rss_mb(pid)
    return _proc_status_kb(pid, "VmRSS")


def _win_global_memory() -> tuple[float, float] | None:
    """(total_mb, available_mb) via GlobalMemoryStatusEx."""
    import ctypes

    class _MEMSTATUS(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MEMSTATUS()
    status.dwLength = ctypes.sizeof(_MEMSTATUS)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status.ullTotalPhys / _MIB, status.ullAvailPhys / _MIB


# ---------------------------------------------------------------------------
# Linux / macOS helpers
# ---------------------------------------------------------------------------

def _proc_status_kb(pid: int | str, key: str) -> float | None:
    """One `Key:  N kB` line of /proc/<pid>/status as MiB."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as f:
            for line in f:
                if line.startswith(key + ":"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _linux_self_rss_mb() -> float | None:
    with open("/proc/self/statm", encoding="ascii") as f:
        resident_pages = int(f.read().split()[1])
    page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    return resident_pages * page_size / _MIB


def _meminfo_mb(key: str) -> float | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith(key + ":"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _darwin_rss_mb(pid: int) -> float | None:
    import subprocess

    result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    text = result.stdout.strip()
    return int(text) / 1024 if result.returncode == 0 and text else None


def _darwin_total_mb() -> float | None:
    import subprocess

    result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
    text = result.stdout.strip()
    return int(text) / _MIB if result.returncode == 0 and text else None


def _darwin_available_mb() -> float | None:
    import re
    import subprocess

    result = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        return None
    page = re.search(r"page size of (\d+) bytes", result.stdout)
    page_size = int(page.group(1)) if page else 4096
    free = 0
    for label in ("Pages free", "Pages inactive"):
        match = re.search(rf"{label}:\s+(\d+)", result.stdout)
        if match is None:
            return None
        free += int(match.group(1))
    return free * page_size / _MIB


# ---------------------------------------------------------------------------
# Public probes
# ---------------------------------------------------------------------------

def current_rss_mb() -> float | None:
    """This process' resident set size in MiB. macOS has no cheap "current" probe without
    psutil, so it reports `ru_maxrss` (the peak) there -- documented, not hidden."""
    try:
        if sys.platform == "win32":
            mem = _win_self_memory()
            return round(mem[0], 1) if mem else None
        if sys.platform == "darwin":
            import resource

            return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / _MIB, 1)
        return round(_linux_self_rss_mb(), 1)
    except Exception:
        return None


def peak_rss_mb() -> float | None:
    """This process' peak resident set size in MiB (high-water mark since start)."""
    try:
        if sys.platform == "win32":
            mem = _win_self_memory()
            return round(mem[1], 1) if mem else None
        if sys.platform == "darwin":
            import resource

            return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / _MIB, 1)
        hwm = _proc_status_kb("self", "VmHWM")
        return round(hwm, 1) if hwm is not None else None
    except Exception:
        return None


def total_ram_mb() -> float | None:
    try:
        if sys.platform == "win32":
            mem = _win_global_memory()
            return round(mem[0], 1) if mem else None
        if sys.platform == "darwin":
            total = _darwin_total_mb()
            return round(total, 1) if total is not None else None
        total = _meminfo_mb("MemTotal")
        return round(total, 1) if total is not None else None
    except Exception:
        return None


def available_mb() -> float | None:
    """RAM the OS could hand out right now without swapping (Windows ullAvailPhys, Linux
    MemAvailable, macOS free+inactive pages) -- the number the staging worker cap and the
    low-memory runners gate on."""
    try:
        if sys.platform == "win32":
            mem = _win_global_memory()
            return round(mem[1], 1) if mem else None
        if sys.platform == "darwin":
            avail = _darwin_available_mb()
            return round(avail, 1) if avail is not None else None
        avail = _meminfo_mb("MemAvailable")
        return round(avail, 1) if avail is not None else None
    except Exception:
        return None


def snapshot() -> dict:
    return {
        "rss_mb": current_rss_mb(),
        "peak_rss_mb": peak_rss_mb(),
        "total_ram_mb": total_ram_mb(),
        "available_mb": available_mb(),
    }


def child_peak_rss_mb(pid: int) -> float | None:
    """One-shot peak RSS of another (still running, or Windows: still-open-handle) process."""
    try:
        if sys.platform == "win32":
            handle = _win_open_process(pid)
            if handle is None:
                return None
            try:
                mem = _win_process_memory(handle)
            finally:
                _win_close_handle(handle)
            return round(mem[1], 1) if mem else None
        if sys.platform == "darwin":
            rss = _darwin_rss_mb(pid)
            return round(rss, 1) if rss is not None else None
        hwm = _proc_status_kb(pid, "VmHWM")
        return round(hwm, 1) if hwm is not None else None
    except Exception:
        return None


class ChildRssSampler:
    """Peak RSS of a `subprocess.Popen` child *and its descendants*. `av` on PATH is a
    launcher (pip stub or the native exe) that re-execs the real Python CLI, so the tree
    is the only honest unit: `peak_mb` is the high-water mark of the summed resident set
    across the tree at any one sample, `largest_process_peak_mb` the biggest single
    process' own peak (Windows: the kernel's exact PeakWorkingSetSize, read through a
    handle held open past exit; Linux: VmHWM; macOS: polled `ps`, ±one interval).

        with ChildRssSampler(proc) as sampler:
            proc.wait()
        sampler.peak_mb
    """

    def __init__(self, proc, interval_s: float | None = None, include_descendants: bool = True) -> None:
        self.proc = proc
        self.interval_s = interval_s if interval_s is not None else (0.1 if sys.platform == "darwin" else 0.02)
        self.include_descendants = include_descendants
        self.peak_mb: float | None = None
        self.largest_process_peak_mb: float | None = None
        self.current_mb: float | None = None
        self.samples = 0
        self.source = "unavailable"
        self._handles: dict[int, object] = {}   # Windows: pid -> open handle (kept until stop)
        self._exited: set[int] = set()
        self._thread = None
        self._stop = None
        self._lock = None

    def __enter__(self) -> "ChildRssSampler":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        import threading

        if sys.platform == "win32":
            self.source = "win32-peak-ws"
        else:
            self.source = "darwin-ps" if sys.platform == "darwin" else "linux-vmhwm"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.sample()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def _pids(self) -> list[int]:
        pids = [self.proc.pid]
        if self.include_descendants:
            try:
                pids += _win_descendants(self.proc.pid) if sys.platform == "win32" else _posix_descendants(self.proc.pid)
            except Exception:
                pass
        return pids

    def _sample_win(self) -> tuple[float, float] | None:
        total = 0.0
        largest = 0.0
        seen_any = False
        for pid in self._pids():
            handle = self._handles.get(pid)
            if handle is None and pid not in self._exited:
                try:
                    handle = _win_open_process(pid)
                except Exception:
                    handle = None
                if handle is None:
                    continue
                self._handles[pid] = handle
            if handle is None:
                continue
            mem = _win_process_memory(handle)
            if mem is None:
                continue
            seen_any = True
            total += mem[0]
            largest = max(largest, mem[1])
        # Handles of exited processes keep their peak readable: fold them into `largest`.
        for pid, handle in self._handles.items():
            mem = _win_process_memory(handle)
            if mem is not None:
                largest = max(largest, mem[1])
                seen_any = True
        return (total, largest) if seen_any else None

    def _sample_posix(self) -> tuple[float, float] | None:
        total = 0.0
        largest = 0.0
        seen_any = False
        for pid in self._pids():
            current = _posix_current_rss_mb(pid)
            if current is None:
                continue
            seen_any = True
            total += current
            if sys.platform != "darwin":
                hwm = _proc_status_kb(pid, "VmHWM")
                largest = max(largest, hwm if hwm is not None else current)
            else:
                largest = max(largest, current)
        return (total, largest) if seen_any else None

    def sample(self) -> float | None:
        """Take one sample now (also called by the background poller). The whole read runs
        under the lock: the poller and a caller's own `sample()` both walk `_handles`, and
        a concurrent mutation there used to be swallowed as a failed sample, leaving
        `current_mb` stale."""
        with self._lock:
            try:
                result = self._sample_win() if sys.platform == "win32" else self._sample_posix()
            except Exception:
                result = None
            if result is None:
                return self.peak_mb
            total, largest = result
            self.samples += 1
            self.current_mb = round(total, 1)
            self.peak_mb = round(total, 1) if self.peak_mb is None else max(self.peak_mb, round(total, 1))
            self.largest_process_peak_mb = (round(largest, 1) if self.largest_process_peak_mb is None
                                            else max(self.largest_process_peak_mb, round(largest, 1)))
            return self.peak_mb

    def _poll(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.interval_s)
        self.sample()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
            self._thread.join(timeout=5)
        if sys.platform == "win32":
            # Final exact read: the root's own peak survives exit while its handle is open.
            for handle in self._handles.values():
                try:
                    mem = _win_process_memory(handle)
                except Exception:
                    mem = None
                if mem is not None and self.largest_process_peak_mb is not None:
                    self.largest_process_peak_mb = max(self.largest_process_peak_mb, round(mem[1], 1))
                    if mem[1] > (self.peak_mb or 0):
                        # A process that peaked between two samples: its own high-water
                        # mark is a lower bound on the tree peak.
                        self.peak_mb = round(mem[1], 1)
            for handle in self._handles.values():
                try:
                    _win_close_handle(handle)
                except Exception:
                    pass
            self._handles.clear()
        if self.samples == 0:
            self.source = "unavailable"


class MeasuredRun:
    """Result of `run_measured`: exit code, wall time, and the child's peak RSS."""

    __slots__ = ("returncode", "elapsed_ms", "peak_rss_mb", "stdout", "stderr", "source")

    def __init__(self, returncode: int, elapsed_ms: float, peak_rss_mb: float | None,
                 stdout: str | None = None, stderr: str | None = None, source: str = "unavailable") -> None:
        self.returncode = returncode
        self.elapsed_ms = elapsed_ms
        self.peak_rss_mb = peak_rss_mb
        self.stdout = stdout
        self.stderr = stderr
        self.source = source

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MeasuredRun(rc={self.returncode}, {self.elapsed_ms:.0f}ms, peak={self.peak_rss_mb}MB, {self.source})"


def run_measured(args, *, cwd=None, env=None, timeout=None, capture_output=False,
                 stdin=None) -> MeasuredRun:
    """`subprocess.run` that also reports the child's peak RSS. Output is captured to
    pipes only when asked, so a chatty child never blocks on a full pipe otherwise."""
    import subprocess
    import time

    pipe = subprocess.PIPE if capture_output else None
    start = time.perf_counter()
    # Explicit UTF-8 with replacement: the CLI prints glyphs outside the Windows locale
    # codepage, and a measurement helper must never die decoding the thing it measured.
    proc = subprocess.Popen(
        args, cwd=cwd, env=env, stdout=pipe, stderr=pipe, stdin=stdin,
        text=capture_output, encoding="utf-8" if capture_output else None,
        errors="replace" if capture_output else None,
    )
    with ChildRssSampler(proc) as sampler:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    return MeasuredRun(proc.returncode, elapsed_ms, sampler.peak_mb, out, err, sampler.source)
