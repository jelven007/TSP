"""Run the full-product research dispatcher independently of HTTP processes."""
from __future__ import annotations

import logging
import signal
import threading

from app.cell.config import CellSettings
from app.cell.database import Databases
from app.multitenancy.executor import ComputeExecutor
from app.multitenancy.research import ResearchWorker


def main():
    logging.basicConfig(level=logging.INFO)
    db = Databases(CellSettings())
    db.open()
    worker = ResearchWorker(db, ComputeExecutor(), concurrency=db.settings.worker_concurrency)
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    try:
        worker.start()
        stopped.wait()
    finally:
        worker.close()
        db.close()


if __name__ == "__main__":
    main()
