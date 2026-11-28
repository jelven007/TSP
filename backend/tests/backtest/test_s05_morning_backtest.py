"""Current common-template S05 uses its target day's single frozen branch."""
import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest
from test_minute_backtest import _minute_frame
from test_morning_backtest import BUY, SELL, SYMBOL, configuration, service_fixture

from app.backtest.entry_execution import expected_trading_minutes
from app.strategy import custom_signals
from app.strategy import s02_173004552 as routed
from app.strategy.late_session_strength import DEFAULT_PARAMS

SOURCE = '''
import polars as pl
META = {"id": "custom_mvxxcs05", "name": "s05", "asset_types": ["stock"],
        "timeframes": ["1m"], "daily_history_bars": 5, "result_sections": "signals",
        "entry_signal_match": "any", "order_by": "score", "descending": True}
EXECUTION_BACKEND = "minute_filter"
ENTRY_SIGNALS = ["csgi_tail_entry_s05", "csgi_low_gap_entry_s05", "csgi_board_entry_s05"]
EXIT_SIGNALS = ["csgi_tail_exit_s05", "csgi_low_gap_exit_s05", "csgi_board_exit_s05"]
STOP_LOSS = None
MAX_HOLD_DAYS = None
def filter_minute_history(df, params, *, daily=None, selection=None):
    columns = [name for name in ENTRY_SIGNALS if name in df.columns]
    if not columns:
        return df.head(0).rename({"datetime": "last_datetime"}).with_columns(
            pl.lit(None, dtype=pl.Float64).alias("score"))
    matched = df.filter(pl.any_horizontal(pl.col(c).fill_null(False) for c in columns))
    matched = matched.sort("datetime").unique("symbol", keep="first").rename(
        {"datetime": "last_datetime"})
    scores = pl.DataFrame(selection["rows"]).select("symbol", pl.col("s02_score").alias("score"))
    return matched.drop("score", strict=False).join(scores, on="symbol")
'''


def breadth(day, code):
    sessions = routed.sessions_until(day)
    values = ([.6] * 21 if code == 1 else [.7] + [.52] * 20 if code == 2
              else [.95] + [.35] * 19 + [.5] if code == 3 else [.1] * 21)
    return [{"date": str(at), "up": up, "above_ma20": .5 if code == 3 else .1}
            for at, up in zip(sessions, values, strict=True)]


def fixture(tmp_path, monkeypatch, buy_code=1, sell_code=2, *, sell_volume=100_000.):
    from app.services import s05_scoring
    from app.strategy import signal_sections

    frames = {}
    for day, price in ((BUY, 10.2), (SELL, 10.4)):
        bars = expected_trading_minutes(datetime.combine(day, datetime.min.time()).replace(hour=9, minute=31),
                                        count=240)
        frames[day] = _minute_frame(day, [(SYMBOL, at.strftime("%H:%M"), price) for at in bars]).with_columns(
            pl.lit(sell_volume if day == SELL else 100_000.).alias("volume"))
    service, _, _ = service_fixture(tmp_path, frames=frames)
    service.engine.repo.store = SimpleNamespace(data_dir=tmp_path)
    (tmp_path / ".research_snapshot.json").write_text(json.dumps({
        "id": "s05-frozen-test-input",
        "created_at": f"{SELL}T23:00:00+08:00",
        "files": {}, "runtime": {},
    }))
    (tmp_path / "strategies/s05.py").write_text(SOURCE)
    service.strategy_engine.reload()
    for branch, entry, stop in (("tail", "14:05:00", "10:00:00"),
                                ("low_gap", "14:14:00", "13:25:00"),
                                ("board", "14:40:00", "10:55:00")):
        custom_signals.save_one(tmp_path, {
            "id": routed.ENTRY_IDS[branch], "name": branch, "kind": "entry",
            "template": "late_session_strength", "timeframe": "intraday", "enabled": True,
            "conditions": [], "params": {**dict.fromkeys(DEFAULT_PARAMS), "time": entry,
                "end_time": "14:55:00", "evaluation_delay_minutes": 1}})
        custom_signals.save_one(tmp_path, {
            "id": routed.EXIT_IDS[branch], "name": branch, "kind": "exit",
            "template": "morning_liquidation", "timeframe": "intraday", "enabled": True,
            "conditions": [], "params": {"start_time": "09:43:00", "time": stop,
                "fallback_time": "13:55:00", "rise_pct": None, "fall_pct": None}})
    selected = [{"symbol": SYMBOL, "reference_close": 10., "s02_score": 82., "s02_rank": 1}]
    monkeypatch.setattr(signal_sections, "select_rows", lambda *args, **kwargs: deepcopy(selected))

    def score(_repo, snapshot, source_day, day, observed, *, include_entry_route):
        assert not include_entry_route
        assert source_day < day
        return {**snapshot, "rows": deepcopy(selected), "score_coverage": {},
                "score_provenance": {}, "market_breadth": breadth(day, buy_code if day == BUY else sell_code)}

    monkeypatch.setattr(s05_scoring, "compute_selection", score)
    return service


