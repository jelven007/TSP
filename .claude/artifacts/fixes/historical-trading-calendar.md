# Bug: 历史遗漏K线因交易日历覆盖不足而失败

> Status: FIXED（源码；未部署）
> Mode: default
> Severity: functional
> Last updated: 2026-10-26

## Symptom

用户执行历史遗漏获取后收到“选定区间缺少可靠交易日历, 无法判断遗漏数据;
请缩小范围或配置交易日历数据源”。本地任务记录确认2020-01-01和2023-01-02
开始的周K遗漏获取均在日历判断阶段失败。

## Expected

已有官方年度日历覆盖的区间可以按真实交易日检测缺口；
未覆盖年份继续要求可靠Provider证据，不用行政工作日、单股停牌或缺K线反推休市。

## Reproduction

- 起点：`period=1w, mode=missing`，开始日期分别为2020-01-01、2023-01-02。
- `_trading_days`回归扫描至2026-10-26，不访问外部行情。
- 服务回归分别只补两个历史周，断言实际行、周一标签、请求范围和重复零请求。
- 选择器：`actual_historical_request or historical_week`，四项。
- 旧实现连续三次均为4 failed；每次退出码1，失败消息与用户报错一致。

## Hypotheses & diagnosis

| 假设 | 预测观察与验证方式 | 结论 |
| --- | --- | --- |
| H1：内置覆盖不足（prior 70%，核对后H） | 历史工作日为None，备用日历起点晚于请求；读取源码并运行历史回归 | confirmed：内置仅2026，备用指数仅近370天 |
| H2：仅某个源暂时不可用（prior 30%，核对后L） | 源恢复即可支持历史请求；将源日历隔离为None，比较公布年份与历史年份 | eliminated作为唯一根因：即使有近一年源日历，也不能认证2020/2023 |

## Root cause

`repair_daily.run_repair_daily → kline_gaps.sync_missing → _trading_days`。
周K先把起始日期对齐周一；`market_time.published_trading_day`只内置2026年。
未知年份转到`data_integrity._trading_calendar`，默认无独立日历的自定义日线源
只请求上证指数近370天；范围校验因此正确拦截历史请求。
保护性报错本身无需删除，缺失的官方覆盖才是修复位置。

## Fix

- `backend/app/market_time.py`增加2019–2025年最终公告安排，保留2026原区间。
- 新日历版本`SSE-2019-2026-20261026`；应用版本保持0.4.31。
- 包含2019劳动节修订、2020春节延长和2024除夕休市。
- 公布年份零日历网络请求；周末/未知年份/现有源证据策略保持原契约。
- 不新增Provider接口、设置或数据集，不触发真实历史补采。

## Verification

- V-1：旧实现四项复现，3/3稳定失败；修复后窄模块113 passed。
- V-2：仅替换进程中的日历模块为原v0.4.31源码，保留新测试，四项重新失败；
  原因仍为同一日历错误。工作树源码与测试没有回滚。
- V-3：26个受影响测试模块549 passed、49 skipped；真实PostgreSQL环境未配置，
  依赖一次性PostgreSQL的相关用例跳过，不据此声称生产验证通过。
- V-4：新增旧日历快照兼容回归后，数据发布/研究快照模块33 passed。
- V-5：修改的Python文件Ruff通过；文档、目录、版本和diff检查见
  [最终验收记录](../../../reports/historical-calendar-20261026/verification.md)。

## Regression test

- `test_historical_trading_calendar.py`：长范围、46组节假日/复市、
  7个行政补班周末、未知年份及已有Provider证据。
- `test_repair_kline.py`：历史周K实际补入、跨2019/2020周一标签、重复幂等。
- `test_data_release.py`：研究日历版本和覆盖年份。
- `test_research_snapshot.py`：升级拒绝混用旧日历，旧快照保留，新输入具有新身份。

## Pattern analysis

实际搜索`git grep -n published_trading_day -- backend/app`命中23行；
消费者为完整市场新鲜度、健康、完整性、遗漏检测、实时交易日、板块分钟和模拟交易。
本轮统一日历修复随现有函数接入上述消费者，相应测试按调用链回归。
搜索`_trading_calendar(`在生产代码命中定义及两个调用：
`data_integrity._candidate_days`保留旧工作日近似，`kline_gaps._trading_days`严格拒绝未知；
两者职责不同，没有把完整性扫描的近似当作遗漏补齐证据。

## Open questions / Follow-ups

- 2018及更早、2027及后续未公布年份仍须可靠源日历；十年历史能力不因本修复而完成。
- 原运行进程仍使用已发布v0.4.31，需正式集成/构建/升级后原任务才使用新日历。
- 旧研究快照必须按其原日历与构建回放；新研究重新捕获输入，不改写旧快照。
- 数据源是否返回全部历史周期/证券，以及实际剩余缺口，须真实补采后另行验收。
