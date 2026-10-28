# Bug: 单证券缺数丢弃健康证券并使整个K线任务失败

> Status: FIXED
> Mode: default
> Severity: functional
> Last updated: 2026-10-29

## Symptom

301110.SZ来源变化导致分钟任务失败；其他已取证券也不提交。
用户明确要求失败证券跳过、保留其他证券并记录未补齐的数据。

## Expected

逐证券失败不得停止其余证券；成功响应但仍有缺口的任务完成并携带警告和持久清单。
取消、写入失败、批次异常保持明确异常。不得伪造历史OHLC/成交额。

## Reproduction

tests/test_kline_symbol_isolation.py 中三只失败后第四只健康、单/双工作线程及持久清单用例，
原实现连续三次各5失败。反向恢复原失败汇总后5失败/4通过。

## Hypotheses & diagnosis

| 假设 | 判定 | 证据 |
| --- | --- | --- |
| Provider将单证券异常汇总为批次异常，丢弃健康帧 | confirmed | _iter_daily_serial/_minute_partition尾部raise；三次失败提前break |
| 只修分页来源恢复就能取得三四月完整分钟K | eliminated | category7/8没有旧区间；历史分时/逐笔虽非空但缺字段或与原始K不符 |

## Root cause

分页来源隔离是必要保护；问题在上层将其失败扩散到整个证券分区，并在三次失败后
放弃后续证券。sync_missing另把所有真实remaining无条件变为error，即使其他证券已保存。

## Fix

保留单证券分页来源保护，去除整批失败汇总和连续三只的提前退出。
成功取数后的缺口生成warning、skipped_symbols和运行目录CSV；
全量1分K按持久键核对，API消息显示警告。原子写、缓存和任务取消链保持。

## Verification

- V-1: 隔离/Provider/预算/修正/周期定向188 passed。
- V-2: 原失败汇总反向验证5 failed / 4 passed。
- V-3: 修改模块Ruff通过；API42项历史诊断，本次新增0项。
- V-4: 主工作区整合337 passed，固定北京业务日/时间；原生/取消/分区/缓存另72 passed。
- V-5: ECS定向补采0写入/5520剩余，逐键清单核对5520唯一键；常驻发布仍待执行。

## Regression test

tests/test_kline_symbol_isolation.py覆盖失败证券后健康证券、单/双并发、
初始化失败、遗漏/全量持久报告及报告写入失败。
已有部分响应任务测试改为succeeded并验证written/remaining/CSV，不追溯修改旧任务状态。

## Pattern analysis

git grep定位failed_symbols及“取数不完整”：当前修复涉及日K和分钟两个同类汇总。
证券目录的完整性拒绝发布属于不同契约，保留。远程周K不用于本地派生周K入口。

## Open questions / Follow-ups

完整3/4月OHLC/成交额仍需要能认证该区间的原始分钟数据。
tests/test_kline_periods_api.py有固定2026-10-29为未来日的既有假设；整合验证须固定业务日期。
发布受主线质量门与显式提交授权约束。详见reports/minute-301110-20261029/skip-and-backfill.md。
