# 失效TCP的跨平台请求计数回归

> Status: FIXED
> Mode: default
> Severity: 发布质量门阻断
> Last updated: 2026-10-30

## Symptom / Expected

PR #35 CI `40921979434`后端为4,835通过、9跳过、1失败，
唯一失败在`test_rustdx_stale_source.py`的pinned参数：
返回SourceChanged，但请求尝试计数为1而不是测试规定的0。
需要保证失效会话重建后拒绝旧来源的任何页，计数与实际尝试一致。

## Reproduction

在原有全关闭场景外，增加`SHUT_WR`模拟peer_addr仍可读取、
发送端已经关闭的连接。使用候选桥接0.3.0/API4，
同一用例连续三次均因`assert (1 == 0)`失败，其他三种组合通过。
不会用单次macOS全关闭通过覆盖Linux CI的真实失败。

## Hypotheses & diagnosis

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| 原生桥接提交了替换来源的数据 | 排除 | CI和发送端关闭复现均返回SourceChanged，rows为空 |
| 测试假定操作系统能在首次写前发现TCP关闭 | 根因确认 | Linux全关闭仍保留peer_addr；macOS发送端关闭复现同一计数1 |

`Tcp::is_reusable`检查缓存状态与peer_addr；对端或发送端关闭未必令这些
状态立即失败。`bars_json`把发起传输的失败尝试计入requests，
重连前后来源检查仍拒绝旧页。计数1是正确业务尝试，不是接受了新来源页。

## Fix / Regression test

仅调整`backend/tests/test_rustdx_stale_source.py`：
同时覆盖SHUT_RDWR/SHUT_WR及pinned/unpinned四组合。
对pinned必须返回SourceChanged、空rows、无source，
最多一次失败尝试并与全局minute_requests增量完全相等；
从头重新请求才能返回新的connection_id，活跃连接仍为1。
核心、桥接生产代码与固定上游身份均保持。

## Verification

- V-1：新增场景在原计数断言下连续三次失败。
- V-2：修正断言后，失效来源与原生池模块共18项通过，28.01秒。
- V-3：独立临时副本恢复原零请求断言，同一场景再次失败。
- V-4：Ruff、git diff --check和107文件上游身份检查通过。
- V-5：完整rustdx Python回归225项通过，59.88秒；工程/文档/版本检查通过。
- 远程CI结果继续记入发布验证，不预称远程通过。

## Pattern analysis

`git grep -n 'requests.*== 0' -- backend/tests/test_rustdx_*.py`共8处。
其余为无缺口零请求、已知配置/组变化在调用前拒绝、无等待者，
没有依赖未知TCP故障被提前发现的相同断言。原生池显式来源替换的零请求测试保留。
