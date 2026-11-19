# 金融不变量与回归入口

本表用于定位修改必须守住的业务契约。列出的测试是现有证据入口，
不表示本轮已执行全部测试，也不替代真实交易窗口验收。

| 不变量 | 回归入口 | 修改时重点核对 |
| --- | --- | --- |
| 比例、百分比与金额单位分别表达 | [自定义比例单位](../../backend/tests/test_custom_pct_units.py)、[金额结果](../../backend/tests/test_monetary_results.py) | Provider 到存储、策略、UI 的换算只发生一次 |
| 信号时间与成交时间分开 | [策略交易时钟](../../backend/tests/test_strategy_trading_clock.py)、[S01 入场时间](../../backend/tests/test_s01_entry_timing.py) | 不使用未来分钟；任务迟到不改写历史信号 |
| 价格缺失不能变为零价格 | [报价可空列](../../backend/tests/test_quote_extra_null_columns.py)、[分钟输入重试](../../backend/tests/test_monitor_snapshot_freshness.py) | 缺价显式拒绝或等待；未知值不进入金融计算 |
| 限价与历史证券状态遵循当日规则 | [涨跌停价](../../backend/tests/test_price_limits.py)、[历史证券状态](../../backend/tests/test_instrument_history.py)、[ST 规则](../../backend/tests/test_st_limit_and_sharpe.py) | 不以当前证券状态回填全部历史 |
| T+1 可卖数量与批次守恒 | [下单时可卖量](../../backend/tests/test_paper_t1_available_at_order.py)、[持仓批次](../../backend/tests/test_holding_batches.py) | 当日买入不能绕过可卖时点；部分成交后余额一致 |
| 公司行为保持现金与股份权益一致 | [公司行为](../../backend/tests/test_paper_corporate_actions.py) | 除权、分红、份额变化不能重复入账 |
| 买入预算覆盖费用和容量 | [模拟交易](../../backend/tests/test_paper_trading.py)、[策略组合仲裁](../../backend/tests/test_strategy_portfolio_arbitration.py) | 同时检查现金、持仓上限、整手与实际成交容量 |
| 派发新鲜度与提交入场截止分阶段守卫 | [派发快照新鲜度](../../backend/tests/test_monitor_snapshot_freshness.py)、[入场过期](../../backend/tests/test_full_entry_expiry.py) | 派发时固定的有效输入不因沙箱启动而误判；不放宽实际提交守卫 |
| 未执行、缺输入、成功零命中分别表示 | [S02 零命中](../../backend/tests/test_s02_zero_completion.py)、[策略日视图](../../backend/tests/test_strategy_day_views.py) | 空成员列表不自动等于失败或缺归档 |
| 旧任务不能覆盖新身份、新版本、新日期 | [历史级联](../../backend/tests/test_strategy_history_cascade.py)、[多租户监控](../../backend/tests/test_full_monitor.py) | 固定输入、授权版本、租约与迟到提交保护保持 |
| 完整池与数值筛选、显示条数分开 | [每日池](../../backend/tests/test_daily_stock_pools.py)、[策略独立池](../../backend/tests/test_strategy_owned_daily_pools.py) | 完整成员分页读取；已保存空池不回退全市场；拒绝错日期／身份 |
| 买入池为空仍可独立处理持仓卖出 | [策略独立池](../../backend/tests/test_strategy_owned_daily_pools.py) | `test_holding_exit_survives_empty_entry_pool` 固定卖出不被空入场池吞掉 |
| 分片费用不重复征收 | [隔夜订单费用](../../backend/tests/backtest/test_overnight_fees.py) | 同一订单分片的固定过户费用只计一次 |
| 取消必须穿透分片，失效任务不能释放新任务槽 | [任务取消](../../backend/tests/test_job_stall_and_cancel.py) | 正常进度维持心跳；取消异常不被单股隔离捕获；防止僵尸释放 |
| 数据源冲突不能经去重悄然丢失 | [分钟结果守卫](../../backend/tests/test_minute_result_guards.py) | 正常重复可归并；冲突量价保留证据；缺参考不能覆盖完整窗口 |

下列成对入口便于审查正常／拒绝路径，不用宽泛文件名代替具体契约：

| 契约 | 正常路径 | 拒绝／边界路径 |
| --- | --- | --- |
| 完整池读取 | `test_all_members_are_read_and_bound_to_local_execution_snapshot` | `test_wrong_snapshot_identity_date_or_count_is_rejected` |
| 零命中状态 | `test_completed_zero_is_materialized_and_confirms_tomorrow_empty_sell` | `test_invalid_or_nonempty_receipt_does_not_convert_unknown_to_zero` |
| 派发快照 | `test_fresh_dispatched_quotes_survive_sandbox_startup` | `test_legacy_payload_does_not_accept_stale_quotes` |
| 实时时钟 | `test_already_confirmed_buy_remains_success_after_close` | `test_live_cutoff_is_inclusive_at_exact_second` |
| 任务进度 | `test_progressing_job_is_not_reaped` | `test_cancelled_error_survives_chunk_isolation` |
| 并发容量 | `test_run_with_capacity_runs_when_slot_free` | `test_run_with_capacity_fails_readably_when_slot_hogged` |
| 分钟数据 | `test_cent_difference_keeps_exact_reference_without_material_conflict` | `test_volume_has_no_cent_tolerance_and_conflict_evidence_survives` |

依赖 PostgreSQL 的用例必须运行在隔离测试库。源码提取或纯性能优化应先运行受影响行，
再按实际变更扩大回归；不能以修改测试预期的方式掩盖金融语义变化。
