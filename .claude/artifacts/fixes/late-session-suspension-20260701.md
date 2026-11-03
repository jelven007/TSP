# Bug: 本地尾盘走强误把已公告停牌证券计入缺价池

> Status: FIXED
> Mode: default
> Severity: functional
> Last updated: 2026-11-02

## Symptom / Expected

用户看到“2026-07-01: 尾盘走强: 股票池中 2/1567 只缺少指定分钟价格或昨收”。
已公开且已生效的停牌证据应在信号时点排除证券；没有证据的缺行情仍须明确失败。

## Reproduction / Hypotheses

本地原事件文件只有 4 条；000595.SZ、000656.SZ 昨收有效但 7 月 1 日无分钟行。
补齐事件后 7 月 1 日恢复；全 84 日继续暴露 8 月 17 日、9 月 7 日、9 月 14 日三个同日公告问题。

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| 价格文件丢失或昨收缺失 | 排除为本次根因 | 两只昨收有效；交易所事件证明当日停牌 |
| 本地停复牌事件遗漏 | 确认 | 原 4 条与已核验 63 条不同，缺少 59 条 |
| 本地旧构建未消费公告时间 | 确认 | loader 丢弃 announced_at；同日公告回归旧构建 3 项失败 |

## Root cause / Fix

数据遗漏与运行构建未同步同时存在。仅补数据能修复 7 月 1 日，不能修复同日盘前公告。
保留原事件，追加缺少的 59 条，发布新的公共快照；将整个本地运行单元切换为固定
0.4.45 主线源码、冻结依赖、前端及匹配 Compute。工作区业务代码和用户策略未改写。

## Verification

- V-1：原构建回归测试出现 3 项失败/1 项通过，捕获同日公开时间消费差异。
- V-2：安装后的公告时点及尾盘模块共 53 项通过，包含信号后公告不能提前使用。
- V-3：当前公共快照完整 84 日重放，无缺价、池错误、回放错误及跳过日期。
- V-4：实际 Compute 重算四个错误日期，池人数 1565/1623/1664/1642、入场错误为空。
- V-5：8 个进程的运行目录匹配、管理员登录、两库 schema v7、11 组状态签名及原失败任务保持。

## Regression test / Pattern analysis

复用已入主线的 `backend/tests/test_instrument_suspension.py`：
`test_replay_uses_signal_time_not_end_of_day_for_suspension`、
`test_premarket_pool_honors_each_batch_clock` 及旧格式/未知时间/错误公告日期用例。
共享 `dated_status/pool_evidence` 同时服务盘前池、分钟回放及持仓出场；
本次通过完整产物更新保留其观察时点传递，不在调用方吞掉缺价错误。

## Delivery / Boundaries

完整身份、原参数、数据区间与剩余平台提示见
[修复验收](../../../reports/local-late-session-gap-20261102.md)。
本次没有追加资金收益回测或线上部署；旧研究输入与失败结果仍不可变。
