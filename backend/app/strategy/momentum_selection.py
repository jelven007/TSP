"""14:30点时全量选股; 只使用已公告股本与前20个交易日成交额。"""
import polars as pl

from app.strategy.builtin._overnight import PARAMS as COST_PARAMS
from app.strategy.builtin._overnight import minute_frame
from app.strategy.momentum_rules import parameters

_FIELDS = [
    ("prior_return_min", "前日涨幅下限", .005), ("prior_return_max", "前日涨幅上限", .06),
    ("current_return_min", "14:30涨幅下限", .004), ("current_return_max", "14:30涨幅上限", .04),
    ("float_cap_min", "14:30流通市值下限(元)", 5e9),
    ("float_cap_max", "14:30流通市值上限(元)", 50e9),
    ("amount20_min", "前20交易日均额下限(元)", 1e8),
    ("market_positive_min", "指定市值池上涨比例下限", .6),
    ("stop_loss_pct", "扣费净止损比例", .06), ("take_profit_pct", "毛止盈比例", .06),
]
_DEFAULT = {k: v for k, _, v in _FIELDS}
_COST_KEYS = {"initial_cash", "entry_nav_fraction", "entry_budget", "position_limit",
              "participation", "slippage", "commission", "minimum_commission", "transfer_fee",
              "stamp_tax", "guard_time", "sell_deadline"}
PARAMS = [
    {"id": key, "label": label, "type": "float", "default": value, "group": "选股与退出",
     "display_scale": 100 if value < 1 else 1, "unit": "%" if value < 1 else "元",
     "step": .1 if value < 1 else 1}
    for key, label, value in _FIELDS
] + [
    {**p, "default": {"commission": .0002, "transfer_fee": 0., "participation": .5,
                      "guard_time": "10:30", "sell_deadline": "11:30"}.get(p["id"], p["default"])}
    for p in COST_PARAMS if p["id"] in _COST_KEYS
]


def daily_context(daily):
    if daily is None or daily.is_empty():
        return pl.DataFrame()
    f = daily.sort("symbol", "date")
    if "reference_close" in f.columns:
        return f
    raw = "raw_close" if "raw_close" in f.columns else "close"
    if "adj_factor" not in f.columns:
        f = f.with_columns(pl.lit(1.).alias("adj_factor"))
    return f.with_columns(
        (pl.col(raw).shift(1).over("symbol")
         * pl.col("adj_factor").shift(1).over("symbol") / pl.col("adj_factor")).alias("reference_close"),
        pl.col("date").shift(1).over("symbol").alias("previous_row_date"),
        pl.col("date").shift(2).over("symbol").alias("before_previous_row_date"),
        pl.col("amount").rolling_mean(20).over("symbol").shift(1).over("symbol").alias("prev_d_amount20"),
    ).with_columns(
        (pl.col(raw) / pl.col("reference_close") - 1).alias("day_return"),
    ).with_columns(pl.col("day_return").shift(1).over("symbol").alias("previous_return"))


