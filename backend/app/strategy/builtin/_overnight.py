"""有界隔夜分钟规则。纯行情/持仓函数, 选股、监控与回测共用。"""
from datetime import datetime

import numpy as np
import polars as pl

EXECUTION = "overnight_capacity_v1"

# 比例均为小数; 成交量为手, 成交额为元, 分钟标签为完成时间。
PARAMS = [
    {"id": key, "label": label, "type": kind, "default": value, "group": group,
     "step": .0001 if kind == "float" and value <= 1 else 1}
    for group, entries in [
        ("前日与尾盘选股", [
            ("prior_price_min", "前收盘下限(元)", "float", 5.),
            ("prior_price_max", "前收盘上限(元)", "float", 100.),
            ("prior_return_min", "前日涨幅下限", "float", .001),
            ("prior_return_max", "前日涨幅上限", "float", .08),
            ("prior_range_max", "前日振幅上限", "float", .04),
            ("amount_min", "前日及当日成交额下限", "float", 50_000_000.),
            ("listing_days", "上市自然日下限", "int", 60),
            ("current_return_min", "当日涨幅下限", "float", 0.),
            ("current_return_max", "当日涨幅上限", "float", .02),
            ("current_range_max", "当日振幅上限", "float", .03),
            ("market_min_valid", "市场有效证券下限", "int", 1000),
            ("market_positive_min", "市场上涨比例下限", "float", .5),
        ]),
        ("完整早盘卖出", [
            ("vwap_buffer", "低于早盘均价幅度", "float", .001),
            ("peak_profit_min", "早盘最高浮盈门槛", "float", .005),
            ("peak_retrace_min", "早盘高点回落门槛", "float", .003),
            ("return_15m_max", "最后15分钟涨幅上限", "float", 0.),
            ("decline_3m", "3分钟跌幅门槛", "float", .003),
            ("volume_ratio_min", "最近3根/之前10根量比", "float", 1.5),
            ("guard_time", "兜底信号时间", "string", "11:22"),
            ("sell_deadline", "卖出成交截止时间", "string", "13:30"),
        ]),
        ("分钟成交与资金", [
            ("initial_cash", "初始现金(元)", "float", 100_000.),
            ("entry_nav_fraction", "单票含费净值比例", "float", .2),
            ("entry_budget", "额外单票上限(元,0不限制)", "float", 0.),
            ("position_limit", "同时持仓上限", "int", 5),
            ("participation", "分钟成交量参与率", "float", .1),
            ("slippage", "买卖各自不利滑点", "float", .0005),
            ("commission", "买卖佣金率", "float", .00025),
            ("minimum_commission", "汇总委托最低佣金(元)", "float", 5.),
            ("transfer_fee", "买卖过户费率", "float", .00001),
            ("stamp_tax", "卖出印花税率", "float", .0005),
        ]),
    ] for key, label, kind, value in entries
]
DEFAULTS = {p["id"]: p["default"] for p in PARAMS}


def suspended_on(symbol, day, params):
    """只有已公布的有来源停牌事实可授权缺少分钟成交。"""
    if params.get("suspension_policy") != "wait_resume":
        return False
    known = []
    for event in params.get("suspension_events", []):
        if (not isinstance(event.get("suspended"), bool) or not event.get("source")
                or not event.get("available_date") or not event.get("date")):
            raise ValueError("停牌事实需要日期、可用日期、布尔状态与来源")
        if (event["symbol"] == symbol and event["date"] <= str(day)
                and event["available_date"] <= str(day)):
            known.append(event)
    return sorted(known, key=lambda e: (e["date"], e["available_date"]))[-1]["suspended"] if known else False


