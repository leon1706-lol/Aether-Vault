"""V1.6.3: the staging worker pool is capped by free RAM (each worker can hold an
`AV_STAGE_BUFFER_MB` layer buffer), by `AV_STAGE_WORKERS_MAX`, and the C++ pool always
gets an explicit, cgroup-aware thread count instead of C++'s raw hardware_concurrency()."""
import pytest

from python.av_cli import core


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("AV_STAGE_WORKERS_MAX", "AV_STAGE_RESERVE_MB", "AV_STAGE_BUFFER_MB", "AV_THREADS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(core, "cpu_count_for_threading", lambda: 8)


def test_worker_budget_follows_the_buffer_cap(monkeypatch):
    assert core.stage_worker_budget_mb() == 34  # default 32 MiB + read buffer + header
    monkeypatch.setenv("AV_STAGE_BUFFER_MB", "8")
    assert core.stage_worker_budget_mb() == 10


def test_ram_cap_shrinks_the_pool_on_a_small_box():
    # (300 - 256 reserve) // 34 = 1
    assert core.effective_stage_workers(0, available_mb=300) == 1
    # (600 - 256) // 34 = 10 -> cpu cap of 8 wins
    assert core.effective_stage_workers(0, available_mb=600) == 8
    # (400 - 256) // 34 = 4
    assert core.effective_stage_workers(0, available_mb=400) == 4


def test_ram_cap_never_goes_below_one():
    assert core.effective_stage_workers(0, available_mb=10) == 1
    assert core.effective_stage_workers(0, available_mb=0) == 1


def test_reserve_is_configurable(monkeypatch):
    monkeypatch.setenv("AV_STAGE_RESERVE_MB", "0")
    assert core.effective_stage_workers(0, available_mb=136) == 4  # 136 // 34
    monkeypatch.setenv("AV_STAGE_RESERVE_MB", "not-a-number")
    assert core.effective_stage_workers(0, available_mb=400) == 4  # default reserve again


def test_hard_max_caps_below_the_ram_cap(monkeypatch):
    monkeypatch.setenv("AV_STAGE_WORKERS_MAX", "3")
    assert core.effective_stage_workers(0, available_mb=4000) == 3
    monkeypatch.setenv("AV_STAGE_WORKERS_MAX", "0")  # non-positive: ignored
    assert core.effective_stage_workers(0, available_mb=4000) == 8


def test_no_probe_means_no_ram_cap(monkeypatch):
    from python.av_cli import sysres

    monkeypatch.setattr(sysres, "available_mb", lambda: None)
    assert core.effective_stage_workers(0) == core.python_pool_size(0)


def test_explicit_single_thread_stays_sequential():
    assert core.effective_stage_workers(1, available_mb=100_000) == 1


def test_explicit_thread_count_is_still_ram_capped():
    assert core.effective_stage_workers(6, available_mb=4000) == 6
    assert core.effective_stage_workers(6, available_mb=350) == 2  # (350-256)//34


def test_configure_native_threads_passes_cgroup_aware_count(monkeypatch):
    calls = []

    class _FakeCore:
        @staticmethod
        def set_max_threads(n):
            calls.append(n)

    monkeypatch.setattr(core, "_get_aether_core", lambda: _FakeCore)
    monkeypatch.setattr(core, "cpu_count_for_threading", lambda: 3)
    monkeypatch.setattr(core, "_native_threads_configured", False)
    assert core.configure_native_threads(None, None) == 0  # 0 = "auto" for the Python pool
    assert calls == [3]  # ...but the C++ pool got the real, quota-aware number


def test_configure_native_threads_caps_the_auto_count_at_the_cpp_pool_max(monkeypatch):
    calls = []

    class _FakeCore:
        @staticmethod
        def set_max_threads(n):
            calls.append(n)

    monkeypatch.setattr(core, "_get_aether_core", lambda: _FakeCore)
    monkeypatch.setattr(core, "cpu_count_for_threading", lambda: 64)
    monkeypatch.setattr(core, "_native_threads_configured", False)
    core.configure_native_threads(None, None)
    assert calls == [16]


def test_configure_native_threads_forwards_an_explicit_override(monkeypatch):
    calls = []

    class _FakeCore:
        @staticmethod
        def set_max_threads(n):
            calls.append(n)

    monkeypatch.setattr(core, "_get_aether_core", lambda: _FakeCore)
    monkeypatch.setattr(core, "_native_threads_configured", False)
    monkeypatch.setenv("AV_THREADS", "5")
    assert core.configure_native_threads(None, None) == 5
    assert calls == [5]


def test_release_native_pool_is_a_no_op_without_the_extension(monkeypatch):
    monkeypatch.setattr(core, "_get_aether_core", lambda: None)
    core.release_native_pool()  # must not raise
