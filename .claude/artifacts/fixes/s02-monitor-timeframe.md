# Bug: s02 开启策略监控被错误按日线校验

> Status: FIXED
> Mode: default
> Severity: functional
> Last updated: 2026-11-01

## Symptom and expected

用户报告`strategy s02 does not support timeframe '1d'; supported=['1m']`。
监控规则保存应按所选策略声明周期校验，s02为1m，日线策略仍为1d；
保留资产类型及目标列表校验。

## Reproduction

真实`POST /api/monitor-rules`提交s02规则，返回400和同一错误。
新增`backend/tests/test_strategy_monitor_timeframe.py`用真实内置策略和真实校验器，
覆盖创建、编辑、停用、重新启用、日线兼容、缺省元数据及不支持的资产类型。
原代码连续三次1失败/3通过；临时撤去修复后再次1失败/3通过。

## Hypotheses and diagnosis

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| H1：监控保存构造固定1d上下文 | confirmed，置信高，优先1 | API实际返回及调用栈指向save_rule的StrategyDataContext |
| H2：s02元数据或单次运行周期错误 | eliminated，置信中，优先2 | 内置声明仅1m，单次运行已从策略声明解析；失败入口实际为监控保存 |

## Root cause and fix

`save_rule`在调用引擎校验前为所有策略构造固定1d上下文；
`validate_context`正确拒绝只支持1m的s02。
只修改`backend/app/api/monitor_rules.py`一行，采用声明的首周期，
未声明周期时仍沿用1d。没有改变交易阈值、回测、执行模型、数据库schema或前端。

## Verification

- V-1：修复后新回归与原监控目标解析6项通过。
- V-2：隔离副本撤去该行修复后重新失败，恢复后70项受影响范围回归通过。
- V-3：扩大回归中的2个旧分组mock在未修改源码同样失败，
  原因是SimpleNamespace缺request.state；最终专项命令显式排除这2项，不计为通过。
- V-4：监控API既有13项Ruff提示与改前逐项一致，新增0项，新测试Ruff通过。
- V-5：工作区与本地API已同步并完整重启，真实监控保存200且SQL可读取；
  92份s02每日状态与全部原研究任务输入、结果逐项哈希不变。
- V-6：真实浏览器1440×1000、390×844，s02两次开启/取消，
  日线策略开启/取消均成功，无页面运行异常；测试使用隔离账号并清理规则。
  浏览器连接工具检索没有可用Browser MCP，采用已有Playwright。

## Regression test

`test_create_edit_disable_and_reenable_use_the_declared_cycle`覆盖分钟及日线；
`test_monitor_asset_validation_is_not_bypassed`保留拒绝错误资产；
`test_undeclared_cycle_keeps_the_daily_default`保留旧元数据行为。

## Pattern analysis

检索单次运行、批量执行、监控保存和实时评估的timeframe使用：
单次运行已按元数据解析；策略页卡片及日/分钟批量执行传入各自周期。
实时分钟评估经minute_filter的monitor_context单独构造分钟上下文，
日线矩阵上下文保持1d。当前错误首次引入点是监控保存，不扩展修改其他入口。

## Deployment boundary

应用仍为0.4.39，未提交、推送、部署线上或运行新收益回测。
Compute执行代码、s02选股/买卖/仓位规则和原结果不变。
本次不认证全量测试通过，证据见`reports/s02-monitor-timeframe-20261101.json`。
