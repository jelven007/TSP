"""Release actions must not repeat after interruption or accept stale receipts."""
import fcntl
import json
import sys

import pytest
from release import pipeline


def command(code):
    return [sys.executable, "-c", code]


@pytest.fixture
def plan(tmp_path):
    source = tmp_path / "source"
    source.write_text("frozen source")
    counter = tmp_path / "counter"
    spec = {
        "format": 1, "environment": "test-production", "revision": "a" * 40,
        "version": "0.4.50", "inputs": {str(source): pipeline.file_hash(source)},
        "lock_dir": str(tmp_path / "locks"),
        "steps": [{
            "id": "prepare", "phase": "preflight", "effect": "read",
            "argv": command("print('private-value')"),
            "probe": command("print('{\"identity\":\"before\"}')"),
            "postcheck": command("print('{\"status\":\"passed\",\"identity\":\"after\"}')"),
        }, {
            "id": "switch", "phase": "switch", "effect": "write",
            "argv": command(
                f"from pathlib import Path;p=Path({str(counter)!r});"
                "p.write_text(p.read_text()+'x' if p.exists() else 'x')"
            ),
            "probe": command("print('{\"identity\":\"before\"}')"),
            "postcheck": command("print('{\"status\":\"passed\",\"identity\":\"after\"}')"),
        }],
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(spec))
    return path, tmp_path / "receipts", source, counter


def test_resume_verifies_success_without_repeating_actions(plan):
    path, receipts, _, counter = plan
    first = pipeline.run(path, receipts)
    assert counter.read_text() == "x"
    assert pipeline.run(path, receipts)["steps"] == first["steps"]
    assert counter.read_text() == "x"
    row = first["steps"]["switch"]
    assert row["status"] == "passed" and row["elapsed_seconds"] >= 0
    assert row["input_sha256"] and row["output_sha256"]
    assert "private-value" not in (receipts / "receipt.json").read_text()


def test_changed_source_invalidates_prior_success(plan):
    path, receipts, source, counter = plan
    pipeline.run(path, receipts)
    source.write_text("changed")
    with pytest.raises(ValueError, match="input"):
        pipeline.run(path, receipts)
    assert counter.read_text() == "x"


def test_interrupted_write_requires_reconciliation_not_replay(plan):
    path, receipts, _, counter = plan
    pipeline.run(path, receipts)
    record = json.loads((receipts / "receipt.json").read_text())
    record["steps"]["switch"]["status"] = "running"
    (receipts / "receipt.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="reconcile"):
        pipeline.run(path, receipts)
    pipeline.run(path, receipts, reconcile=True)
    assert counter.read_text() == "x"
    assert json.loads((receipts / "receipt.json").read_text())["steps"]["switch"]["reconciled"]


def test_failed_postcondition_cannot_be_reconciled(plan):
    path, receipts, _, counter = plan
    spec = json.loads(path.read_text())
    spec["steps"][1]["postcheck"] = command("print('{\"status\":\"failed\"}')")
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="postcheck"):
        pipeline.run(path, receipts)
    with pytest.raises(ValueError, match="postcheck"):
        pipeline.run(path, receipts, reconcile=True)
    assert counter.read_text() == "x"
    assert json.loads((receipts / "receipt.json").read_text())["status"] == "failed"


def test_stale_postcheck_updates_overall_status_without_replay(plan, tmp_path):
    path, receipts, _, counter = plan
    state = tmp_path / "state"
    state.write_text("before")
    spec = json.loads(path.read_text())
    spec["steps"][0]["postcheck"] = command(
        "import json;from pathlib import Path;"
        f"print(json.dumps({{'status':'passed','identity':Path({str(state)!r}).read_text()}}))"
    )
    path.write_text(json.dumps(spec))
    pipeline.run(path, receipts)
    state.write_text("after")
    with pytest.raises(ValueError, match="stale"):
        pipeline.run(path, receipts)
    assert json.loads((receipts / "receipt.json").read_text())["status"] == "failed"
    assert counter.read_text() == "x"


def test_read_retry_preserves_failed_attempt_timing(plan, tmp_path):
    path, receipts, _, counter = plan
    fail = tmp_path / "fail"
    fail.touch()
    spec = json.loads(path.read_text())
    spec["steps"][0]["argv"] = command(
        f"import sys;from pathlib import Path;sys.exit(1 if Path({str(fail)!r}).exists() else 0)"
    )
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="stage"):
        pipeline.run(path, receipts)
    fail.unlink()
    row = pipeline.run(path, receipts)["steps"]["prepare"]
    assert row["attempt"] == 2 and row["prior_attempts"][0]["status"] == "failed"
    assert row["prior_attempts"][0]["elapsed_seconds"] >= 0
    assert counter.read_text() == "x"


def test_local_write_requires_resume_and_respects_pause_marker(plan, tmp_path, monkeypatch):
    path, receipts, _, counter = plan
    spec = json.loads(path.read_text())
    spec["environment"] = "local"
    marker = tmp_path / "local.paused"
    monkeypatch.setattr(pipeline, "PAUSE_MARKER", marker)
    spec["pause_marker"] = str(marker)
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="resume"):
        pipeline.run(path, receipts)
    marker.touch()
    with pytest.raises(ValueError, match="paused"):
        pipeline.run(path, receipts, resume_local=True)
    assert not counter.exists() and marker.exists()


def test_environment_lock_blocks_concurrent_task(plan):
    path, receipts, _, counter = plan
    spec = json.loads(path.read_text())
    lock_path = pipeline.lock_path(spec)
    lock_path.parent.mkdir()
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="locked"):
            pipeline.run(path, receipts)
    assert not counter.exists()


def test_phase_order_and_shell_strings_are_rejected(plan):
    path, receipts, _, _ = plan
    spec = json.loads(path.read_text())
    spec["steps"].reverse()
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="phase"):
        pipeline.run(path, receipts)
    spec["steps"].reverse()
    spec["steps"][0]["argv"] = "echo unsafe"
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="argv"):
        pipeline.run(path, receipts)