def select_entries(minute, values, *, daily=None, members=None):
    p = {**parameters(values), **_DEFAULT, **(values or {})}
    if minute.is_empty() or daily is None or daily.is_empty():
        return pl.DataFrame()
    f = minute_frame(minute)
    day = f["datetime"].max().date()
    scope = p.get("market_scope") or {}
    members = members if members is not None else scope.get("members")
    if members is None:
        raise ValueError("策略缺少已持久化的指定股票池")
    live = [r[0] for r in members if r[1] is None or str(r[1]) <= str(day)]
    f = f.filter((pl.col("datetime").dt.date() == day) & pl.col("symbol").is_in(live)
                 & pl.col("bar").is_between(831, 870))
    if f.is_empty():
        return pl.DataFrame()
    if f.select(pl.struct("symbol", "datetime").n_unique()).item() != f.height:
        raise ValueError("选股窗口含重复分钟")
    valid = (pl.all_horizontal([pl.col(c).is_finite() & (pl.col(c) > 0)
                               for c in ("open", "high", "low", "close")])
             & pl.col("volume").is_finite() & (pl.col("volume") >= 0)
             & (pl.col("high") + 1e-8 >= pl.max_horizontal("open", "close", "low"))
             & (pl.col("low") - 1e-8 <= pl.min_horizontal("open", "close", "high"))).fill_null(False)
    f = f.filter(valid)
    snap = f.filter(pl.col("bar") == 870).select(
        "symbol", pl.col("datetime").alias("last_datetime"),
        pl.col("close").alias("price"), pl.col("volume").alias("signal_volume"))
    panel = daily_context(daily)
    # 当日行只提供除权参考和已公告股本; 价格和成交额条件均来自过去。
    today = panel.filter(pl.col("date") == day)
    if today.is_empty():
        prior = panel.filter(pl.col("date") < day).group_by("symbol", maintain_order=True).tail(1)
        raw = "raw_close" if "raw_close" in prior.columns else "close"
        today = prior.with_columns(
            pl.col(raw).alias("reference_close"), pl.col("day_return").alias("previous_return"),
            pl.col("date").alias("previous_row_date"),
            pl.col("previous_row_date").alias("before_previous_row_date"),
        )
        amounts = panel.filter(pl.col("date") < day).group_by("symbol", maintain_order=True).tail(20).group_by("symbol").agg(
            n=pl.len(), prev_d_amount20=pl.col("amount").mean())
        today = today.drop("prev_d_amount20").join(amounts.filter(pl.col("n") == 20), on="symbol", how="inner")
    required = ["symbol", "reference_close", "previous_return", "previous_row_date",
                "before_previous_row_date", "prev_d_amount20", "float_shares"]
    if not set(required).issubset(today.columns):
        raise ValueError("选股缺少原始价、前20日成交额或公告股本")
    if "name" in today.columns:
        required.append("name")
    x = snap.join(today.select(*required), on="symbol", how="inner").with_columns(
        (pl.col("price") / pl.col("reference_close") - 1).alias("return_1430"),
        (pl.col("price") * pl.col("float_shares")).alias("float_cap_1430"),
    ).filter(pl.col("reference_close").is_finite() & (pl.col("reference_close") > 0)
             & pl.col("float_cap_1430").is_between(p.get("float_cap_min", 5e9), p.get("float_cap_max", 50e9)))
    if x.is_empty():
        return x
    breadth = x.select((pl.col("return_1430") > 0).mean()).item()
    pivot = f.select("symbol", "bar", "close").pivot(on="bar", index="symbol", values="close")
    for bar in range(831, 871):
        if str(bar) not in pivot.columns:
            pivot = pivot.with_columns(pl.lit(None).cast(pl.Float64).alias(str(bar)))
    pivot = pivot.with_columns(
        pl.all_horizontal([pl.col(str(i)).is_not_null() for i in range(831, 871)]).alias("m5_complete"),
        pl.all_horizontal([pl.col(str(i)).is_not_null() for i in range(860, 871)]).alias("m1_complete"),
        pl.all_horizontal([pl.col(str(i + 5)) - pl.col(str(i)) > 1e-10 for i in range(860, 866)]).alias("m1_rising"),
        pl.all_horizontal([pl.col(str(835 + (i + 5) * 5)) - pl.col(str(835 + i * 5)) > 1e-10 for i in range(3)]).alias("m5_rising"),
    )
    x = x.join(pivot.select("symbol", "m1_complete", "m5_complete", "m1_rising", "m5_rising"),
               on="symbol", how="inner").with_columns(
        pl.lit(breadth).alias("market_breadth"), pl.lit(x.height).alias("breadth_denominator"))
    from app.market_time import published_trading_days
    past_days = sorted(d for y in (day.year - 1, day.year) for d in published_trading_days(y) if d < day)
    if len(past_days) < 2:
        raise ValueError("选股缺少交易日历")
    return x.filter(
        pl.col("previous_return").is_between(p["prior_return_min"] - 1e-12, p["prior_return_max"] + 1e-12)
        & pl.col("return_1430").is_between(p["current_return_min"] - 1e-12, p["current_return_max"] + 1e-12)
        & (pl.col("previous_row_date") == past_days[-1])
        & (pl.col("before_previous_row_date") == past_days[-2])
        & (pl.col("prev_d_amount20") >= p.get("amount20_min", 1e8))
        & (pl.col("market_breadth") >= p["market_positive_min"])
        & (pl.col("signal_volume") > 0) & pl.col("m1_complete") & pl.col("m5_complete")
        & pl.col("m1_rising") & pl.col("m5_rising")
    ).with_columns(pl.col("price").alias("close")).sort("symbol")


def prioritized_selection(rows, values):
    """完整选股列表沿用买入优先级, 不截断达标股票。"""
    if rows.is_empty():
        return rows
    limit = int(parameters(values)["position_limit"])
    key = "return_1430" if rows.height > limit else "prev_d_amount20"
    return rows.sort([key, "symbol"], descending=[True, False], nulls_last=True)


def ranked_candidates(rows, values):
    return prioritized_selection(rows, values).head(int(parameters(values)["position_limit"]))
