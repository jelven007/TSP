"""One bounded external delivery. Input on stdin; never log credentials/destinations."""

import json
import sys

from app.cell.config import CellSettings
from app.cell.notifications import DeliveryError, NotificationTransport


def main():
    payload = json.load(sys.stdin)
    try:
        NotificationTransport(CellSettings(**payload["settings"])).send_direct(
            payload["channel"], payload["event"], payload["id"]
        )
        outcome = {}
    except DeliveryError as exc:
        outcome = {"error": exc.code, "retryable": exc.retryable}
    except Exception:
        outcome = {"error": "notification_delivery_failed", "retryable": False}
    print(json.dumps(outcome))


if __name__ == "__main__":
    main()
