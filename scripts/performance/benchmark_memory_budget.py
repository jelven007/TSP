"""Compare panel retention on two checkouts with one interpreter and workload."""
from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path


def measure(backend: Path, rows: int) -> dict:
    sys.path.insert(0, str(backend.resolve()))
    import numpy as np
    import polars as pl
    import psutil

    from app.backtest.engine import PanelCache

    cache = PanelCache()
    process = psutil.Process()
    stopped = threading.Event()
    peak = [process.memory_info().rss]

    def sample():
        while not stopped.wait(.01):
            peak[0] = max(peak[0], process.memory_info().rss)

    sampler = threading.Thread(target=sample)
    sampler.start()
    start = time.perf_counter()
    sums = []
    try:
        for key in ("a", "b", "c"):
            def compute(*_):
                # Distinct allocations represent three projected numeric panels.
                return pl.DataFrame({f"f{i}": np.arange(rows, dtype=np.float64) for i in range(16)})

            frame = cache.get_or_compute(
                [key], date(2025, 1, 1), date(2026, 1, 1), None, compute,
            )
            sums.append(frame["f0"].sum())
            del frame
        gc.collect()
    finally:
        stopped.set()
        sampler.join()
    elapsed = time.perf_counter() - start
    retained = sum(entry.df.estimated_size() for entry in cache._cache.values())
    return {
        "elapsed_seconds": round(elapsed, 4),
        "peak_rss_bytes": max(peak[0], process.memory_info().rss),
        "final_rss_bytes": process.memory_info().rss,
        "retained_panel_bytes": retained,
        "sums": sums,
        "polars": pl.__version__,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, help="baseline backend directory")
    parser.add_argument("--after", type=Path, help="candidate backend directory")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.rows <= 0 or args.samples <= 0:
        parser.error("rows and samples must be positive")
    if args.child:
        print(json.dumps(measure(args.child, args.rows)))
        return
    if args.before is None or args.after is None:
        parser.error("--before and --after are required")
    env = {
        **os.environ, "TSP_MEMORY_BUDGET_MB": "1536", "POLARS_MAX_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "TICKFLOW_ENV_FILE": os.devnull,
    }
    results = {"before": [], "after": []}
    # Alternate to avoid warming only one side.
    for _ in range(args.samples):
        for name, root in (("before", args.before), ("after", args.after)):
            output = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--child", str(root.resolve()),
                 "--rows", str(args.rows)],
                cwd=root, env=env, text=True, capture_output=True, check=True, timeout=120,
            )
            results[name].append(json.loads(output.stdout.splitlines()[-1]))
    expected = [args.rows * (args.rows - 1) / 2] * 3
    if any(value["sums"] != expected for values in results.values() for value in values):
        raise RuntimeError("numeric results changed")
    metrics = ("elapsed_seconds", "peak_rss_bytes", "final_rss_bytes", "retained_panel_bytes")
    print(json.dumps({
        "os": platform.system(), "architecture": platform.machine(), "python": platform.python_version(),
        "rows": args.rows, "columns": 16, "panels": 3, "budget_mb": 1536,
        "medians": {
            name: {key: statistics.median(value[key] for value in values) for key in metrics}
            for name, values in results.items()
        },
        "samples": results,
    }, indent=2))


if __name__ == "__main__":
    main()
