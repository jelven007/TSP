# 当前 PRD 已实现功能补齐记录

日期：2026-10-23。任务：根据当前系统已实现功能补齐现有 PRD。
源码基线为 `cc837b95f471e5b299bf5e8618332d500bff7e3a`，
工作分支 `docs/requirements-realtime-research-20261023`，应用版本保持 `0.4.17`。

主文档为[需求基线 / PRD](../../docs/requirements.md)。
本轮直接完善既有 Markdown，没有另建不同内容的 PRD；此前目录和文档变更保留。
使用 PRD 编写规范组织用户、触发、处理、结果、状态、权限与异常；
采用现有文档格式，S1–S3 后续规划不改为已实现。

## 1. 补齐内容

原 PRD 有 124 个唯一编号，本轮增加 67 条已实现行为，合计 **191 条唯一编号**。
第 4.10–4.20 节补齐当前具体流程；原第 4.1–4.9 节的领域需求保留。
同时补充首次使用流程、运行模式权限、业务对象、状态、金融计算与验收追踪。

| 节 | 新增需求与数量 | 解决的缺口 |
| --- | --- | --- |
| 4.10 账户与通用界面 | ENTRY-01–04、UI-01–04：8 | 向导、注册/找回、初始化、管理员保留、身份/资产切换、菜单/详情联动 |
| 4.11 看板与自选 | DASHBOARD-01–03、WATCHLIST-01–05：8 | 网格布局、保存失败边界、搜索、多组关系、导入预览/确认、筛选与清空 |
| 4.12 分析 | RESEARCH-08–11：4 | 个股关键价位、板块分钟质量、财务信息与市场环境/异动 |
| 4.13 策略 | STRATEGY-05–08：4 | 摘要/明细、参数显隐、入场条件、主题/风险、草稿/ID/删除保护 |
| 4.14 因子与挖掘 | FACTOR-05–09、MINING-01–03：8 | 目录/算子、试算保存、组合、状态、引用删除、嵌套验证与发布门槛 |
| 4.15 信号与提醒 | SIGNAL-01–04、LOTS-01–02：6 | 分钟已收盘规则、上升沿、本地回放、AI候选、批次字段与派生规则 |
| 4.16 回测 | BACKTEST-13–16：4 | 输入与成交时点、持久/短期任务边界、结果导出、候选、滚动窗口与DSR |
| 4.17 监控 | MONITOR-07–10：4 | 条件/资产/作用域、触发证据、渠道测试、邮件只读传输配置 |
| 4.18 模拟盘与权益 | PAPER-05–08、CORP-01–04：8 | 本金/费用、订单时刻/占用、自动退出、对比、登记/应收/到账/上市/缴款 |
| 4.19 AI与复盘 | AI-04–06、REVIEW-02–03：5 | 查询/动作权限、确认超时、历史容量/冲突、报告完成与定时推送 |
| 4.20 扩展与管理 | EXT-05–07、ADMIN-04–06、OPEN-05–06：8 | 完整快照、来源质量、字段通道、路由/Key、清库边界、配置/版本及开放权限 |

上表省略统一 `FR-` 前缀，完整编号以主文档为准。
当前源码的“已实现”表示有实现依据，不意味着本次重跑全部页面或通过生产签收。

## 2. 实现和测试依据

以下是核对入口，引用测试不自动表示本轮运行过；实际执行范围见第 3 节。

