# Bug: 301110.SZ 分页来源变化导致取数不完整

> Status: FIXED（源码；历史覆盖受源限制，常驻服务尚待升级）
> Mode: default
> Severity: functional
> Last updated: 2026-10-29

## Symptom / Expected

ECS 任务 `528dd8d387` 获取三四月1分K时，在来源变化后终止301110.SZ取数。
预期来源瞬时变化时丢弃全部旧页并完整重采；持续变化仍明确失败，不提交混合结果。

## Reproduction

`backend/tests/test_rustdx_source_restart.py` 用确定性来源变化模拟旧连接失效。
原版本连续三次均6失败/1通过；最终9项回归在进程内移除重采后8失败/1通过。

## Hypotheses & diagnosis

| 假设 | 判断 | 证据 |
| --- | --- | --- |
| 分页身份变化信号未被Provider恢复编排接住 | confirmed，程序根因 | 原生明确报SourceChanged；旧分页直接传播；重建全部局部状态后回归通过 |
| 上游没有所请求的旧分钟历史 | confirmed，独立覆盖限制 | 两个可用节点真实最早为6月3/4日，三四月0行；大偏移空页 |

## Root cause / Fix

桥接在请求前发现来源身份变化并拒绝旧分页，这是正确保护。
Provider原来没有实现从头重采信号，异常最终被包装为分钟取数不完整。
共享`_bar_records`新增一次特定信号重采，原实现为`_bar_records_once`；
新尝试重新创建所有页/错误/来源/结果局部状态。普通失败不重试，取消不吞掉。

## Verification

- V-1：最终回归9 passed；进程内反向验证8 failed/1 passed。
- V-2：固定业务日邻接303 passed；原生/安装/行情预算39 passed。
- V-3：主工作区定向118 passed，最终受影响模块342 passed（固定业务日）；
  Ruff、make check及diff空白检查通过。
- V-4：ECS真实原生读取注入一次来源变化，3,600行与无中断读取完全相等。
- V-5：源端23,760分钟键与本地逐一对账，无源端可供而本地缺失的键。

上述计数范围不同，不能相加为全仓通过。
真实当天运行有一条主线既有未来日期测试失败，已在报告说明。

## Regression test / Pattern analysis

测试路径：`backend/tests/test_rustdx_source_restart.py`。
搜索`RustdxSourceChangedError/SourceChanged/bars_with_source`：
Python集中在客户端和Provider，原生检查位于`bars_json`。
共用恢复入口覆盖日K/分钟/远程周K，不移除客户端或原生来源检查。

## Open questions / Follow-ups

三四月仍缺5,520个键，需要有该范围分钟数据的源或历史文件。
线上遗漏任务仍运行；常驻部署未执行，不能把工具进程验证写成线上已升级。
一条已有API测试把2026-10-29写死为未来日，需另行固定其测试时钟。
详见[完整记录](../../../reports/minute-301110-20261029/verification.md)。
