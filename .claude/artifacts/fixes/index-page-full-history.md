# Bug: 指数页默认历史范围截断本地六年数据

> Status: FIXED
> Mode: default
> Severity: functional
> Author: user
> Last updated: 2026-10-29

## Symptom

指数页日K最多约 122 个点、周K约 156 个点，月K约 72 个点；本地实际已有约六年数据。

## Expected

首次进入指数页及切换日/周/月周期时，默认展示当前本地数据集的全部可用历史；用户手动选择日期后再按显式范围查询。

## Reproduction

- 前端：`frontend/src/pages/Indices.test.tsx`
- 后端：`backend/tests/test_index_kline_periods.py`
- 复现稳定性：前后端均 3/3 reliably fails。
- 本地数据证据：上证指数日K 1452、周K 307、月K 72；最早日期为 2020-11。

## Hypotheses & diagnosis

| # | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| H1 | 页面请求前写死时间窗导致截断 | confirmed | `defaultRange` 分别固定 6/36/120 个月，数量与 122/156/72 完全吻合 |
| H2 | 本地指数历史本身不完整 | eliminated | parquet 实测日K 1452、周K 307、月K 72 |
| H3 | 图表缩放只允许约 122/156 个点 | eliminated | `sourceRows` 来自 API 完整响应，`visibleBars` 只控制初始视窗；请求响应已被日期参数截断 |

## Root cause

提交 `27e18417` 为指数周期页加入了固定默认范围：日K 6 个月、周K 36 个月、月K 120 个月。前端总是显式发送这些日期；同时后端在缺省开始日期时也使用同样的固定窗口，因此两层都会截断本地已有历史。

## Fix

- `backend/app/services/index_kline.py`：从实际 parquet 分区计算周期可用范围。
- `backend/app/api/indices.py`：未指定开始日期时从本地最早分区读取，并返回实际首条日期。
- `frontend/src/lib/api.ts`：开始日期为空时不发送 `start_date`。
- `frontend/src/pages/Indices.tsx`：首次和切换周期先请求全量，再用响应中的真实最早日期回填日期框。

## Verification

- V-1：新增回归测试修复前连续 3 次 RED。
- V-2：修复后后端 3 项、前端 6 项 GREEN。
- V-3：临时反向移除生产修复，后端与前端测试重新 RED；恢复后再次 GREEN。
- V-4：指数后端相关测试 42 passed。
- V-5：前端全量 85 files / 455 tests passed，生产构建和 lint 通过。
- V-6：Ruff 增量新增 0，`git diff --check` 与 Python compile 通过。
- V-7：真实本地数据链路返回日K 1452、周K 307、月K 72。

## Regression test

- `test_period_api_without_start_returns_all_local_history`
- `requests all locally available history before applying an explicit range`

## Pattern analysis

| 搜索方式 | 命中数 | 是否本次同类隐患 |
|---|---:|---|
| 前端 `setMonth(... -N)` / `defaultRange` | 9 | 否；其余为股票预览或回测显式默认窗口，不宣称展示全量本地历史 |
| 后端固定 180/1095/3650 天 | 1 | 已修；仅保留在本地无任何分区时的兼容兜底 |
