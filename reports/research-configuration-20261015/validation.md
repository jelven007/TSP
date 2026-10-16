# 第三批研究配置升级验证

基线：`bc09e3d`，分支 `feat/phase1-full-multitenancy`。本批改动仅保存在本地，
未提交、推送或部署；工作区原有租户目录显示名称改动保留。

## 实现范围

- 入场叠加条件接入设置、选股、实时监控、日线回测、参数优化和步进优化。
- 在入场评分前过滤，按证券实际交易记录偏移，条件及暖机依赖参与数据计划和缓存。
- 缺字段时关闭入场、保留可计算的退出，并避免因缺数据制造候选池退出。
- 每组与每折使用独立嵌套配置，保存实际参数试验和失败原因；OOS 失败仍保留训练记录。
- 复用 SQL 持久任务保存私有输入、行情版本及结果出处，结果页支持下载配置与试验记录。
- 仅为完整夏普参数网格使用实际每日组合收益统计计算 DSR，标明本次网格及独立试验近似。
- 自动入场事件携带条件，沿用原入场政策快照保存至订单、成交和持仓。

业务契约详见 [研究配置与试验记录](../../docs/research-configuration.md)。

## 验证结果

| 验证 | 结果 | 记录 |
| --- | --- | --- |
| 扩大后端回归，92 个测试文件，925 项不同测试 | 首轮 924 passed，1 项原有超时测试失败；该项单独复跑通过 | `/tmp/tsp-third-final-regression.log`、`/tmp/tsp-third-timing-recheck.log` |
| 最终组合策略、API、回测正确性与条件边界复验 | 66 passed，包含于上述覆盖，不重复计数 | `/tmp/tsp-third-composite-final.log` |
| PostgreSQL 研究、运行、监控、计算与模拟盘定向验证 | 58 passed，包含于上述覆盖 | `/tmp/tsp-third-final-pg.log` |
| 全部前端测试 | 130 passed | `/tmp/tsp-third-frontend.log` |
| TypeScript 与 Vite 生产构建 | 通过 | `/tmp/tsp-third-build.log` |
| Ruff 与前端 ESLint 对照已提交基线 | 无新增诊断 | `/tmp/tsp-third-lint-new.json`、`/tmp/tsp-third-frontend-lint-new.json` |
| 差异空白检查 | `git diff --check` 通过 | 本地执行 |

扩大回归的范围为 `tests/backtest/`，以及 `test_strategy_*`、`test_screener_*`、
`test_monitor_*`、`test_composite_*`、`test_paper_*`、`test_full_*`，
另包含统计函数、分钟策略和相关监控边界测试。PostgreSQL 使用独立临时容器，
测试夹具创建并清理独立数据库及角色，不连接生产数据库。

首轮失败的 `test_full_ai.py::test_confirmation_is_session_owned_durable_and_expires`
使用 30 毫秒审批过期窗口，在完整回归负载下返回 `expired`，单独复跑通过。
该测试与审批实现均未修改。另修正原有实时刷新测试桩，使其接受已提交行情服务调用的
`execution_events` 参数，继续验证策略结果更新通知，未修改通知实现。

## 新增失败回归依据

修复前已复现以下错误，再验证修复后通过：

- 同一优化任务内嵌套条件和固定参数被前一组修改；最佳参数重跑及 OOS 配置随之变化。
- 优化结果缺试验族、实际收益统计和不完整状态；非有限目标可能被选为最佳组合。
- OOS 失败丢失训练试验及测试配置；SQL 任务结果缺少输入快照出处。
- 入场字段缺失阻断显式退出；关闭入场后产生伪造的全部 `pool_exit`。
- 自动入场事件缺少叠加条件快照。

这些验证使用固定输入及实际 PostgreSQL 事务，证明代码契约，不作为策略收益、
生产成交可达性或 DSR 统计假设有效性的证明。当前全量独立候选模式不计算每日账户 DSR；
取消、过期租约和权限撤销继续丢弃迟到结果。