| 功能 | 主要实现依据 | 现有契约/回归 |
| --- | --- | --- |
| 向导/角色/空间 | [向导](../../frontend/src/pages/Onboarding.tsx)、[身份服务](../../backend/app/multitenancy/identity.py)、[权限边界](../../backend/app/multitenancy/access.py) | [邮箱账户](../../backend/tests/test_auth_accounts.py)、[多租户身份](../../backend/tests/test_full_identity.py) |
| 看板/自选 | [布局保存](../../frontend/src/components/dashboard/useDashboardLayout.ts)、[自选](../../backend/app/services/watchlist.py)、[导入](../../frontend/src/components/WatchlistImportDialog.tsx) | [布局](../../backend/tests/test_dashboard_layout_prefs.py)、[分组](../../backend/tests/test_watchlist_groups.py)、[CSV](../../backend/tests/test_watchlist_csv.py) |
| 分析/质量 | [个股分析页](../../frontend/src/pages/StockAnalysis.tsx)、[分钟质量](../../backend/app/services/sector_minute.py)、[市场口径](../../docs/reference/market-data.md#phase) | [板块质量](../../backend/tests/test_sector_minute_quality.py)、[成分分时](../../backend/tests/test_dimension_intraday.py) |
| 策略/迭代 | [创建窗口](../../frontend/src/components/screener/StrategyBuilderDialog.tsx)、[策略接口](../../backend/app/api/strategy.py)、[迭代器](../../backend/app/strategy/ai_iterator.py) | [公开目录](../../backend/tests/test_builtin_catalog.py)、[参数规范](../../backend/tests/test_strategy_param_normalize.py)、[AI迭代](../../backend/tests/test_ai_iterate.py) |
| 因子/生命周期 | [因子接口](../../backend/app/api/factors.py)、[定义存储](../../backend/app/factors/store.py)、[目录](../../backend/app/factors/registry.py) | [因子API](../../backend/tests/test_factor_api.py)、[因子存储](../../backend/tests/test_factor_store.py)、[DSL](../../backend/tests/test_factor_dsl.py) |
| 挖掘/发布门槛 | [挖掘引擎](../../backend/app/backtest/mining.py)、[挖掘接口](../../backend/app/api/mining.py)、[口径](../../docs/reference/research.md#mining) | 复用挖掘与发布相关测试；真实数据与容量须独立验证 |
| 信号/持仓提醒 | [信号接口](../../backend/app/api/signals.py)、[分钟特征](../../backend/app/strategy/intraday_features.py)、[批次规则](../../backend/app/strategy/lots.py) | [盘中信号](../../backend/tests/test_intraday_signals.py)、[因子条件](../../backend/tests/test_custom_signals_factor_conditions.py)、[提醒](../../backend/tests/test_lots.py) |
| 研究结果 | [回测接口](../../backend/app/api/backtest.py)、[冻结配置/输入](../../docs/reference/research.md#research) | [窗口边界](../../backend/tests/backtest/test_walkforward_boundaries.py)、[统计口径](../../backend/tests/backtest/test_trade_quality_stats.py) |
| 通知 | [监控引擎](../../backend/app/strategy/monitor.py)、[通道设置](../../frontend/src/pages/settings/Monitoring.tsx) | [通知配置](../../backend/tests/test_notification_settings.py)、[适配器](../../backend/tests/test_notification_adapters.py)、[邮件默认](../../backend/tests/test_email_defaults.py) |
| 模拟盘/公司行为 | [模拟账务](../../backend/app/strategy/paper.py)、[公司行为](../../docs/reference/trading-ai.md#corporate)、[权益UI](../../frontend/src/components/PaperCorporateActions.tsx) | [权益账务](../../backend/tests/test_paper_corporate_actions.py)、[撮合时刻](../../backend/tests/test_paper_settle_price_time.py) |
| 助手/报告 | [动作确认](../../backend/app/custom/assistant/actions.py)、[工具](../../backend/app/custom/assistant/tools.py)、[会话规则](../../docs/reference/trading-ai.md#sessions) | [会话冲突](../../backend/tests/test_assistant_sessions.py)、[多租户会话](../../backend/tests/test_full_assistant_sessions.py) |
| 扩展/开放 | [扩展拉取](../../backend/app/services/ext_pull.py)、[能力矩阵](../../backend/app/data_providers/capabilities.py)、[开放规则](../../backend/app/services/api_gateway.py) | [完整快照](../../backend/tests/test_ext_pull_complete.py)、[开放契约](../../backend/tests/test_openapi_contract.py) |

重点核对的事实：

- 因子状态由用户显式选择，当前没有“检验达标后才能激活”的自动强制门禁；
  改公式回草稿，删除默认检查已有引用，强制删除不自动修复下游。
- 自选一股可在多组；移出、清空或删除一个组不删除自选证券。
- 持仓提醒只派生规则，不进入模拟盘账务。
- 模拟盘登记/应收/到账/上市与可用资金分开，默认源业务日期未认证时暂停相关处理。
- 清除数据使用现有数据集范围，不等同研究输入归档或通用撤销。
- 独立模式的短期回测连接、持久研究任务、历史会话同步分别说明，不承诺跨进程重启恢复所有执行。

## 3. 本轮验证

| 检查 | 结果 |
| --- | --- |
| PRD 编号与结构 | 191 个唯一编号；原 124 条编号顺序与定义保留，新增67条；11个具体功能章节 |
| 原目标保持 | S1–S3 阶段和原目标需求状态保留，不以本轮补写改为已实现 |
| 表格/围栏 | 列数与代码围栏配对检查通过 |
| 目录/版本/whitespace | `make check`通过：68个入口；应用版本0.4.17一致；`git diff --check`通过 |
| PRD与本报告的本地链接 | 83 个本地文件链接全部有效 |
| 数值与边界 | 因子公式/试算/组合、模拟盘费用/持仓/顺延、挖掘范围/门槛与源码定义一致 |
| 定向领域测试 | **184 passed, 20 warnings**，16.47秒；没有跳过 |

测试使用临时隔离 CPython 3.12 / Polars 1.44.2 环境与现有夹具；
没有使用本轮新建的模拟测试替代现有实现。命令从 `backend` 执行：

```text
python -m pytest
  tests/test_watchlist_groups.py tests/test_watchlist_csv.py
  tests/test_dashboard_layout_prefs.py tests/test_lots.py
  tests/test_intraday_signals.py tests/test_custom_signals_factor_conditions.py
  tests/test_factor_api.py tests/test_factor_store.py
  tests/test_paper_corporate_actions.py tests/test_notification_settings.py
  tests/test_notification_adapters.py tests/test_email_defaults.py
  tests/test_auth_accounts.py tests/test_ai_iterate.py
  tests/test_assistant_sessions.py tests/test_builtin_catalog.py
  -q -rs -p no:cacheprovider
```

该文件组覆盖本轮补齐的关键状态/边界，不是全量后端或页面端到端验收。
20条警告均来自自选组/CSV用例使用既有 `watchlist.py` 的 `datetime.utcnow()`，
没有为文档补齐修改业务代码或处理该历史弃用项。
未执行前端构建/浏览器、真实模型/Provider、一次性PostgreSQL、Compute、
Windows真机、开盘/十年容量或生产验证；历史验证不计入本轮结果。
本轮仅修改 PRD 并增加核对报告，未改业务代码、版本、数据库或部署。
