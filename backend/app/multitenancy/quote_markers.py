"""One lightweight platform marker stream for all gateway clients."""
from __future__ import annotations

import logging
import threading
import time

import httpx

from app.services.quote_service import QuoteService

logger = logging.getLogger(__name__)


class QuoteMarkerBridge:
    def __init__(self, market_url: str, market_token: str, on_event=None):
        if not market_url or not market_token:
            raise RuntimeError("platform market marker stream is not configured")
        self.url = market_url.rstrip("/") + "/internal/cell-market/events"
        self.token = market_token
        self.events = QuoteService()
        self.on_event = on_event
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="quote-markers")
        self.client: httpx.Client | None = None
        self.connected = False
        self.last_marker_at: float | None = None
        self.reconnects = 0

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        client = self.client
        if client is not None:
            client.close()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def subscribe(self):
        return self.events.subscribe()

    def unsubscribe(self, subscriber) -> None:
        self.events.unsubscribe(subscriber)

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "last_marker_at": self.last_marker_at,
            "reconnects": self.reconnects,
        }

    def _dispatch(self, event: str | None) -> None:
        if event == "quotes_updated":
            self.events._broadcast_quote_updated()
        elif event == "depth_updated":
            self.events.notify_depth_updated()
        else:
            return
        if self.on_event is not None:
            self.on_event(event)
        self.last_marker_at = time.time()

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                with httpx.Client(
                    headers={"Authorization": "Bearer " + self.token},
                    timeout=httpx.Timeout(30, connect=5),
                    trust_env=False,
                ) as client:
                    self.client = client
                    with client.stream("GET", self.url) as response:
                        response.raise_for_status()
                        self.connected = True
                        event = None
                        for line in response.iter_lines():
                            if self.stop.is_set():
                                return
                            if not line:
                                self._dispatch(event)
                                event = None
                            elif line.startswith("event:"):
                                event = line.removeprefix("event:").strip()
            except Exception:
                if not self.stop.is_set():
                    logger.warning("platform quote marker stream unavailable")
            finally:
                self.client = None
                self.connected = False
            self.reconnects += 1
            self.stop.wait(1)
