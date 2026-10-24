# 后端操作脚本

脚本复用 `backend/app/` 的既有服务、仓库与数据契约。
从 `backend` 执行 `uv run --frozen --no-sync python -m scripts.<分类>.<模块> ...`；
先按冻结锁安装依赖。旧位置转换见[迁移表](../../reports/document-consolidation-20261024/historical-evidence.md#layout-history-3-目录迁移对照)。

| 分类 | 现有模块 | 职责 |
| --- | --- | --- |
| `acceptance/` | `accept_external_sse`、`accept_full_compose`、`accept_market_pipeline`、`accept_phase2_step1`、`accept_phase2_step2`、`accept_production_recovery` | 一次性候选、市场链路、SSE、恢复验收 |
| `acceptance/` | `observe_candidate_soak`、`observe_second_acceptance`、`prepare_continuous_acceptance`、`run_active_acceptance`、`validate_online_timeout` | 持续观察、正式负载与超时验证 |
| `performance/` | `benchmark_full_quote_bridge`、`benchmark_rustdx_quotes`、`benchmark_second_archive`、`stress_online_timeout`、`stress_polars_concurrency` | 快照、归档、连接数与并发量测 |
| `operations/` | `backup_full_host`、`maintain_full_host`、`backfill_rustdx_st_history`、`cleanup_halt_days` | 双库备份、公共版本保留、ST 回填与停牌数据修正 |
| `research/` | `analyze_market_waves`、`render_market_waves_svg`、`run_governed_builtin_backtest` | 离线波段分析、绘图与固定口径研究 |
| `diagnostics/` | `probe_tickflow_pro` | Provider 能力探测 |
| `demos/` | `demo_ai_strategy_gate`、`demo_recap_push_mode` | 策略门控与复盘推送行为演示 |

各脚本的网络、数据库、写盘与 dry-run 行为沿用原契约；以具体 `--help` 和专项文档为准。
备份与维护见[运维手册](../../docs/operations.md)，性能验收见
[测试与验收](../../docs/testing.md)。运行数据与输出放在明确的隔离路径。
