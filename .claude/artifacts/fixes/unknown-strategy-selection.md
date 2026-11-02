# Bug: 线上回测页重复请求已删除策略

> Status: FIXED
> Mode: default
> Severity: functional
> Last updated: 2026-11-02

## Symptom

用户在“线上环境”报告`unknown strategy: custom_mvxznhd9`。

## Expected

打开回测页先确认当前目录；已删除的历史选择及策略专属状态清理，
保留通用输入，刷新不再请求失效详情。目录失败不推断删除。

## Reproduction

隔离浏览器保存旧ID后打开线上回测页，捕获一次失效详情请求与对应错误提示，
零计算/委托请求。相邻回归在原实现连续三次失败；
最新v0.4.42仍能真实复现，旧v0.4.41验证不冒充新基线验收。

## Hypotheses & diagnosis

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| 目录/策略注册失败 | 排除 | 受控目录无加载错误，目标定义已删除，详情404符合删除状态 |
| 浏览器旧选择在目录确认前请求详情，且只清内存 | 根因 | 旧`enabled: !!selectedStrategy`、空目录提前返回、未写回storage；真实刷新及回归复现 |

## Root cause

`strategyBacktestLast`引入已失效选择，详情查询没有当前目录成员门槛。
内存清理晚于请求且未同步持久化，刷新重新引入旧值；成功空目录又被误判为未就绪。

## Fix

只改`StrategyBacktest.tsx`及相邻测试。目录挂载强制刷新，成功且停止获取后确认成员；
详情/运行使用同一门槛。清理失效选择及参数/覆盖/结果/签名，同时保留通用输入。
请求错误保留旧选择，不重建定义、不改后端协议或交易规则。

## Verification

- V-1：v0.4.42候选相邻7项通过。
- V-2：只反转本次补丁后回归失败，恢复后7项通过。
- V-3：新基线全量前端102文件/537项、ESLint及生产构建通过。
- V-4：候选runtime/Compute清单一致，后端及非静态产物保持。
- V-5：线上双库恢复核验、七角色健康、受控配置、登录、四API绑定及Worker心跳通过。
- V-6：实际线上六场景通过，失效详情0、有效详情200，空目录与503分别符合预期，
  页面异常及计算/委托POST为0；公开HTML/JS/CSS与候选哈希一致。
- V-7：主线同步后的工作区源码与测试逐字节匹配候选，7项回归、类型及隔离构建通过。

## Regression test

`frontend/src/pages/backtest/StrategyBacktest.test.tsx`：
`validates a saved deleted strategy before requesting detail`（含成功空目录与旧缓存）、
`retains the saved strategy when the catalog request fails without requesting its detail`、
`loads a valid saved strategy only after the catalog confirms it`。

## Pattern analysis

`git grep -n -E 'strategyGet\(|strategyBacktestLast|screenerStrategies\('`
在页面及策略组件中命中41行（包含本次测试）。
`strategyBacktestLast`生产读写仅在本页；Paper两处只取目录建议，
StrategySettings/结果组件由显式操作或有效结果标识读取详情，
未确认存在同样的“持久旧选择→自动详情”路径，不扩大本次改动。

## Open questions / Follow-ups

准备期间生产基线变化触发安全停止，已改为在v0.4.42源码上重验；
本次不覆盖共享工作区其他功能，不提交、不推送、不新跑历史收益。
共享工作区同步主线后已重新应用相同修复与回归，并与线上候选逐文件核对。
最终身份、验证与部署限制见
[报告](../../../reports/unknown-strategy-20261102.md)。
