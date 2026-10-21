"""A missing native transport must produce a service error, not an HTTP 500."""
import socket

import pytest

from app.cell.context import AccessError
from app.multitenancy.executor import ComputeExecutor


def test_missing_unix_transport_returns_503_without_running_private_code(monkeypatch):
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    with pytest.raises(AccessError) as unavailable:
        ComputeExecutor().execute({"kind": "describe", "tenant_id": "test"})
    assert unavailable.value.status == 503


def test_unknown_operation_is_rejected_even_when_transport_is_unavailable(monkeypatch):
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    with pytest.raises(ValueError, match="unsupported compute"):
        ComputeExecutor().execute({"kind": "shell"})
