# Bug: 周K生成无法读取含 quote_ts 的新旧日K分区

> Status: FIXED
> Mode: default
> Severity: functional
> Last updated: 2026-10-28

## Symptom

数据总览周K卡片点击“生成数据”时报Polars `extra column ... quote_ts`，
任务在读取本地日K阶段失败。

## Expected

周K生成应兼容日K分区渐进新增元数据列，继续按标准OHLCVA聚合，
不要求删除或迁移已有行情。

## Reproduction

`backend/tests/test_kline_periods.py::test_weekly_generation_tolerates_daily_quote_schema_evolution`
构造10月26/27两个日K分区，分别覆盖额外列在前和在后。
旧实现连续3次均2 failed / 35 passed，错误分别为缺少和额外`quote_ts`。

## Hypotheses & diagnosis

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| 周K聚合不接受`quote_ts` | 排除 | `aggregate_weekly`只选择标准OHLCVA |
| 日K读取绕过schema兼容入口 | 确认 | 证券枚举和范围读取均直接调用`pl.scan_parquet` |
| ECS文件损坏 | 排除 | 两分区元数据可读；共享兼容扫描成功读取并聚合 |

## Root cause

Polars 1.44默认使用首个Parquet文件的schema并对其他文件执行严格校验。
周K生成在共享`app.parquet`兼容助手建立后仍保留两处裸扫描：
旧文件在前时新文件的`quote_ts`是额外列，新文件在前时旧文件又缺少该列。

## Fix

- 基础证券枚举改用`scan_parquet_compat`。
- 本地日K范围读取改用`scan_daily_parquet`，同时获得标准日K类型转换。
- 周K聚合、写盘、视图、缓存和任务契约保持不变。

## Verification

- V-1：旧实现连续3次稳定2失败。
- V-2：修复后新回归2项通过，周K模块37项通过。
- V-3：生成API/进度39项、共享Parquet5项、周K修正2项通过。
- V-4：进程内恢复裸扫描后新测试重新2失败，再恢复修复后2通过。
- V-5：Ruff与`git diff --check`通过。
- V-6：ECS实际混合分区只读扫描10,430行、5,215证券，聚合5,215条周K。

## Pattern analysis

搜索`scan_parquet`和共享兼容helper后，周K两处是本次真实失败链路。
其他裸扫描属于稳定schema表或独立职责，不在本修复中扩展修改。

## Open questions / Follow-ups

整份`test_repair_kline.py`在干净基线worktree有2个既存数据源环境失败：
默认rustdx未加载当前并行的未提交插件改造；周K筛选用例2项通过。
