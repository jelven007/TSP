"""Run real Docker isolation and full strategy computation against disposable data."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.multitenancy.compute_broker import ComputeBroker
from app.multitenancy.executor import ComputeExecutor

FIXTURE = """
import json
from datetime import date,timedelta
from pathlib import Path
import polars as pl
root=Path('/fixture')
rows=[]
for offset in range(400):
    day=date(2025,1,1)+timedelta(days=offset)
    if day.weekday()>4: continue
    for symbol,price in [('000001.SZ',11.),('600000.SH',21.)]:
        price += offset * (0.001 if symbol=='000001.SZ' else 0.003)
        rows.append(dict(symbol=symbol,date=day,open=price,high=price+0.2,low=price-0.2,
            close=price,raw_open=price,raw_close=price,raw_high=price+0.2,raw_low=price-0.2,
            volume=1000000.,
            amount=200000000.,turnover_rate=1. if symbol=='000001.SZ' else 2.,
            consecutive_limit_ups=0,consecutive_limit_downs=0,quote_ts=0,
            quote_time_basis='source_trading_date',
            is_risk_warning=False,instrument_status_known=True,is_listed=True,
            signal_n_day_low=offset==10,signal_ma20_breakdown=offset==20))
frame=pl.DataFrame(rows).with_columns(
    pl.col('consecutive_limit_ups','consecutive_limit_downs').cast(pl.UInt32))
for day in frame['date'].unique().to_list():
    for directory in ['kline_daily','kline_daily_enriched']:
        target=root/directory/('date='+str(day))
        target.mkdir(parents=True)
        frame.filter(pl.col('date')==day).write_parquet(target/'part.parquet')
target=root/'instruments'
target.mkdir()
pl.DataFrame([dict(symbol=symbol,name=name,code=symbol[:6],exchange=symbol[-2:],
    total_shares=100000000.,float_shares=100000000.,listing_date='2020-01-01')
    for symbol,name in [('000001.SZ','平安银行'),('600000.SH','浦发银行')]]).write_parquet(target/'part.parquet')
from app.enriched_generation import bump_enriched_generation
print(json.dumps({'as_of':str(frame['date'].max()),'generation':bump_enriched_generation(root)}))
"""

PROBE = """
import json,os
from pathlib import Path
assert os.getuid()==10001
assert not any(name in os.environ for name in ['TSP_PRIVATE_PROBE','TSP_CELL_CONTROL_DSN',
    'TSP_CELL_DATA_DSN','AI_API_KEY','SMTP_PASSWORD'])
assert not Path('/var/run/docker.sock').exists()
try:
    Path('/market/forbidden-write').write_text('no')
except OSError: pass
else: raise AssertionError('public market mount is writable')
Path('/tmp/allowed-write').write_text('yes')
assert len(os.listdir('/market'))>0
print(json.dumps({'uid':os.getuid(),'public_readonly':True,'credentials_absent':True}))
"""

ARTIFACT_PROBE = """
import base64,io,json,sys
import polars as pl
values=json.load(sys.stdin)
assert set(values)=={'factors','correlation','candidates','folds'}
for name,encoded in values.items():
    frame=pl.read_parquet(io.BytesIO(base64.b64decode(encoded)))
    assert len(frame.columns)>0,name
print(json.dumps({'artifacts':list(values),'valid_parquet':True}))
"""

MONITOR_PROBE = """
import json,time
from pathlib import Path
import polars as pl
from app.market_time import cn_today
from app.multitenancy.market import encode_frame
from app.strategy.eligibility import attach_listing_age
frame=pl.read_parquet(sorted(Path('/market/kline_daily_enriched').glob('date=*/part.parquet'))[-1])
frame=frame.with_columns(pl.lit(cn_today()).alias('date'),pl.lit(time.time()*1000).alias('quote_ts'))
frame=attach_listing_age(frame,pl.read_parquet('/market/instruments/part.parquet'))
print(json.dumps({'sequence':int(time.time()*1000),'captured_at':time.time(),
    'status':{'is_trading_hours':True},'frames':{'stock':encode_frame(frame)},
    'days':{'stock':str(cn_today())},'indices':encode_frame(pl.DataFrame()),
    'names':{'000001.SZ':'平安银行','600000.SH':'浦发银行'},'sectors':{},'abnormal':[]}))