def config(**extra):
    return configuration(strategy_id=routed.SID, initial_capital=1_000_000, max_positions=1,
        minute_price_rule="high_low_v1", entry_participation_pct=.1, exit_participation_pct=.1,
        overrides={"basic_filter": {"enabled": False}}, **extra)


@pytest.mark.parametrize("sell_code,branch,exit_time", [
    (1, "tail", "10:01"), (2, "low_gap", "13:26"), (3, "board", "10:56"),
])
def test_three_morning_signals_use_sell_days_branch_not_previous_buy(tmp_path, monkeypatch, sell_code, branch, exit_time):
    service = fixture(tmp_path, monkeypatch, buy_code=1, sell_code=sell_code)
    before = custom_signals.load_all(tmp_path)
    result = service.run(config())
    assert not result.error, result.error
    assert result.stats["n_trades"] == 1
    trade, = result.trades
    assert trade["entry_date"] == f"{BUY} 14:06"
    assert trade["exit_date"] == f"{SELL} {exit_time}"
    assert trade["exit_signal_id"] == "csgi_" + routed.EXIT_IDS[branch]
    assert all(p["entry_date"] == SELL for p in result.stats["execution"]["remaining_positions"])
    assert all(fill["participation"] <= .1 for fill in result.fills)
    assert custom_signals.load_all(tmp_path) == before


@pytest.mark.parametrize("buy_code,entry_time", [(2, "14:15"), (3, "14:41")])
def test_common_replay_selects_only_target_days_entry_signal(tmp_path, monkeypatch, buy_code, entry_time):
    service = fixture(tmp_path, monkeypatch, buy_code=buy_code, sell_code=1)
    result = service.run(config())
    assert not result.error, result.error
    assert result.trades[0]["entry_date"] == f"{BUY} {entry_time}"


def test_daily_branch_four_is_valid_zero_with_no_positions(tmp_path, monkeypatch):
    result = fixture(tmp_path, monkeypatch, buy_code=4, sell_code=4).run(config())
    assert not result.error, result.error
    assert result.stats["n_trades"] == 0 and result.stats["final_equity"] == 1_000_000
    assert not result.fills


def test_system_s05_replays_saved_symbols_and_carries_the_original_branch(tmp_path, monkeypatch):
    service = fixture(tmp_path, monkeypatch, buy_code=1, sell_code=1)
    # The reconstruction fixture would choose branch 1 and trade both days.
    # The frozen job must ignore it and recompute branch 2 using only the archive.
    service.s05_selection_archive = {
        str(day): {
            "snapshot": routed.freeze_market_route(
                {"as_of": str(day), "source_date": str(routed.sessions_until(day)[-1]),
                 "market_breadth": breadth(day, code), "entry_execution": "common_template_v1",
                 "rows": [{"symbol": SYMBOL, "reference_close": 10., "s02_score": 82.,
                           "s02_fund_score": 95., "s02_tech_score": 45., "s02_rank": 1}]},
                day, routed.MARKET_DEFAULTS, observed=datetime.combine(
                    routed.sessions_until(day)[-1], datetime.min.time().replace(hour=22))),
            "evidence": {"record_id": f"saved:{day}", "source_sha256": "abc", "manual_replay": True},
        }
        for day, code in ((BUY, 2), (SELL, 4))
    }
    result = service.run(config(s05_exit_policy="carry_entry_branch_v2"))
    assert not result.error, result.error
    buy, = [hit for hit in result.signals if hit["side"] == "buy"]
    assert buy["symbol"] == SYMBOL and buy["signal_time"] == f"{BUY} 14:15"
    assert buy["signal_score"] == pytest.approx(80)
    assert buy["selection_rank"] == 1
    trade, = result.trades
    assert trade["exit_signal_id"] == "csgi_low_gap_exit_s05"
    assert [item["branch_code"] for item in result.provenance["s05_selection_replay"]] == [2, 4]
    assert result.provenance["s05_selection_replay"][0]["archive"]["record_id"] == f"saved:{BUY}"

    # Absence is a failed experiment, never a fresh S05-universe reconstruction.
    service.s05_selection_archive.clear()
    missing = service.run(config(s05_exit_policy="carry_entry_branch_v2"))
    assert missing.error and "预选归档" in missing.error
    assert not missing.fills


@pytest.mark.parametrize("sell_code", [2, 4])
def test_entry_branch_controls_t_plus_one_even_on_other_or_no_buy_branch(tmp_path, monkeypatch, sell_code):
    service = fixture(tmp_path, monkeypatch, buy_code=1, sell_code=sell_code)
    cfg = config()
    cfg.s05_exit_policy = "carry_entry_branch_v2"
    result = service.run(cfg)
    assert not result.error, result.error
    trade, = result.trades
    assert trade["exit_date"] == f"{SELL} 10:01"
    assert trade["exit_signal_id"] == "csgi_tail_exit_s05"
    if sell_code == 4:
        assert not [f for f in result.fills if f["date"] == str(SELL) and f["side"] == "buy"]


