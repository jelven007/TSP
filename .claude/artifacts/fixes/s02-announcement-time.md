# Bug: 当日停牌公告在分钟信号时刻不可消费

> Status: FIXED
> Mode: default
> Severity: functional
> Last updated: 2026-11-02

## Symptom / Expected

已于09:29公开的当日停牌事件在14:30完整池内仍被标为未知，导致尾盘广度缺价。
只允许消费观察时点之前已公开且已生效的事件；08:00、09:00批次和分钟信号各有自己的截止时点。
旧文件缺精确时点时继续保守延后，缺行情不推断停牌。

## Reproduction

`backend/tests/test_instrument_suspension.py` 的
`test_replay_uses_signal_time_not_end_of_day_for_suspension` 连续三次在原实现失败：
09:00公告在14:30回放仍报1/2缺价；15:00公告不可提前排除的反例保持成立。

## Hypotheses & diagnosis

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| 原数据没有真实停牌证据 | 排除 | 公开停牌记录和带时间的报道确认生效日及公开时点 |
| 日期过滤丢失当日可用事件 | 根因确认 | `dated_status` 仅使用 `announced_on < day`，Loader丢弃可选时间列 |

## Fix

独立事件文件兼容可选北京时间 `announced_at`，Loader验证其公告日并统一时区。
观察时点传入Screener、分钟回放与盘前批次；尾盘完整池使用最早有效尾盘信号时刻，
不能使用日終15:00公告改变14:30分母。公共缓存和研究指纹仍使用原文件身份，无SQL迁移。

## Verification

- 原实现：新回放反例三次失败，日期时间接口和旧Schema兼容反例失败。
- 修复工作目录：174项通过，11项PostgreSQL环境相关测试跳过。
- 实际线上候选：隔离导入150项通过；确认模块路径来自候选目录。
- 反向验证：显式导入上一线上实现，新回放反例再次失败。
- 改动文件Ruff通过；Screener原有Ruff告警未顺带清理；`git diff --check`通过。
- 匹配Runtime/Compute镜像合成收益、分片费用、截止余股及公告时点检查通过。
- 线上七角色健康且镜像匹配；控制/业务两库恢复比对，schema7保持。
- 固定公共输入的84日完整池审计：无跳日、无错误、14:30缺价/昨收为零。

## Pattern analysis

搜索 `pool_evidence|dated_status|build_daily_pool_context` 覆盖分钟回放、Screener、
本地盘前生成及Full Compute批次。四条消费路径均按当前观察时点传递。
没有改变未知行情、持仓清仓期限或自动交易规则。

## Deployment / research boundary

线上0.4.42派生快照已匹配部署；本地分支继承main0.4.43，不人工改号、提交或推送。
首次重启发现上轮root生成的公共停牌文件和数据发布清单无法由普通用户创建硬链接；
只修正受影响文件/目录属主，逐文件哈希保持。服务恢复后显式重启并核验运行镜像。
真实回测另遇买入后次日停牌，强制早盘清仓无法执行；这属于现有交易约束，
完整收益不得由失败前曲线补造。实际场景和统一Excel另见研究报告。
