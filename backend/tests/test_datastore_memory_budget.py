import pytest

from app.tickflow.repository import DataStore


@pytest.mark.parametrize("read_only", [False, True])
def test_native_service_budget_caps_duckdb_and_preserves_queries(tmp_path, monkeypatch, read_only):
    monkeypatch.setenv("TSP_MEMORY_BUDGET_MB", "256")
    store = DataStore(tmp_path, read_only=read_only)
    try:
        limit = store.db.execute(
            "select current_setting('memory_limit')"
        ).fetchone()[0]
        assert limit == "32.0 MiB"
        assert store.db.execute("select current_setting('threads')").fetchone()[0] <= 2
        assert store.db.execute("select sum(i) from range(100000) as t(i)").fetchone()[0] == 4999950000
        if read_only:
            assert not list(tmp_path.iterdir())
    finally:
        store.db.close()


def test_invalid_native_budget_fails_before_repository_read(tmp_path, monkeypatch):
    monkeypatch.setenv("TSP_MEMORY_BUDGET_MB", "invalid")
    with pytest.raises(ValueError, match="TSP_MEMORY_BUDGET_MB"):
        DataStore(tmp_path, read_only=True)