@pytest.mark.parametrize("strict", [False, True])
def test_common_s05_replay_uses_frozen_research_or_own_postclose_clock(tmp_path, monkeypatch, strict):
    from app.market_time import CN_TZ
    from app.services import s05_scoring
    from app.services.selection_clock import window_for

    service = fixture(tmp_path, monkeypatch)
    if strict:
        # Isolate the scoring clock from the separate whole-dataset certification gate.
        monkeypatch.setattr("app.backtest.morning_backtest.research_preflight", lambda *args: [])
    original = s05_scoring.compute_selection
    observations = []

    def score(repo, snapshot, source_day, day, observed, **kwargs):
        expected = (datetime.combine(source_day, window_for(routed.SID)[0], CN_TZ)
                    if strict else datetime.fromisoformat(f"{SELL}T23:00:00+08:00"))
        assert observed == expected, "S05 scoring must distinguish frozen research and its 22:00 cutoff"
        observations.append((source_day, day, observed))
        return original(repo, snapshot, source_day, day, observed, **kwargs)

    monkeypatch.setattr(s05_scoring, "compute_selection", score)
    result = service.run(config(strict_research=strict))
    assert not result.error, result.error
    assert [day for _, day, _ in observations] == [BUY, SELL]
    assert result.stats["n_trades"] == 1
    provenance = result.provenance["s05_selection_replay"]
    assert all(row["scoring_provenance"]["observation_policy"] == (
        "source_day_observed" if strict else "retained_research_snapshot") for row in provenance)
    assert all(row["scoring_provenance"]["input_snapshot_id"] == "s05-frozen-test-input"
               for row in provenance)
    assert bool(any("后来采集" in warning for warning in result.stats["warnings"])) is not strict


def test_common_s05_replay_requires_frozen_input_identity(tmp_path, monkeypatch):
    service = fixture(tmp_path, monkeypatch)
    (tmp_path / ".research_snapshot.json").unlink()
    result = service.run(config())
    assert result.error and "冻结" in result.error
    assert not result.fills and not result.equity_curve


def test_observed_parquet_uses_public_snapshot_and_strict_rejects_later_collection(tmp_path):
    from app.backtest.minute_replay import _s05_research_observation
    from app.services.ext_data import ExtConfig, ExtField
    from app.services.s03_selection import _extension
    from app.strategy.basic_filter import previous_session

    source = previous_session(BUY)
    frozen = datetime.fromisoformat(f"{SELL}T23:00:00+08:00")
    (tmp_path / ".public_release.json").write_text(json.dumps({
        "id": "s05-public-frozen-input", "published_at": frozen.timestamp(),
    }))
    (tmp_path / ".research_snapshot.json").write_text(json.dumps({
        "id": "unused-input", "created_at": f"{BUY}T20:00:00+08:00",
    }))
    fields = ["super_large_net_inflow", "large_net_inflow"]
    config = ExtConfig("ext_sina_money_flow", "funds", "timeseries",
                       [ExtField(name, "float") for name in fields])
    root = tmp_path / "ext_data" / config.id
    root.mkdir(parents=True)
    (root / "config.json").write_text(json.dumps(config.to_dict()))
    partition = root / f"timeseries/date={source}/part.parquet"
    partition.parent.mkdir(parents=True)
    pl.DataFrame([{
        "symbol": SYMBOL, "date": source, "super_large_net_inflow": 3e6,
        "large_net_inflow": 2e6, "observed_at": stamp,
    } for stamp in (f"{BUY}T20:30:00+08:00", f"{SELL}T23:01:00+08:00")]).write_parquet(partition)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    observed, provenance = _s05_research_observation(repo, source, False)
    assert observed == frozen and provenance["input_snapshot_id"] == "s05-public-frozen-input"
    frame, evidence = _extension(tmp_path, fields, source, source, observed, config_id=config.id)
    assert frame.height == 1 and evidence["partitions"][0]["sha256"]
    strict, provenance = _s05_research_observation(repo, source, True)
    assert provenance["observation_policy"] == "source_day_observed"
    with pytest.raises(ValueError, match="无有效观察"):
        _extension(tmp_path, fields, source, source, strict, config_id=config.id)


def test_disabled_selected_exit_and_mixed_trigger_still_reject(tmp_path, monkeypatch):
    service = fixture(tmp_path, monkeypatch)
    signal = next(s for s in custom_signals.load_all(tmp_path) if s["id"] == routed.EXIT_IDS["low_gap"])
    custom_signals.save_one(tmp_path, {**signal, "enabled": False})
    disabled = service.run(config())
    assert disabled.error and "禁用" in disabled.error
    assert not disabled.fills
    cfg = config()
    cfg.overrides["exit_signals"] = [*service.strategy_engine.get(routed.SID).exit_signals,
                                    "signal_ma5_breakdown"]
    mixed = service.run(cfg)
    assert mixed.error and "混合" in mixed.error
    assert not mixed.fills


def test_insufficient_exit_capacity_preserves_fills_and_rejects_nav(tmp_path, monkeypatch):
    result = fixture(tmp_path, monkeypatch, sell_volume=1.).run(config())
    assert result.error and "余股" in result.error
    assert any(f["side"] == "buy" for f in result.fills)
    assert not result.equity_curve
    assert result.stats["execution"]["remaining_positions"]
