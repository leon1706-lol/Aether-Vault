"""`av_cli.sysres` -- the dependency-free memory probes every V1.6.3 footprint number is
built on. If these lie, every budget downstream lies with them."""
import subprocess
import sys

import pytest

from python.av_cli import sysres


def test_current_rss_is_positive_and_le_peak():
    current = sysres.current_rss_mb()
    peak = sysres.peak_rss_mb()
    assert current is not None and current > 0
    assert peak is not None and peak > 0
    # macOS reports ru_maxrss for both; everywhere else current can never exceed the
    # high-water mark by more than sampling jitter.
    assert current <= peak * 1.01


def test_total_and_available_are_sane():
    total = sysres.total_ram_mb()
    available = sysres.available_mb()
    assert total is not None and total > 256
    assert available is not None and 0 < available <= total


def test_snapshot_has_every_key():
    snap = sysres.snapshot()
    assert set(snap) == {"rss_mb", "peak_rss_mb", "total_ram_mb", "available_mb"}


def test_child_sampler_sees_an_allocation():
    code = "b = bytearray(150 * 2**20); b[::4096] = b'x' * len(b[::4096]); import time; time.sleep(0.4)"
    proc = subprocess.Popen([sys.executable, "-c", code])
    with sysres.ChildRssSampler(proc) as sampler:
        proc.wait(timeout=30)
    if sampler.source == "unavailable":
        pytest.skip("no child RSS probe on this platform")
    assert sampler.peak_mb is not None and sampler.peak_mb >= 140, (sampler.peak_mb, sampler.source)


def test_child_sampler_covers_descendants():
    """`av` on PATH is a launcher that re-execs the real CLI: a parent-only probe would
    report the 4 MB stub. The tree's peak must see the grandchild's allocation."""
    child = "b = bytearray(120 * 2**20); b[::4096] = b'x' * len(b[::4096]); import time; time.sleep(0.6)"
    parent = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {child!r}])"
    proc = subprocess.Popen([sys.executable, "-c", parent])
    with sysres.ChildRssSampler(proc) as sampler:
        proc.wait(timeout=60)
    if sampler.source == "unavailable":
        pytest.skip("no child RSS probe on this platform")
    assert sampler.peak_mb is not None and sampler.peak_mb >= 110, (sampler.peak_mb, sampler.source)
    assert sampler.largest_process_peak_mb >= 110
    run = sysres.run_measured([sys.executable, "-c", parent], timeout=60)
    assert run.peak_rss_mb >= 110


def test_child_sampler_can_exclude_descendants():
    child = "b = bytearray(120 * 2**20); b[::4096] = b'x' * len(b[::4096]); import time; time.sleep(0.5)"
    parent = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {child!r}])"
    proc = subprocess.Popen([sys.executable, "-c", parent])
    with sysres.ChildRssSampler(proc, include_descendants=False) as sampler:
        proc.wait(timeout=60)
    if sampler.source == "unavailable":
        pytest.skip("no child RSS probe on this platform")
    assert sampler.peak_mb is not None and sampler.peak_mb < 100


def test_run_measured_returns_exit_code_and_peak():
    run = sysres.run_measured([sys.executable, "-c", "raise SystemExit(3)"], capture_output=True, timeout=30)
    assert run.returncode == 3
    assert run.elapsed_ms > 0
    assert run.peak_rss_mb is None or run.peak_rss_mb > 0


def test_run_measured_captures_output_only_when_asked():
    run = sysres.run_measured([sys.executable, "-c", "print('hi')"], capture_output=True, timeout=30)
    assert run.stdout.strip() == "hi"
    run = sysres.run_measured([sys.executable, "-c", "pass"], timeout=30)
    assert run.stdout is None


def test_daemon_process_rss_helper_still_works():
    from python.av_cli import daemon

    rss = daemon._process_rss_mb()
    assert rss is not None and rss > 0
    peak = daemon._process_peak_rss_mb()
    assert peak is not None and peak >= rss * 0.99


def test_server_sysres_copy_agrees_with_cli_copy():
    from python.av_server import sysres as server_sysres

    cli_mb = sysres.current_rss_mb()
    server_bytes = server_sysres.current_rss_bytes()
    assert cli_mb is not None and server_bytes is not None
    assert abs(server_bytes / (1024 * 1024) - cli_mb) < 5
    assert server_sysres.peak_rss_bytes() >= server_bytes * 0.99


def test_module_scope_imports_stay_minimal():
    """`daemon.py`/`core.py` import this on hot paths; `test_import_graph.py` pins what
    those paths may load, so ctypes/subprocess/threading must stay inside functions."""
    code = (
        "import sys; import av_cli.sysres; "
        "print(','.join(m for m in ('ctypes','subprocess','threading','re') if m in sys.modules))"
    )
    result = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, timeout=60,
                            cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "python"))
    assert result.returncode == 0, result.stderr
    loaded = {m for m in result.stdout.strip().split(",") if m}
    assert not loaded & {"subprocess", "threading"}, loaded
