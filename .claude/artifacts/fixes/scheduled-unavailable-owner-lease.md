# Bug：不可用账户未释放调度租约

> Status: FIXED
> Mode: default
> Severity: functional
> Last updated: 2026-11-02

## Symptom与Expected

本地08:00池未生成，6个停用账户旧到期任务占住单并发调度名额；
预期拒绝不可用账户工作，同时释放当前名额，允许有效账户领取任务。

## Reproduction

真实隔离PostgreSQL中先排不可用账户旧任务，再排有效账户当前任务。
`backend/tests/test_full_schedules.py`新增
`test_unavailable_owner_releases_schedule_slot_for_next_active_tenant`，
分别覆盖active=false及account_state=suspended。
旧代码两用例连续3次失败，断言失败位置为租约未释放。

## Hypotheses与diagnosis

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| 账户授权抛错跳过租约清理 | confirmed | process首次account调用位于原try/finally外；SQL用例留下lease_until |
| 配置/目录或池删除栅栏阻止生成 | eliminated | capture_identities与真实Compute成功；实际08:00补生成25池无错误 |

## Root cause与Fix

`TenantSchedules.process`在进入任务清理区前调用`FullWorker.account`，
账号拒绝是预期行为，但调度完成未执行。先计算下一到期时间，再捕获该调用的
AccessError并调用有owner/revision栅栏的Scheduler.finish，随后重抛。
只修改当前租约，不更改账户、权限、他人租约或成功批次。

## Verification

- V-1：修复后2用例通过；有效账户立即获得下一个名额。
- V-2：暂时移除修复后2用例再次失败，恢复修复后包含全部受影响测试59项通过。
- V-3：Ruff与git diff --check通过；两端匹配产物、双库恢复、真实池/API及登录核验。
- V-4：本地实际6个不可用账户活租约为0，有效账户未逾期，下次检查正常。

## Pattern analysis

搜索multitenancy worker中的runtime.account与scheduler.finish，检查Scheduled、
Notifications及FullWorker处理链；Notifications账户读取已在try/finally内，
FullWorker失败/完成也有清理路径。未扩大修改其他处理链。

## 部署与边界

本地/线上v0.4.42生效，单运行文件补丁，配置及版本保持；
实际08:00与临时09:00探针明确区分，未新跑收益回测。
完整验证及当前运行身份见[运行签收](../../../reports/daily-stock-pool-runtime-20261102.md)。