"""

CODE = """import polars as pl
META = {"id": "custom_sandbox", "name": "隔离选股", "description": "", "tags": [],
        "params": [{"id":"threshold","type":"float","default":15.,"min":0.,"max":30.,"step":5.}],
        "scoring": {}, "asset_types": ["stock"], "timeframes": ["1d"]}
def filter(df, params):
    return pl.col("close") > params.get("threshold", 15)
"""


def run(args) -> dict:
    with tempfile.TemporaryDirectory(prefix="tsp-compute-check-", dir=args.work_dir) as directory:
        root = Path(directory)
        market = root / "public"
        market.mkdir()
        fixture = subprocess.run(
            ["docker", "run", "--rm", "--user", "0:0", "--network", "none",
             "--mount", f"type=bind,src={market},dst=/fixture", "--entrypoint", "/app/.venv/bin/python",
             args.image, "-c", FIXTURE], capture_output=True, text=True, timeout=120, check=True,
        )
        market_info = json.loads(fixture.stdout.strip().splitlines()[-1])
        as_of = market_info["as_of"]
        with ComputeBroker(root / "compute.sock", args.image, market, 4) as broker:
            command = broker.command("tsp-compute-isolation-probe")
            command = command[:-2] + ["-c", PROBE]
            probe = subprocess.run(command, capture_output=True, text=True, timeout=60, check=True)
            isolation = json.loads(probe.stdout)
            thread = threading.Thread(target=broker.serve_forever, daemon=True)
            thread.start()
            try:
                executor = ComputeExecutor(str(root / "compute.sock"))
                base = {"tenant_id": "disposable-tenant", "sources": [
                    {"id": "custom_sandbox", "source": "custom", "code": CODE}],
                    "factors": [], "signals": [], "overrides": {}}
                describe = executor.execute({**base, "kind": "describe", "strategy_id": "custom_sandbox"})
                assert describe["definition"]["meta"]["id"] == "custom_sandbox"
                assert describe["definition"]["execution_backend"] == "polars_expr"
                snapshot_probe = subprocess.run(
                    broker.command("tsp-compute-monitor-probe")[:-2] + ["-c", MONITOR_PROBE],
                    capture_output=True, text=True, timeout=60, check=True,
                )
                monitor_payload = {**base, "kind": "monitor", "snapshot": json.loads(snapshot_probe.stdout),
                    "groups": {"mine": ["600000.SH"]}, "rules": [
                        {"id": "private_monitor", "name": "私有策略监控", "type": "strategy",
                         "strategy_id": "custom_sandbox", "asset_type": "stock", "scope": "watchlist_group",
                         "group_id": "mine", "symbols": [], "cooldown_seconds": 3600},
                        {"id": "private_price", "name": "价格监控", "type": "price",
                         "asset_type": "stock", "scope": "symbols", "symbols": ["600000.SH"],
                         "conditions": [{"field": "close", "op": ">", "value": 15}], "cooldown_seconds": 3600},
                    ]}
                monitored = executor.execute(monitor_payload)
                assert monitored["results"]["custom_sandbox"]["total"] == 1, monitored
                assert any(event["rule_id"] == "private_price" for event in monitored["events"]), monitored
                resumed = executor.execute({**monitor_payload, "state": monitored["state"]})
                assert not any(event["rule_id"] == "private_price" for event in resumed["events"]), resumed
                if args.monitor_only:
                    return {**isolation, "private_monitor": "passed", "group_scope": "passed",
                            "durable_cooldown_restart": "passed", "full_monitor_results": "passed"}
                result = executor.execute({**base, "kind": "strategy", "strategy_ids": ["custom_sandbox"],
                    "asset_type": "stock", "timeframe": "1d", "as_of": as_of,
                    "params_map": {}, "overrides_map": {}})
                selected = result["results"]["custom_sandbox"]
                assert selected["total"] == 1, selected
                assert selected["rows"][0]["symbol"] == "600000.SH", selected
                # Invalid code never reaches an engine execution and cannot
                # affect the next independent container.
                bad = {**base, "sources": [{"id": "custom_sandbox", "source": "custom",
                                           "code": "import os\n" + CODE}]}
                try:
                    executor.execute({**bad, "kind": "describe", "strategy_id": "custom_sandbox"})
                except Exception as exc:
                    assert "禁止" in str(exc), str(exc)
                else:
                    raise AssertionError("unsafe import was accepted")
                assert executor.execute({**base, "kind": "describe",
                                         "strategy_id": "custom_sandbox"})["definition"]["meta"]["id"] == "custom_sandbox"
                progress = []
                config = {"strategy_id": "custom_sandbox", "symbols": None,
                          "start": "2025-01-01", "end": as_of, "holding_days": 5}
                backtest = executor.execute({**base, "kind": "backtest", "config": config}, progress.append)
                assert backtest["error"] is None, backtest
                assert backtest["stats"]["n_trades"] > 0, backtest["stats"]
                assert progress
                signal_backtest = executor.execute({**base, "kind": "signal_backtest", "config": {
                    "symbols": ["000001.SZ", "600000.SH"], "start": "2025-01-01",
                    "end": as_of, "entries": ["rsi_oversold"], "exits": [],
                    "rsi_oversold_threshold": 101, "matching": "open_t+1",
                    "asset_type": "stock"}})
                assert signal_backtest["trades"], signal_backtest
                factor = executor.execute({**base, "kind": "factor_batch", "config": {
                    "factor_names": ["turnover_rate"], "symbols": None, "start": "2025-01-01",
                    "end": as_of, "n_groups": 2}})
                assert factor["error"] is None and factor["n_symbols"] == 2, factor
                optimize = executor.execute({**base, "kind": "optimize", "config": {
                    "strategy_id": "custom_sandbox", "symbols": None, "start": "2025-01-01",
                    "end": as_of, "param_grid": {"threshold": [15., 25.]}, "max_workers": 1}})
                assert len(optimize["results"]) == 2, optimize
                walkforward = executor.execute({**base, "kind": "walkforward", "config": {
                    "strategy_id": "custom_sandbox", "symbols": None, "start": "2025-01-01",
                    "end": as_of, "param_grid": {"threshold": [15.]}, "max_workers": 1,
                    "train_days": 100, "test_days": 30, "step_days": 30}})
                assert walkforward["folds"], walkforward
                mining = executor.execute({**base, "kind": "mining", "config": {
                    "run_id": "disposable-mining", "request": {
                        "factor_names": ["turnover_rate"], "asset_type": "stock",
                        "budget_profile": "exploratory", "require_regime": False,
                        "beam_width": 1, "max_combination_factors": 1, "max_finalists": 1},
                    "data_fingerprint": {"generation": market_info["generation"]}}})
                assert mining["status"] in {"succeeded", "succeeded_with_budget_exhausted"}, mining
                checked = subprocess.run(
                    broker.command("tsp-compute-artifact-probe")[:-2] + ["-c", ARTIFACT_PROBE],
                    input=json.dumps(mining["artifact_data"]), capture_output=True, text=True,
                    timeout=60, check=True,
                )
                assert json.loads(checked.stdout)["valid_parquet"] is True
                return {**isolation, "metadata": "passed", "strategy": "passed",
                        "selected": ["600000.SH"], "unsafe_import": "rejected",
                        "independent_recovery": "passed", "backtest": "passed",
                        "signal_backtest": "passed",
                        "factor_batch": "passed", "optimize": "passed",
                        "walkforward": "passed", "mining_artifacts": "passed"}
            finally:
                broker.shutdown()
                thread.join(timeout=3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--work-dir", type=Path, default=Path("/tmp"))
    parser.add_argument("--monitor-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    started = time.time()
    result = run(args)
    if args.output is None:
        print(json.dumps(result, ensure_ascii=False))
    else:
        inspected = json.loads(subprocess.run(
            ["docker", "image", "inspect", args.image, "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout)
        report = {
            "status": "passed",
            "scope": "local_emulated_full_compute_image_acceptance",
            "limits": [
                "Apple ARM host using Linux AMD64 emulation; not native performance evidence",
                "disposable deterministic market fixture; not production data",
            ],
            "artifact": {
                "reference": args.image,
                "id": inspected["Id"],
                "architecture": inspected["Architecture"],
                "os": inspected["Os"],
                "repo_digests": inspected.get("RepoDigests") or [],
            },
            "results": result,
            "elapsed_seconds": round(time.time() - started, 3),
        }
        report["report_sha256"] = hashlib.sha256(
            json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
