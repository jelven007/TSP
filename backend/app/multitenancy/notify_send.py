"""Bounded delivery subprocess; receives transport settings only, never a DSN."""
import json
import sys
from types import SimpleNamespace

from pydantic import SecretStr

from app.cell.notifications import DeliveryError, NotificationTransport


def main():
    data = json.loads(sys.stdin.read(16 * 1024 * 1024))
    settings = data["settings"]
    settings["smtp_password"] = SecretStr(settings["smtp_password"])
    try:
        NotificationTransport(SimpleNamespace(**settings)).send_direct(data["channel"], data["event"], data["id"])
        outcome = {"ok": True}
    except DeliveryError as exc:
        outcome = {"error": exc.code, "retryable": exc.retryable}
    except Exception:
        outcome = {"error": "notification_delivery_failed", "retryable": True}
    print(json.dumps(outcome))


if __name__ == "__main__":
    main()
