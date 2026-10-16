# 第四批（诊断与体验）验证记录

- 基线提交：`ddb5f799`（分支 `feat/phase1-full-multitenancy`）
- 范围：模拟盘诊断与展示的三项验收——估值口径标记、回合明细口径、未执行原因留痕
- 状态：本地改动，尚未提交 / 推送 / 部署

## 实现要点

### 1. 估值价源与口径标记
- `paper.overview()` 为每个持仓标注 `price_source`（`live` / `daily_close` / `cost`），
  并返回 `valuation` 块：整体 `basis`、`as_of`、`quote_ts`、`covered/total`、`missing`。
  任一持仓缺当前行情退回成本时整体口径保守降级为 `cost`。
- API `overview` 非实时路径用各持仓最近日线收盘估值（`basis=daily_close`），
  实时路径用 enriched `raw_close` 并带快照 `quote_ts`（毫秒→秒）。
- 前端 `ValuationBadge` 标注估算口径、数据时间与覆盖；净值曲线仍标「按交易日收盘定版」，二者不混用。

### 2. 回合明细口径
- `round_trips()` 产出配对片段（`kind="pairing"`，`cycle_seq` + `cost_basis`）。
- 新增 `position_cycles()`：按（证券, 周期）聚合为完整持仓周期，收益率资金加权。
- `stats()` 保留历史片段字段，新增 `cycles` 块（周期数 / 周期胜率 / 资金加权收益率）。
- 新增 `GET /api/paper/rounds` 并列返回 pairings 与 cycles。

### 3. 未执行原因留痕
- `paper_auto` 对入场跳过分支（active_exit / cooldown / max_positions /
  missing_entry_policy / no_reference_price / zero_qty / duplicate / order_rejected）
  写入按账户有界列表，带人类可读文案与明细；`load_auto_skips()` + `GET /api/paper/auto_skips`。
- 前端账户面板新增「未执行原因」列表。

## 测试与检查

| 项 | 命令 | 结果 |
|---|---|---|
| 模拟盘后端（含 PG） | `pytest test_paper_trading/api/auto/arena/risk_policy/settle/t1/full_paper/full_runtime/strategy_monitor_events` | 172 passed |
| 后端 ruff（改动文件） | `ruff check paper.py paper_auto.py api/paper.py contracts.py test_paper_trading.py` | All checks passed |
| 前端类型检查 | `tsc -b` | 通过 |
| 前端构建 | `pnpm build` | 通过 |
| 前端测试 | `pnpm vitest run src/pages` | 14 passed（含 Paper.test.tsx） |

PostgreSQL 用一次性容器 `tsp-phase1-test-pg`（127.0.0.1:55439）承载 `TSP_TEST_POSTGRES_DSN`。
新增用例：估值价源标记与口径降级、配对/持仓周期区分与资金加权、自动跟单冷却/名额/缺快照跳过留痕。
