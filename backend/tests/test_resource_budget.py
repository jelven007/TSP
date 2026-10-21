from pathlib import Path

import pytest

from app import resource_budget
from app.indicators import pipeline

MIB = 1024**2


@pytest.fixture(autouse=True)
def clear_budget(monkeypatch):
    monkeypatch.delenv("TSP_MEMORY_BUDGET_MB", raising=False)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value))


@pytest.mark.parametrize("version", [1, 2])
def test_memory_budget_uses_container_and_parent_limits(tmp_path, monkeypatch, version):
    root = tmp_path / "cgroup"
    membership = tmp_path / "membership"
    if version == 2:
        membership.write_text("0::/parent/child\n")
        base, filename = root, "memory.max"
    else:
        membership.write_text("7:cpu,cpuacct:/other\n5:memory:/parent/child\n")
        base, filename = root / "memory", "memory.limit_in_bytes"
    write(base / filename, 8 * 1024 * MIB)
    write(base / "parent" / filename, 2 * 1024 * MIB)
    write(base / "parent/child" / filename, 4 * 1024 * MIB)
    monkeypatch.setattr(resource_budget, "_physical_memory_bytes", lambda: 16 * 1024 * MIB)
    monkeypatch.setattr(resource_budget, "_CGROUP_ROOT", root)
    monkeypatch.setattr(resource_budget, "_CGROUP_MEMBERSHIP", membership)

    assert resource_budget.effective_memory_bytes() == 2 * 1024 * MIB
    assert pipeline._adaptive_sym_batch(1000, 1500) == 100
    # A long history must not force at least 50 symbols into a small container.
    assert pipeline._adaptive_sym_batch(20, 10000) == 15
    assert pipeline._adaptive_sym_batch(5, 100) == 5


@pytest.mark.parametrize("value", ["max", "0", "-1", "unreadable", str(2**63 - 4096)])
def test_unknown_or_unlimited_cgroup_preserves_physical_limit(tmp_path, monkeypatch, value):
    write(tmp_path / "memory.max", value)
    monkeypatch.setattr(resource_budget, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(resource_budget, "_CGROUP_MEMBERSHIP", tmp_path / "missing")
    monkeypatch.setattr(resource_budget, "_physical_memory_bytes", lambda: 16 * 1024 * MIB)
    assert resource_budget.effective_memory_bytes() == 16 * 1024 * MIB


def test_no_os_memory_probe_uses_conservative_batching(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_budget, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(resource_budget, "_CGROUP_MEMBERSHIP", tmp_path / "missing")
    monkeypatch.setattr(resource_budget, "_physical_memory_bytes", lambda: None)
    assert resource_budget.effective_memory_bytes() is None
    assert pipeline._adaptive_sym_batch(1000, 1500) == 100


def test_namespaced_cgroup_root_without_membership_still_applies(tmp_path, monkeypatch):
    write(tmp_path / "memory.max", 1024 * MIB)
    monkeypatch.setattr(resource_budget, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(resource_budget, "_CGROUP_MEMBERSHIP", Path("/not-present"))
    monkeypatch.setattr(resource_budget, "_physical_memory_bytes", lambda: None)
    assert resource_budget.effective_memory_bytes() == 1024 * MIB


def test_updated_container_limit_is_not_cached_forever(tmp_path, monkeypatch):
    write(tmp_path / "memory.max", 4 * 1024 * MIB)
    monkeypatch.setattr(resource_budget, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(resource_budget, "_CGROUP_MEMBERSHIP", tmp_path / "missing")
    monkeypatch.setattr(resource_budget, "_physical_memory_bytes", lambda: 16 * 1024 * MIB)
    assert resource_budget.effective_memory_bytes() == 4 * 1024 * MIB
    write(tmp_path / "memory.max", 1024 * MIB)
    assert resource_budget.effective_memory_bytes() == 1024 * MIB


def test_native_windows_service_budget_reserves_memory_for_other_roles(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_budget, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(resource_budget, "_CGROUP_MEMBERSHIP", tmp_path / "missing")
    monkeypatch.setattr(resource_budget, "_physical_memory_bytes", lambda: 16 * 1024 * MIB)
    monkeypatch.setenv("TSP_MEMORY_BUDGET_MB", "2048")
    assert resource_budget.effective_memory_bytes() == 2048 * MIB
    assert resource_budget.cache_budget_bytes(512) == 256 * MIB
    assert pipeline._adaptive_sym_batch(1000, 1500) == 100
    monkeypatch.setenv("TSP_MEMORY_BUDGET_MB", "32768")
    assert resource_budget.effective_memory_bytes() == 16 * 1024 * MIB


@pytest.mark.parametrize("value", ["", "-1", "0", "2GB", "2.5"])
def test_invalid_native_budget_fails_explicitly(monkeypatch, value):
    monkeypatch.setenv("TSP_MEMORY_BUDGET_MB", value)
    with pytest.raises(ValueError, match="TSP_MEMORY_BUDGET_MB"):
        resource_budget.effective_memory_bytes()
