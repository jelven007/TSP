"""Only an exact, bounded audit chain explains a changed business resource."""
import pytest
from release import business_diff


def snapshot(rows):
    return {"scope": "tenant:test", "transaction": "repeatable-read",
            "rows": rows, "captured_at": "2026-11-07T00:00:00+00:00"}


def audit(before, after, **extra):
    return {"resource": "holdings/1", "before": before, "after": after,
            "request_id": "request-test", "actor_id": "actor-test", "action": "holdings.update",
            "at": "2026-11-07T00:01:00+00:00", **extra}


def compare(before, after, events):
    return business_diff.compare(
        snapshot(before), {**snapshot(after), "captured_at": "2026-11-07T00:02:00+00:00"},
        events,
    )


def test_exact_chain_preserves_normal_changes_and_reports_unknowns():
    result = compare({"holdings/1": "a" * 64}, {"holdings/1": "c" * 64},
                     [audit("a" * 64, "b" * 64), audit("b" * 64, "c" * 64)])
    assert result["status"] == "passed"
    assert result["changes"][0]["kind"] == "modified"
    assert result["changes"][0]["explained"] is True
    assert result["database_writes"] is False
    assert compare({"holdings/1": "a" * 64}, {"holdings/1": "c" * 64}, [])["status"] == "blocked"


@pytest.mark.parametrize("extra", [
    {"resource": "*"}, {"request_id": ""}, {"actor_id": ""},
    {"action": ""}, {"at": "2026-11-06T00:00:00+00:00"},
])
def test_broad_or_unbounded_events_do_not_explain_change(extra):
    assert compare({"holdings/1": "a" * 64}, {"holdings/1": "b" * 64},
                   [audit("a" * 64, "b" * 64, **extra)])["status"] == "blocked"


def test_add_and_delete_and_reversed_chain():
    assert compare({}, {"holdings/1": "b" * 64}, [audit(None, "b" * 64)])["status"] == "passed"
    assert compare({"holdings/1": "a" * 64}, {}, [audit("a" * 64, None)])["status"] == "passed"
    assert compare({"holdings/1": "a" * 64}, {"holdings/1": "c" * 64},
                   [audit("b" * 64, "c" * 64), audit("a" * 64, "b" * 64)])["status"] == "blocked"


def test_scope_or_inconsistent_snapshot_rejected():
    with pytest.raises(ValueError, match="scope"):
        business_diff.compare(snapshot({}), {**snapshot({}), "scope": "other"}, [])
    with pytest.raises(ValueError, match="transaction"):
        business_diff.compare({**snapshot({}), "transaction": "none"}, snapshot({}), [])
