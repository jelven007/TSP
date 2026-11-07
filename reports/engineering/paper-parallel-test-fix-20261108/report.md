> 脱敏历史正文副本；执行日期、版本和测试计数沿原记录。原始附件及用户文件保持，本次统一发布的验证与部署另行签收。

# 4项并行改动测试修复核验

日期：2026-11-08。结论：原4项测试全部通过，相关扩大回归356项全部通过。
用户授权范围为查看并修复此前4项并行改动测试；本次未提交、推送、加版本或部署。

## 范围与原因

接手时重新执行原4项，结果已为4通过。并行任务在上次失败快照后继续完成了修正，
本次核对调用链、加强测试并复验，未把已存在的生产修正冒称本次新写。

| 原失败项 | 已核实的正确处理 |
| --- | --- |
| 部分策略周期 | 失败策略仍保留结果和failed状态；部分失败周期不新开仓，已有退出继续执行。旧“只保留成功策略”断言已修正 |
| 早盘缺持仓 | 通过exit_filter_error记录可观测错误，没有虚假退出信号；旧“必须整体抛异常”断言已修正 |
| 监控缺分钟 | 先评估已有持仓退出，再返回入场错误；缺分钟不再整体抛错打断时钟退出 |
| 缺分钟且停牌 | 保留秒级时钟、持仓和仓库停复牌证据；停牌日不生成虚假卖出，入场仍被阻断 |

源码入口为StrategyEngine.run/_run_strategy、overnight_runtime.monitor_context、
MonitorRuleEngine.compute_strategy_group及失败结果发布。原4项来自
test_strategy_group_monitor_runtime.py和test_morning_liquidation.py。

## 本次修改与用例

仅增强test_morning_liquidation.py的两项缺分钟测试：

- 明确断言context.input_errors及result.entry_filter_error保存“缺少分钟数据”。
- 明确断言买入候选rows和entry_signal_hits均为空。
- 非停牌案例精确断言退出证券为a；停牌案例精确断言退出信号为空。

修改前保存了5个相关源/测试文件基线；核对后只有该测试文件发生本次变更，
3个相关生产源码和另一测试文件哈希保持。没有删除用例、放宽断言或改写业务参数。

## 验证结果

| 检查 | 实际结果 |
| --- | --- |
| 原4项定向复现、增强后及反向证明后复验 | 均4通过 |
| 跟单、费用、组合监控、早盘退出、协调器、OpenAPI及Full PostgreSQL | 356通过，0失败 |
| 既有提示 | 5条：1条Polars按组sortedness，4条datetime.utcnow弃用提示 |
| 两个目标测试文件Ruff | 通过 |
| 回归敏感性反向证明 | 临时进程将缺分钟分支恢复旧抛错处理，两项按预期失败；源码未改，进程内函数恢复 |
| 覆盖率 | 用户和项目未要求本次覆盖率门槛，按技能规则跳过，不报告覆盖率数值 |
| 单测工作流 | Step1～7已完成，utree flush已执行 |
| 仓库质量门 | make check：目录、文档、版本及diff格式检查通过 |

定向输出见four-tests.log（受控原始附件），扩大回归见regression.log（受控原始附件），
源/测试哈希及结果见evidence.json（受控原始附件）。新增0个测试入口，增强2个既有用例；
4个报告失败项均已解除，未发现该范围内仍需修复的新生产缺陷。

后端使用TICKFLOW_ENV_FILE=/dev/null、MULTITENANCY_ENABLED=false、SERVICE_ROLE=standalone、
POLARS_MAX_THREADS=4、工作区backend的PYTHONPATH。Full路径使用本次独立UTF8 PostgreSQL
127.0.0.1:56549，运行测试后停止；实际本地应用/Worker/PostgreSQL/Compute未切换。

## 边界

此前314通过/4失败是上次并行修改未收敛时的历史快照，原记录保留，
本回执补充当前结果。仅解除这4项测试阻塞，不代表已经完成所有发布或生产部署门禁。
股票范围、scope_only_v3完整池、30秒确认、选股/买卖/仓位/费用参数、
实际账户和历史冻结结果均保持。本次没有回测或交易执行。