def parameters(values):
    params = {**DEFAULTS, **(values or {})}
    if "transfer_fee_fixed" in params:
        value = float(params["transfer_fee_fixed"])
        if isinstance(params["transfer_fee_fixed"], bool) or not np.isfinite(value) or value < 0:
            raise ValueError("固定费用须为有效非负数")
        params["transfer_fee_fixed"] = value
    for definition in PARAMS:
        key = definition["id"]
        if definition["type"] == "string":
            continue
        value = float(params[key])
        if not np.isfinite(value) or (value < 0 and key != "return_15m_max"):
            raise ValueError("隔夜参数必须是有效非负数: " + key)
        if definition["type"] == "int" and value != int(value):
            raise ValueError("隔夜整数参数无效: " + key)
        params[key] = int(value) if definition["type"] == "int" else value
    guard = minute_number(params["guard_time"])
    deadline = minute_number(params["sell_deadline"])
    momentum = params.get("_execution") == "momentum_capacity_v1"
    if not ((571 <= guard <= 690 or 781 <= guard <= 900) if momentum else 675 <= guard <= 688):
        raise ValueError("兜底信号时间必须在11:15~11:28")
    if not (guard <= deadline <= 900 if momentum else guard < deadline <= 900) or 691 <= deadline <= 780:
        raise ValueError("卖出截止必须晚于兜底信号且在收盘前的交易分钟, 不得位于午休")
    if not 0 < params["participation"] <= 1:
        raise ValueError("分钟参与率必须大于0且不超过1")
    if params["position_limit"] < 1 or params["initial_cash"] <= 0:
        raise ValueError("资金与持仓上限必须大于0")
    if not 0 < params["entry_nav_fraction"] <= 1:
        raise ValueError("单票净值比例必须大于0且不超过1")
    if params["market_min_valid"] < 1 or params["market_positive_min"] > 1:
        raise ValueError("市场有效证券数和上涨比例无效")
    if params["slippage"] >= 1 or any(params[x] > 1 for x in ("commission", "transfer_fee", "stamp_tax")):
        raise ValueError("费用及滑点必须使用小数比例")
    for lower, upper in (("prior_price_min", "prior_price_max"),
                         ("prior_return_min", "prior_return_max"),
                         ("current_return_min", "current_return_max")):
        if params[lower] > params[upper]:
            raise ValueError("隔夜区间下限不能超过上限")
    return params


def entry_budget(nav, cash, values):
    params = parameters(values)
    budget = min(cash, nav * params["entry_nav_fraction"])
    return min(budget, params["entry_budget"]) if params["entry_budget"] else budget


