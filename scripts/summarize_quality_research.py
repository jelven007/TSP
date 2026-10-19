"""Read-only post-study diagnostics; never selects or rewrites production settings."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def metrics(trades):
    if not trades:
        return {"trades": 0, "net_pnl": 0, "win_rate": None, "expectancy": None, "profit_factor": None}
    money = np.array([t["pnl_amount"] for t in trades])
    returns = np.array([t["pnl_pct"] for t in trades])
    loss = -money[money < 0].sum()
    return {
        "trades": len(trades), "net_pnl": float(money.sum()),
        "win_rate": float((returns > 0).mean()), "expectancy": float(returns.mean()),
        "profit_factor": float(money[money > 0].sum() / loss) if loss else None,
    }


def board(symbol):
    code = symbol.split(".")[0]
    if code.startswith("688"):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    return "沪主板" if symbol.endswith(".SH") else "深主板"


def check_trade_cash(trade, config):
    """Independent arithmetic audit of recorded cash, including minimum fees."""
    buy = trade["entry_price"] * trade["shares"]
    sell = trade["exit_price"] * trade["shares"]
    commission = config["commission_pct"]
    minimum = config["minimum_commission"]
    tax = config.get("stamp_tax_pct")
    if tax is None:
        tax = .0005 if trade["exit_date"][:10] >= "2023-08-28" else .001
    extra = config["transfer_fee_pct"] + config["slippage_bps"] / 10000
    entry = buy + max(minimum, buy * commission) + buy * extra
    exit_ = sell - max(minimum, sell * commission) - sell * (extra + tax)
    # Prices and quantities are rounded to four decimals by the public API.
    # Propagate half a last decimal through multiplication and the cost factor.
    gross_rounding = .00005 * (abs(trade["shares"]) + max(trade["entry_price"], trade["exit_price"])) + .00005**2
    tolerance = .011 + gross_rounding * (1 + commission + extra + tax)
    return {
        "symbol": trade["symbol"], "entry_date": trade["entry_date"],
        "exit_date": trade["exit_date"], "exit_reason": trade["exit_reason"],
        "entry_error": round(entry - trade["entry_value"], 6),
        "exit_error": round(exit_ - trade["exit_value"], 6),
        "pnl_error": round(trade["exit_value"] - trade["entry_value"] - trade["pnl_amount"], 6),
        "within_rounding": abs(entry - trade["entry_value"]) <= tolerance
        and abs(exit_ - trade["exit_value"]) <= tolerance
        and abs(trade["exit_value"] - trade["entry_value"] - trade["pnl_amount"]) <= .021,
    }


class Prices:
    def __init__(self, output):
        manifests = list((output / "matrix-cache").glob("v*/manifest.json"))
        if len(manifests) != 1:
            raise ValueError("需要唯一且完整的批次矩阵缓存")
        path = manifests[0]
        manifest = json.loads(path.read_text())
        self.dates = {day[:10]: i for i, day in enumerate(manifest["timestamp_labels"])}
        self.symbols = {symbol: i for i, symbol in enumerate(manifest["symbols"])}
        self.arrays = {}
        for name, spec in {**manifest["arrays"], **manifest["fields"]}.items():
            if name in {"open", "close", "high", "low", "amount"}:
                self.arrays[name] = np.memmap(
                    path.parent / "matrix.bin", dtype=spec["dtype"], mode="r",
                    offset=spec["offset"], shape=tuple(spec["shape"]),
                )

    def enrich(self, trade):
        entry = self.dates[trade["entry_date"][:10]]
        exit_ = self.dates[trade["exit_date"][:10]]
        signal = self.dates.get(str(trade.get("entry_signal_date"))[:10])
        symbol = self.symbols[trade["symbol"]]
        price = trade["entry_price"]
        # Daily H/L on the exit day can be after a stop/open sale. Exclude that
        # day and add the actual execution endpoint; these are bounded diagnostics.
        high = np.r_[price, self.arrays["high"][entry:exit_, symbol], trade["exit_price"]]
        low = np.r_[price, self.arrays["low"][entry:exit_, symbol], trade["exit_price"]]
        gap = None
        amount = None
        median_amount = None
        if signal is not None:
            close = self.arrays["close"][signal, symbol]
            if np.isfinite(close) and close > 0:
                gap = float(price / close - 1)
            if "amount" in self.arrays:
                value = self.arrays["amount"][signal, symbol]
                if np.isfinite(value):
                    amount = float(value)
                history = self.arrays["amount"][:signal + 1, symbol]
                valid = history[np.isfinite(history) & (history > 0)]
                if len(valid) >= 20:
                    median_amount = float(np.median(valid[-20:]))
        return {
            **trade, "board": board(trade["symbol"]),
            "signal_to_fill_return": gap,
            "gap_group": "未知" if gap is None else "低开" if gap < 0 else "0–3%" if gap <= .03 else ">3%",
            "signal_amount": amount, "median_amount_20_valid_days": median_amount,
            "liquidity_group": "未知" if median_amount is None else "<5000万" if median_amount < 5e7 else "5000万–2亿" if median_amount < 2e8 else "≥2亿",
            "mae_before_exit_day": float(np.nanmin(low) / price - 1),
            "mfe_before_exit_day": float(np.nanmax(high) / price - 1),
        }


def overlap_diagnostics(output):
    """Compare baseline signals without constructing a combined trading account."""
    baselines, axes = {}, None
    popcount = np.array([int(value).bit_count() for value in range(256)], dtype=np.uint8)
    for path in sorted((output / "results").glob("*__original.json")):
        result = json.loads(path.read_text())
        with np.load(path.with_suffix(".npz"), allow_pickle=False) as stored:
            current_axes = (stored["dates"].tolist(), stored["symbols"].tolist())
            if axes is not None and axes != current_axes:
                raise ValueError("信号重合诊断需要完全一致的日期和股票轴")
            axes = current_axes
            mask = np.zeros(int(np.prod(stored["shape"])), dtype=np.uint8)
            mask[stored["entries"]] = 1
            signals = np.packbits(mask)
        curve = result["equity_curve"]
        previous = float(result["config"]["initial_capital"])
        daily = {}
        for point in curve:
            daily[point["date"]] = point["value"] / previous - 1 if previous > 0 else 0.
            previous = point["value"]
        baselines[path.stem.split("__")[0]] = {
            "signals": signals, "count": result["entry_signals"], "daily": daily,
            "trades": {(t["symbol"], t["entry_date"]) for t in result["trades"]},
        }
    rows = []
    ids = sorted(baselines)
    for i, left in enumerate(ids):
        a = baselines[left]
        for right in ids[i + 1:]:
            b = baselines[right]
            intersection = int(popcount[np.bitwise_and(a["signals"], b["signals"])].sum())
            union = a["count"] + b["count"] - intersection
            trade_union = len(a["trades"] | b["trades"])
            dates = sorted(a["daily"].keys() & b["daily"].keys())
            x, y = (np.array([record["daily"][day] for day in dates]) for record in (a, b))
            correlation = float(np.corrcoef(x, y)[0, 1]) if len(dates) > 1 and x.std() > 0 and y.std() > 0 else None
            rows.append({
                "left": left, "right": right, "signal_intersection": intersection,
                "signal_jaccard": intersection / union if union else None,
                "left_signals_in_right": intersection / a["count"] if a["count"] else None,
                "right_signals_in_left": intersection / b["count"] if b["count"] else None,
                "closed_trade_entry_jaccard": len(a["trades"] & b["trades"]) / trade_union if trade_union else None,
                "daily_return_correlation": correlation, "common_return_dates": len(dates),
            })
    return rows


def summarize(output, report):
    identity = json.loads((output / "identity.json").read_text())
    completed = json.loads((output / "completed.json").read_text())
    if completed["identity_sha256"] != identity["sha256"]:
        raise ValueError("批次未完成或身份不符")
    prices = Prices(output)
    records = {}
    for path in sorted((output / "results").glob("*.json")):
        result = json.loads(path.read_text())
        if result["identity_sha256"] != identity["sha256"]:
            raise ValueError(f"结果身份不符: {path.name}")
        trades = [prices.enrich(trade) for trade in result["trades"]]
        config, stats = result["config"], result["stats"]
        audits = [check_trade_cash(trade, config) for trade in trades]
        costs = sum(
            trade["entry_value"] - trade["entry_price"] * trade["shares"]
            + trade["exit_price"] * trade["shares"] - trade["exit_value"]
            for trade in trades
        )
        annual = {}
        for point in result["equity_curve"]:
            year = point["date"][:4]
            prior = annual.get(year, {}).get("end_equity")
            if prior is None:
                prior_years = sorted(annual)
                start = annual[prior_years[-1]]["end_equity"] if prior_years else config["initial_capital"]
                annual[year] = {"start_equity": start}
            annual[year]["end_equity"] = point["value"]
        for values in annual.values():
            values["return"] = values["end_equity"] / values["start_equity"] - 1
        pnl = sum(t["pnl_amount"] for t in trades)
        reconciliation = None
        if "final_equity" in stats and "unrealized_pnl_amount" in stats:
            reconciliation = stats["final_equity"] - config["initial_capital"] - pnl - stats["unrealized_pnl_amount"]
        top_months = set(result["diagnostics"]["top_five_months"])
        groups = {
            group: {label: metrics([t for t in trades if t[group] == label])
                    for label in sorted({t[group] for t in trades})}
            for group in ("board", "gap_group", "liquidity_group")
        }
        pnl_order = sorted(trades, key=lambda t: t["pnl_amount"])
        examples = (pnl_order[:2] + pnl_order[-2:] + trades[-1:]) if trades else []
        record = {
            "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "metrics": result["metrics"], "annual_account_returns": annual,
            "groups": groups,
            "excluding_top_five_months": metrics([t for t in trades if t["entry_date"][:7] not in top_months]),
            "mae_median": float(np.median([t["mae_before_exit_day"] for t in trades])) if trades else None,
            "mfe_median": float(np.median([t["mfe_before_exit_day"] for t in trades])) if trades else None,
            "costs_on_closed_trades_including_slippage": round(costs, 2),
            "account_reconciliation_error": round(reconciliation, 6) if reconciliation is not None else None,
            "account_reconciles": abs(reconciliation) <= .02 + len(trades) * .011 if reconciliation is not None else None,
            "trade_cash_checks": len(audits),
            "trade_cash_failures": [a for a in audits if not a["within_rounding"]],
            "execution": stats.get("execution"),
            "examples": [check_trade_cash(trade, config) for trade in examples],
        }
        records[path.stem] = record
        print(f"diagnosed {path.stem}: cash failures={len(record['trade_cash_failures'])}", flush=True)
    write(report / "additional-diagnostics.json", {
        "identity_sha256": identity["sha256"],
        "diagnostic_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitations": [
            "退出日最高/最低价可能发生在卖出之后；MAE/MFE只含之前完整持仓日及成交端点，不是完整分钟级极值",
            "板块、截至信号日20个有效日成交额中位数及信号到成交缺口仅作事后归因，不根据收益选择股票池；成交额有效日不足20日记未知",
            "年度账户收益为连续账户分段，跨年持仓保留；不是每年独立清仓回测",
            "费用来自已平仓现金差额，包含模拟滑点；不是实际券商收费",
            "算术核对不证明日线假设下的订单在真实市场可成交",
        ],
        "results": records,
    })
    write(report / "baseline-overlap.json", {
        "identity_sha256": identity["sha256"],
        "diagnostic_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "interpretation": "22个原版的同股同信号日、已平仓同股同买入日和独立账户日收益比较；不是共同资金账户，不据此反向优化权重",
        "pairs": overlap_diagnostics(output),
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    summarize(args.output, args.report)
