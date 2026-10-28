"""Compare concurrent status reads on real Parquet data in a read-only app."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
import time

BASE = Path(__file__).parent
parser = argparse.ArgumentParser()
parser.add_argument("implementation", choices=("before", "after"))
parser.add_argument("--readers", type=int, default=4)
args = parser.parse_args()
state_dir = BASE / f"benchmark-state-{args.implementation}"
state_dir.mkdir(exist_ok=True)
os.environ["DATA_DIR"] = str(state_dir)
os.environ["POLARS_MAX_THREADS"] = "2"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api import data
from app.services import pipeline_jobs
from app.services.public_query_cache import PublicQueryCache
from app.tickflow.repository import DataStore, KlineRepository

pipeline_jobs.job_store = pipeline_jobs.JobStore(store_dir=state_dir / "job_store")

if args.implementation == "before":
    spec = importlib.util.spec_from_file_location("baseline_cache", BASE / "baseline_cache.py")
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    PublicQueryCache = baseline.PublicQueryCache

cache = PublicQueryCache()
data.public_query_cache = cache
counts, counts_lock = Counter(), Lock()
original_table_stats = data._get_table_stats


def counted_stats(name, fetch, *, data_dir):
    def counted_fetch():
        with counts_lock:
            counts[name] += 1
        return fetch()
    return original_table_stats(name, counted_fetch, data_dir=data_dir)


data._get_table_stats = counted_stats
data_dir = Path("/Users/bytedance/.local/share/tsp-dev/TSPP/data")
store = DataStore(data_dir, read_only=True)
app = FastAPI()
app.include_router(data.router)
app.state.repo = KlineRepository(store)
app.state.indicators_ready = True
stop = Event()
invalidation_count = 0


def background_quote_updates():
    global invalidation_count
    while not stop.wait(2):
        cache.invalidate({"overview"})
        invalidation_count += 1


updater = Thread(target=background_quote_updates, daemon=True)
updater.start()
results = []
try:
    with TestClient(app) as client:
        for phase in ("cold", "warm"):
            barrier = Barrier(args.readers)
            with counts_lock:
                counts.clear()

            def read_status(number):
                barrier.wait(timeout=10)
                start = time.perf_counter()
                response = client.get("/api/data/status")
                value = response.json()
                result = {
                    "reader": number, "status": response.status_code,
                    "seconds": round(time.perf_counter() - start, 3),
                    "rows": {key: (value.get(key) or {}).get("rows")
                             for key in ("daily", "enriched", "minute", "5m", "10m", "15m")},
                }
                result["within_30_seconds"] = result["seconds"] < 30
                print(json.dumps({"implementation": args.implementation, "phase": phase, **result}),
                      flush=True)
                return result

            with ThreadPoolExecutor(max_workers=args.readers) as pool:
                records = list(pool.map(read_status, range(args.readers)))
            result = {"phase": phase, "requests": records, "table_scans": dict(counts)}
            results.append(result)
            print(json.dumps({"implementation": args.implementation,
                              "phase": phase, "table_scans": dict(counts)}), flush=True)
finally:
    stop.set()
    updater.join(timeout=3)
    store.db.close()

output = {
    "implementation": args.implementation, "readers": args.readers,
    "read_only_data_root": str(data_dir), "polars_threads": 2,
    "quote_invalidation_interval_seconds": 2,
    "quote_invalidations": invalidation_count,
    "application_config_and_jobs": "isolated temporary data directory",
    "phases": results,
}
(BASE / f"status-benchmark-{args.implementation}.json").write_text(
    json.dumps(output, indent=2) + "\n",
)