def minute_number(value):
    text = str(value)
    parsed = datetime.strptime(text, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def minute_frame(frame):
    if frame.is_empty():
        return frame
    stamp = pl.col("datetime")
    if frame.schema["datetime"].time_zone is not None:
        stamp = stamp.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
    return frame.with_columns(
        stamp.alias("datetime"),
        (stamp.dt.hour().cast(pl.Int32) * 60 + stamp.dt.minute().cast(pl.Int32)).alias("bar"),
    ).sort("symbol", "datetime")


def valid_rows(rows, required, fields):
    if any(t not in rows for t in required):
        return False
    try:
        return all(
            np.isfinite(float(rows[t][field]))
            and (float(rows[t][field]) >= 0 if field in ("volume", "amount") else float(rows[t][field]) > 0)
            for t in required for field in fields
        )
    except (TypeError, ValueError, KeyError):
        return False


def morning_features(frame, entry_unit_cost):
    if frame.is_empty() or not {"datetime", "open", "high", "low", "close", "volume", "amount"}.issubset(frame.columns):
        return None
    rows = {x["bar"]: x for x in minute_frame(frame).to_dicts() if 571 <= x["bar"] <= 675}
    if len(rows) != frame.filter(
        pl.col("datetime").dt.time().is_between(
            datetime.strptime("09:31", "%H:%M").time(), datetime.strptime("11:15", "%H:%M").time()
        )
    ).height:
        return None
    required = list(range(571, 676))
    if not valid_rows(rows, required, ("open", "high", "low", "close", "volume", "amount")):
        return None
    if entry_unit_cost is None or not np.isfinite(entry_unit_cost) or entry_unit_cost <= 0:
        return None
    shares = sum(float(rows[t]["volume"]) * 100 for t in required)
    amount = sum(float(rows[t]["amount"]) for t in required)
    if shares <= 0 or amount <= 0:
        return None
    close, high = float(rows[675]["close"]), max(float(rows[t]["high"]) for t in required)
    return {"close_1115": close, "vwap_0930_1115": amount / shares,
            "vwap_deviation": close / (amount / shares) - 1,
            "return_15m": close / float(rows[660]["close"]) - 1,
            "morning_high": high, "entry_unit_cost": entry_unit_cost,
            "peak_profit": high / entry_unit_cost - 1, "retrace_from_peak": 1 - close / high}


def exit_signal(frame, asof_bar, entry_unit_cost, values, pending=None):
    if pending is not None:
        return dict(pending)
    params = parameters(values)
    guard = minute_number(params["guard_time"])
    deadline = minute_number(params["sell_deadline"])
    if not 675 <= asof_bar <= deadline:
        return None
    signal = {"signal_bar": asof_bar, "first_eligible_bar": asof_bar + 1,
              "last_eligible_bar": deadline, "ret_3m": None, "volume_ratio_3m": None}
    if asof_bar >= guard:
        return dict(signal, reason="time_guard")
    if frame.is_empty() or not {"datetime", "close", "volume"}.issubset(frame.columns):
        return None
    if asof_bar == 675:
        features = morning_features(frame, entry_unit_cost)
        if features and features["return_15m"] <= params["return_15m_max"] and (
            features["vwap_deviation"] <= -params["vwap_buffer"]
            or (features["peak_profit"] >= params["peak_profit_min"]
                and features["retrace_from_peak"] >= params["peak_retrace_min"])
        ):
            return dict(signal, reason="morning_vwap_or_trailing")
    source = minute_frame(frame)
    rows = {x["bar"]: x for x in source.to_dicts() if x["bar"] <= asof_bar}
    if source.filter(pl.col("bar").is_between(asof_bar - 12, asof_bar)).height != 13:
        return None
    if not valid_rows(rows, list(range(asof_bar - 12, asof_bar + 1)), ("close", "volume")):
        return None
    prior = sum(float(rows[t]["volume"]) for t in range(asof_bar - 12, asof_bar - 2)) / 10
    if prior <= 0:
        return None
    ratio = sum(float(rows[t]["volume"]) for t in range(asof_bar - 2, asof_bar + 1)) / 3 / prior
    ret = float(rows[asof_bar]["close"]) / float(rows[asof_bar - 3]["close"]) - 1
    if ret <= -params["decline_3m"] and ratio >= params["volume_ratio_min"]:
        return dict(signal, reason="volume_decline", ret_3m=ret, volume_ratio_3m=ratio)
    return None


def select_entries(frame, values, daily=None):
    empty = pl.DataFrame(schema={"symbol": pl.String, "close": pl.Float64, "score": pl.Float64})
    params = parameters(values)
    if frame.is_empty() or daily is None or daily.is_empty():
        return empty
    frame = minute_frame(frame)
    day = frame["datetime"].max().date()
    cutoff = params.get("_s01_entry_bar", 885)
    environment_only = params.get("_s01_environment_only", False)
    frame = frame.filter((pl.col("datetime").dt.date() == day) & (
        pl.col("bar").is_between(571, 690) | pl.col("bar").is_between(781, cutoff)))
    required = list(range(571, 691)) + list(range(781, cutoff + 1))
    if (params.get("_s01_external_basic")
            and params.get("market_validity") == "price_amount_prev_close_v2"):
        # Current signal market scope is independent of individual complete-window checks.
        fields = ("open", "high", "low", "close", "amount")
        if not set(fields).issubset(frame.columns) or not {"date", "raw_close"}.issubset(daily.columns):
            return empty
        market = frame.group_by("symbol").agg(
            count=pl.len(), unique=pl.col("bar").n_unique(),
            valid=pl.all_horizontal([
                pl.col(f).is_finite() & (pl.col(f) >= 0 if f == "amount" else pl.col(f) > 0)
                for f in fields
            ]).fill_null(False).all(),
            close=pl.col("close").last(), last_datetime=pl.col("datetime").last(),
            high=pl.col("high").max(), low=pl.col("low").min(), amount=pl.col("amount").sum(),
        ).filter(pl.col("valid") & (pl.col("count") == pl.col("unique")))
        previous = daily.filter(pl.col("date") == params["_s01_previous_day"]).select(
            "symbol", pl.col("raw_close").alias("prior_close"),
            *[f for f in ("prior_ex_date",) if f in daily.columns],
        ).unique("symbol", keep="last")
        market = market.join(previous, on="symbol", how="inner").filter(
            pl.col("prior_close").is_finite() & (pl.col("prior_close") > 0)
            & (pl.col("symbol").str.ends_with(".SH") | pl.col("symbol").str.ends_with(".SZ"))
        ).with_columns(
            (pl.col("close") / pl.col("prior_close") - 1).alias("current_return"),
            ((pl.col("high") - pl.col("low")) / pl.col("prior_close")).alias("current_range"),
        )
        if environment_only:
            return market
        # The adapter already checked the market using all valid securities.
        condition = pl.col("count") == len(required)
        if not params.get("_s01_skip_entry"):
            condition &= (
                pl.col("current_return").is_between(params["current_return_min"], params["current_return_max"])
                & (pl.col("current_range") <= params["current_range_max"])
                & (pl.col("amount") >= params["current_amount_min"]))
        if "prior_ex_date" in market.columns:
            condition &= ~pl.col("prior_ex_date").fill_null(False)
        return market.filter(condition).with_columns(
            (-pl.col("current_range") + .001 * pl.col("amount").log()).alias("score"),
        ).sort(["score", "symbol"], descending=[True, False])
    # 完整、唯一、标准分钟窗口。未知价格/金额不参与市场分母。
    good = frame.group_by("symbol").agg(
        count=pl.len(), unique=pl.col("bar").n_unique(),
        valid=pl.all_horizontal([
            pl.col(f).is_finite() & (pl.col(f) >= 0 if f in ("amount", "volume") else pl.col(f) > 0)
            for f in ("open", "high", "low", "close", "amount", "volume")
        ]).fill_null(False).all(),
        close=pl.col("close").last(), last_datetime=pl.col("datetime").last(),
        **({} if environment_only else {
            "high": pl.col("high").max(), "low": pl.col("low").min(), "amount": pl.col("amount").sum()}),
    ).filter((pl.col("count") == len(required)) & (pl.col("unique") == len(required)) & pl.col("valid"))
    history = daily.filter(pl.col("date") < day).sort("symbol", "date")
    if not {"raw_close", "raw_high", "raw_low", "amount"}.issubset(history.columns):
        return empty
    prior = history.group_by("symbol", maintain_order=True).tail(2)
    if prior.is_empty():
        return empty
    info = prior.group_by("symbol").agg(
        *[pl.col(f).last().alias(f) for f in ("name", "listing_age_days", "is_risk_warning",
                                             "prior_ex_date") if f in prior.columns],
        n=pl.len(), prior_close=pl.col("raw_close").last(),
        previous_close=pl.col("raw_close").first(), prior_high=pl.col("raw_high").last(),
        prior_low=pl.col("raw_low").last(), prior_amount=pl.col("amount").last(),
        age_increment=pl.lit(day) - pl.col("date").last(),
    ).filter(pl.col("n") == 2)
    if "listing_age_days" in info.columns:
        info = info.with_columns(
            (pl.col("listing_age_days") + pl.col("age_increment").dt.total_days()).alias("listing_age_days"))
    good = good.join(info, on="symbol", how="inner").with_columns(
        (pl.col("close") / pl.col("prior_close") - 1).alias("current_return"),
        (pl.col("prior_close") / pl.col("previous_close") - 1).alias("prior_return"),
        ((pl.col("prior_high") - pl.col("prior_low")) / pl.col("previous_close")).alias("prior_range"),
    ).filter(pl.col("prior_close").is_finite() & (pl.col("prior_close") > 0))
    market = good.filter(pl.col("symbol").str.ends_with(".SH") | pl.col("symbol").str.ends_with(".SZ"))
    if environment_only:
        return market
    market = market.with_columns(
        ((pl.col("high") - pl.col("low")) / pl.col("prior_close")).alias("current_range"))
    if not params.get("_s01_skip_entry") and (market.height < params["market_min_valid"] or (
        market.filter(pl.col("current_return") > 0).height / market.height < params["market_positive_min"]
    )):
        return empty
    condition = pl.lit(True) if params.get("_s01_external_basic") else (
        pl.col("prior_close").is_between(params["prior_price_min"], params["prior_price_max"])
        & pl.col("prior_return").is_between(params["prior_return_min"], params["prior_return_max"])
        & (pl.col("prior_range") <= params["prior_range_max"])
        & (pl.col("prior_amount") >= params["amount_min"])
    )
    if not params.get("_s01_skip_entry"):
        condition &= (
            pl.col("current_return").is_between(params["current_return_min"], params["current_return_max"])
            & (pl.col("current_range") <= params["current_range_max"])
            & (pl.col("amount") >= (params["current_amount_min"] if "_s01_entry_bar" in params
                                   else params["amount_min"])))
    if not params.get("_s01_external_basic"):
        if "listing_age_days" not in good.columns:
            return empty
        condition &= pl.col("listing_age_days") >= params["listing_days"]
        if "name" in good.columns:
            condition &= ~pl.col("name").fill_null("").str.to_uppercase().str.contains("ST")
        if "is_risk_warning" in good.columns:
            condition &= ~pl.col("is_risk_warning").fill_null(False)
    if "prior_ex_date" in good.columns:
        condition &= ~pl.col("prior_ex_date").fill_null(False)
    return market.filter(condition).with_columns(
        (-pl.col("current_range") + .001 * pl.col("amount").log()).alias("score"),
    ).sort(["score", "symbol"], descending=[True, False])
