from __future__ import annotations

import pytest

from scripts.accept_external_sse import (
    MAX_CONCURRENT_CLIENTS,
    _capacity_summary,
    _concurrent_fanout_acceptance,
)


def test_capacity_summary_reports_all_clients_and_percentiles() -> None:
    result = _capacity_summary(
        [
            {
                "ticket_latency_ms": value,
                "public_latency_ms": value * 2,
                "private_latency_ms": value * 3,
                "private_sequence": 7,
            }
            for value in (1.0, 2.0, 3.0, 4.0)
        ],
        private_sequence=7,
        elapsed_seconds=1.2345,
    )

    assert result == {
        "clients": 4,
        "connected": 4,
        "public_received": 4,
        "private_received": 4,
        "private_sequence": 7,
        "ticket_latency_ms": {"p50": 2.0, "p95": 4.0, "max": 4.0},
        "first_public_latency_ms": {"p50": 4.0, "p95": 8.0, "max": 8.0},
        "private_fanout_latency_ms": {
            "p50": 6.0,
            "p95": 12.0,
            "max": 12.0,
        },
        "elapsed_seconds": 1.234,
    }


def test_capacity_summary_rejects_mismatched_private_sequence() -> None:
    with pytest.raises(AssertionError, match="unexpected private sequences"):
        _capacity_summary(
            [
                {
                    "ticket_latency_ms": 1,
                    "public_latency_ms": 1,
                    "private_latency_ms": 1,
                    "private_sequence": 8,
                }
            ],
            private_sequence=7,
            elapsed_seconds=1,
        )


@pytest.mark.parametrize("clients", [0, MAX_CONCURRENT_CLIENTS + 1])
def test_concurrent_fanout_rejects_unbounded_client_count(clients: int) -> None:
    with pytest.raises(ValueError, match="concurrent clients"):
        _concurrent_fanout_acceptance(
            "http://127.0.0.1",
            "token",
            object(),
            {},
            clients=clients,
        )
