# 测试与验收指南

2026-10-25本次结果统一见[生产基线验证](../reports/production-baseline-20261025/verification.md)。
命令和矩阵定义“如何验证”；带日期的旧计数只证明当时版本。
示例迁移验证从 `examples/custom-data-source/` 加载真实服务与YAML，
检查日K、复权、实时响应。源码恢复工具的测试在
`tests/tooling/test_source_recovery.py`，使用一次性Git仓库覆盖六版本选择、
非主线标签排除、未打标签拒绝、归档损坏、路径越界、重复版本和旧目录保护。

统一功能A–I、二期第三步3.1–3.6、负载/持续窗口、恢复目标和签收模板见
[生产验收](#acceptance)。本地文件清单及实际检查见
[生产体系验证](../reports/production-system-consolidation-20261023/verification.md)；
历史通过数保持日期，不能计入本次。

一级页面顶栏按 `FR-UI-05` 验证：PageHeader 保留富文本说明、状态和按钮回调，
标题独立态不生成空工具栏；浏览器在1440px与390px下检查各菜单标题/间距/操作区边界，
长工具栏可滚动、菜单按钮无遮挡，页签和弹窗保持原交互。浅/深色、加载/空/失败态
沿用同一外观，不把页面暂无行情解释为顶栏渲染失败。

2026-10-24 完整本地变更验证见[最新整合记录](../reports/production-integration-20261024/verification.md)：
后端全量、仓库工具、前端 test/lint/build、原生 Cargo 和页面检查分别留证。
历史批次覆盖首次手动同步与调度暂停、管理员幂等/旧身份、跨平台认证默认、
SMTP未配置/优先级/LOGIN、路由保护和亮色恢复均纳入定向边界。
CI、Docker手动质量门及桌面质量门提供独立 PostgreSQL 16 服务，
`TSP_TEST_POSTGRES_DSN` 仅指向一次性测试库；默认配置测试自行清理环境变量，
旧独立业务测试以显式 standalone/空dotenv/隔离DATA_DIR执行。
Cargo 使用项目 `.venv/bin/python`，避免链接失效的系统 Python。
测试数据目录由步骤读取 `$RUNNER_TEMP` 后写入 `$GITHUB_ENV`；
Job 级 `env` 不允许引用 `runner` 上下文。工作流修改应另外运行
`actionlint -shellcheck= -pyflakes=` 检查 Actions 表达式，YAML 解析不能替代该检查；
该命令不执行内嵌 Shell/Python 的静态分析，实际运行结果仍由远程质量门确认。

初始化自动同步回归覆盖缺省字段全部开启、已保存关闭值不被覆盖、首次任务单飞与
部分落盘重试、修改时间保留暂停、完整性/五档启动门控、概念/行业后台任务与旧配置修复。
财务回归断言周期同步在工作线程执行、互斥状态、同步时间和仓库写锁发布，并覆盖总开关关闭与
能力缺失；见 `backend/tests/test_financial_auto_sync.py`。默认值统一见
[运维配置](operations.md#config-数据页初始化默认值)，不把历史手动默认测试结论当作当前行为。

官方个股目录回归见 `test_official_stock_catalog.py`、`test_instrument_catalog_admin.py`：
覆盖两市四板、总数与字段校验、股本单位、重复/空/不完整目录拒绝、下载失败保留快照、
管理员 CRUD、成员 403、代码/股本验证、同日删除 tombstone、写入失败恢复历史、
元数据来源保留及与行情任务独立单飞。`test_manual_sync_defaults.py` 断言启动同步、
周末与节假日的北京时间 09:00 cron、总开关关闭仍执行目录任务；关闭路径断言外层
协程取消后仍等待下载线程结束，下载前取消进入终态。历史状态、缓存与发布同时复核。
前端 `StockCatalogDialog.test.tsx` 覆盖新增/编辑表单重置、CRUD、删除确认及拒绝写入时保留草稿。
页面联调使用真实组件、API、仓库和隔离临时数据，检查桌面暗色及 390px 亮色下的
表单滚动、增改、删除确认/取消、错误重试及成员拒绝；完整应用认证部署另沿用平台门禁。

本文档定义 TSP 的测试层次、最低门禁和版本验收流程。功能验收依据
[需求基线](requirements.md)，工程细则依据[贡献与复审指南](../CONTRIBUTING.md)，
被测源码、生产和标签状态依据[版本基线](version-baseline.md)。

目录与关键入口先执行根目录 `make check`，工具测试执行 `make test-tooling`；
测试归属见[测试目录](../tests/README.md)，脚本新路径见
[工程目录与迁移表](reference/engineering.md#layout)。
CI在后端质量门中检查目录、文档契约并收集整个`tests/tooling/`，前后端既有测试入口保持不变。

2026-10-23 当前功能与源码的对应关系见[实现索引](architecture.md#capabilities)；
本轮文档核对的实际结果见[同步记录](../reports/documentation-sync-20261023/verification.md)。
下文带日期的通过数是历史记录，不计入本轮。

2026-10-22 整合测试覆盖五档证据与候选统计、收盘临时价/权威缓存/重启、
公共发布/回收/空闲读者、Windows 管理员与资源预算、仓库写入锁和前端账户切换。
整合态执行结果见本次交付报告，历史报告中的失败和跳过不追溯改成通过。

```bash
cd backend
uv run --no-sync pytest tests/test_depth_snapshot_validation.py tests/test_closing_price_cache.py tests/test_final_sync_confirmation.py tests/test_realtime_guard.py tests/test_full_platform.py tests/test_full_host_maintenance.py tests/test_health.py tests/test_online_timeout_validation.py tests/test_local_admin.py tests/test_resource_budget.py tests/test_datastore_memory_budget.py tests/backtest/test_panel_cache_budget.py -q
cd ../frontend
pnpm test
pnpm lint
pnpm build
```

生产旧补丁镜像的 100/250/500 SSE 压测共 93,360 次请求零错误，
500 档 P99 152.6 ms，详见[延长验证](../reports/online-timeout-20261021/extended-validation.md)。
该结果不证明整合源码已上线、业务连接池超时已修复或完整交易日已通过。
Windows CI、Win10 22H2 真机与安装包验收分别记录；
真实数据同步、全部研究任务、取消/重启和进程树内存仍须真机覆盖。
阶段与验收门槛见[本次整合](../reports/local-consolidation-20261022/verification.md#local-contract-2026-10-22-本地变更整合与生命周期基线)。

原生 Windows 10 22H2 x64 / 16 GB 的资源检查、同规模缓存基准和全功能阻断项见
[原生资源优化验收](operations.md#windows)。
`windows-native.yml` 在 Windows Server 2022 执行 PowerShell 5.1、资源预算和
worker 回归，不能替代 Win10 真机与隔离 Compute 验收；缺少目标环境的测试须标记
“未执行”，不能把跳过或 Compute readiness 错误视为通过。

<a id="1-测试目标"></a>
## 1. 测试目标

测试不仅验证接口能返回，还要证明：

- 金融字段、单位、复权、时点和交易规则正确。
- Provider 替换后，上层仍使用统一契约。
- 文件发布、缓存失效和前端刷新保持一致。
- 网络失败、空数据、缺少能力和部分写入不会产生伪成功。
- 历史配置、策略和数据在升级后仍可读取，或有明确迁移方案。
- 发布产物可以在目标平台安装、启动、持久化和回退。

<a id="2-测试层次"></a>
## 2. 测试层次

| 层次 | 主要范围 | 典型证据 |
| --- | --- | --- |
| 单元测试 | 纯函数、规则、字段转换、日期和费用 | 固定输入输出断言 |
| 契约测试 | Provider、API、schema、OpenAPI 快照 | 成功、空、错误、缺能力 |
| 服务集成测试 | 同步、仓库、缓存、调度、任务状态 | 临时数据目录和完整调用链 |
| 领域回归测试 | 策略、因子、回测、监控、模拟交易 | A 股边界和历史缺口样例 |
| 前端测试 | 页面状态、交互、类型和查询缓存 | Vitest、ESLint、TypeScript 构建 |
| 端到端烟测 | 启动、登录、同步、核心页面、持久化 | 可复现人工记录 |
| 发布产物测试 | Docker 和桌面安装包 | 架构/平台矩阵、哈希和启动验证 |

<a id="3-本地测试环境"></a>
## 3. 本地测试环境

后端使用 Python 3.11+、`uv` 和 Rust 编译工具链（构建默认原生桥接），前端使用
Node 20、pnpm 9。首次准备：

```bash
cd backend
uv sync --extra dev --extra cell --frozen
uv run --frozen --no-sync python -m app.plugins.rustdx.client

cd ../frontend
pnpm install --frozen-lockfile
```

自动化测试默认应使用临时目录、fixture 和 mock，不应读取或修改真实 `data/`。
需要真实 Provider 的测试必须单独标记和运行，不能把密钥写入命令、日志或测试文件。
初始化手动同步回归覆盖：无偏好文件和历史缺字段时自动同步/实时/分钟/扩展拉取关闭；
首次或部分失败后启动不创建任务，仍可手动完成全量同步；竞价窗口内启动也无立即任务；
修改调度时间保持暂停，显式启停后下一次运行时间正确；五档缓存恢复不触发取数；
菜单默认顺序、返回值副本和已有配置保留。
主题回归覆盖无偏好、无效值和存储不可用时默认白天模式，首屏脚本与运行时一致，
已有亮色/暗色选择恢复，以及主题切换后的持久化。

```bash
cd backend
uv run pytest tests/test_manual_sync_defaults.py tests/test_preferences_cache.py tests/test_initial_sync.py tests/test_ext_presets_startup.py tests/test_minute_refresh.py tests/test_data_integrity.py tests/test_collection_contract_fixes.py -q
cd ../frontend
pnpm test src/lib/theme.test.ts
pnpm build
```

管理员初始化与SMTP受控配置回归包含 macOS/Linux/Windows、源码/冻结运行、
非 Windows 默认多租户及认证、Windows 默认单机、无 dotenv/空模板、注册及密码找回
默认口令初始化、旧身份与已改密码/口令保留、SMTP 优先级和 API 授权码不泄漏：

```bash
uv run --frozen --no-sync pytest tests/test_config_env.py tests/test_auth_env_bootstrap.py tests/test_local_admin.py tests/test_auth_runtime_mode.py tests/test_auth_accounts.py tests/test_email_defaults.py tests/test_notification_adapters.py tests/test_default_identity_startup.py tests/test_full_identity.py -q
```

`test_full_identity.py` 的数据库验收需要隔离 PostgreSQL 和
`TSP_TEST_POSTGRES_DSN`；运行角色创建管理员和无 DDL 权限必须通过真实数据库验证，
启动顺序 mock 不能替代此项。管理员默认空间验收包括私有业务读写与跨租户拒绝、
并发初始化、重启后数据/密码/角色/租户选择保留、已就绪空间复用、双库激活失败后
登录重试、容量不足和停用账户不被重新启用；`test_default_identity_startup.py` 另核对启动接线。
本轮SMTP自动化使用模拟传输，未执行真实邮箱认证或投递。
原生桥接安装检查不连接外部节点；缺失、依赖版本、来源提交、接口或连接上限不匹配
均以非零状态退出。默认依赖回归见 `tests/test_rustdx_installation.py`，本地协议
连接池回归见 `tests/test_rustdx_native_pool.py`。

2026-10-17 默认安装专项验证：macOS arm64、CPython 3.11 的全新隔离环境使用
普通 `uv sync --frozen` 构建安装桥接；再次默认同步和旧 `--extra rustdx` 命令
均保留可用桥接。安装、Provider、初始化、原生连接池、桌面打包与版本专项
`170 passed`，Rust 单元测试 `6 passed`，前端生产构建、定向 Ruff 和版本递增
检查通过。Rust 本地测试显式指定 `PYO3_PYTHON` 为后端虚拟环境解释器。

实际 Worker 镜像构建能拒绝缺少桥接的旧运行镜像；Linux 原生构建在拉取
`rust:1.99-slim-bookworm` 时被 Docker Hub `Bad Gateway` 阻断，未完成 Linux
wheel 和完整镜像验收。Windows 启动和桌面安装包未在本机运行，需对应平台
流水线验证；该记录不代表生产部署已更新。

同日修复基础镜像拉取路径后，通过 `BASE_IMAGE_REGISTRY=public.ecr.aws/docker`
完成 Linux arm64 的完整运行镜像与 Compute Worker 镜像构建。Rust、Node、Python
基础镜像在 ECR 与 DaoCloud 的清单摘要一致。固定 rustdx Git 提交使用已验证
的本地 Cargo 缓存构建，不读取主机凭据或修改依赖锁。
运行镜像内原生连接池测试 `5 passed`，桥接版本校验、启动、存活及就绪端点、
七项默认 Provider 能力和前端静态页面均通过。启动测试使用隔离临时数据及禁用
外部网络；空数据的新鲜度警告保留，不代表全量数据同步验收。
Linux amd64 API/Compute 已通过本机 Buildx/QEMU 的构建、清单、原生桥接、
Gateway/SSE 与 Compute 元数据烟测；Apple ARM 仿真缺少 AVX/AVX2，不能替代
原生 amd64 重负载或容量。Windows 产物仍需 `windows-latest` 和 Windows 10/11
真机验证，现有运行服务与生产部署未切换。

同日 P2 候选又从当前未提交源码构建 Linux arm64 API/Compute 镜像，两份构建清单
离线核验无修改、缺失或新增文件；Compute 以 UID 10001、无网络和只读公共数据
执行完整策略计算及独立恢复验收。`backend/scripts/acceptance/accept_market_pipeline.py` 可在新的隔离
目录对真实历史数据执行 Provider→标准化→落盘→指标→平台 marker/Arrow 快照→
页面 API 的有界分段量测；结果必须保留 `source_freshness_certified` 和
`trading_day_soak_certified`，历史或周末样本不得把这两个字段标为真。
历史结果与未签收范围汇总见[主线整合记录](../reports/main-consolidation-20261018/verification.md)。
默认 Docker 镜像必须直接执行主应用启动烟测；仅运行 Compute 或桥接自检不足以发现
API 导入依赖缺失。PostgreSQL 适配器现为默认依赖，空 `BACKEND_EXTRAS` 构建也须
通过 `/health/live`，旧 `--extra cell` 只作为兼容命令保留。

2026-10-20 局部刷新、分钟空值与实时写盘门闸集成验证：后端全量
`3866 passed, 120 skipped`，受影响的修正、完整性、分区和容量回归 `110 passed`；
前端 44 个文件 `205 passed`，ESLint `0 error / 74 warning`（均为既有基线告警），
生产构建通过。新增门闸模块/测试及新增导入的定向 Ruff 通过，版本一致性和
`git diff --check` 通过。真实本地 SSE 在约 60 秒收到 `access_revoked`，前端回归
确认静默换票且不整页重载；本地页面行为已人工确认。生产仍固定在 v0.4.11，本批
源码验证不等于线上部署验收。
完整产品的 Compute 镜像还必须通过 `scripts/acceptance/verify_full_compute.py`；该验收包含
`signal_backtest`，用于证明 Compute 层已经按冻结锁安装 `backtest` extra，而不是
只验证不依赖 `vectorbt` 的矩阵回测、优化和挖掘路径。

<a id="4-标准命令"></a>
## 4. 标准命令

当前文档涉及的契约复核可运行：

```bash
cd backend
uv run --frozen --no-sync pytest -q \
  tests/test_capability_matrix.py tests/test_corporate_action_provider.py \
  tests/test_paper_corporate_actions.py tests/test_factor_registry.py \
  tests/test_factor_dsl.py tests/test_openapi_contract.py tests/test_ai_iterate.py \
  tests/test_assistant_sessions.py tests/test_second_quotes_api.py
```

至少核对八项独立路由、公司行为日期/权益/T+1/幂等及旧账本重放、
77 项内置因子目录/DSL、69 项开放契约、迭代草稿与会话冲突。
依赖完整项目环境运行；一次性 PostgreSQL、真实 Provider、Windows 和产物验证另列。
涉及公司行为 UI 时补 `PaperCorporateActions.test.tsx` 与 `Paper.test.tsx`；
涉及数据源 UI 时补 `DataSources.test.tsx`，覆盖 TickFlow 卡片/可选标签移除、
rustdx/fuyao 顺序、未就绪源先选路由、共享缓存不被展示排序改写、旧来源显示、
详情切换和按声明能力套用。仅文档及配置模板说明变化不要求重复未变更的前端全构建。
能力顺序调整须核对 `test_capability_matrix.py` 的注册表与矩阵顺序，
以及 `DataSources.test.tsx` 中路由卡、能力标签、详情表沿用矩阵顺序的断言；
插件以不同顺序声明能力时，界面顺序仍一致。

<a id="41-后端"></a>
### 4.1 后端

全量测试：

```bash
cd backend
uv run pytest tests -q
```

定向测试和静态检查：

```bash
cd backend
uv run pytest tests/test_capability_matrix.py -q
uv run pytest tests/backtest/test_cost_model.py -q
uv run pytest tests/test_openapi_contract.py -q
uv run ruff check app/path.py tests/path.py
```

高风险共享模块改动应先跑定向用例定位问题，再跑后端全量测试。

策略校验与加载的安全回归：

```bash
uv run pytest tests/test_strategy_safety_validation.py tests/test_strategy_code_save.py tests/test_strategy_registry.py tests/test_full_compute.py -q
```

覆盖危险函数别名、dunder 拼接、模块句柄、文件原语及通配/相对导入；
主文件和辅助文件读取、解码、语法与校验异常均须在模块执行前拒绝。
同时验证全部内置策略、合法 Polars/矩阵表达式、辅助依赖、保存失败不落盘、
拒绝更新保留旧策略，以及租户保存拒绝发生在计算服务和私有存储调用前。
载荷仅参与 AST 解析或由模拟加载器阻止执行，测试不读取真实用户策略。

模拟交易日线 schema 回归：

```bash
uv run pytest tests/test_paper_daily_scan_schema.py tests/test_paper_trading.py \
  tests/test_paper_api.py tests/test_paper_settle_price_time.py -q
```

模拟交易应通过统一日线存储 schema 读取新旧股票、ETF 与指数日线分区，
允许 `quote_ts` 等附加列并补齐历史缺列。固定样本断言当日开收盘、
前收盘和沪深 300 基准价格，相关回归覆盖涨跌停判断、T+1 与结算时间。

A 股业务默认日期回归：

```bash
uv run pytest tests/test_ashare_date_defaults_beijing_today.py \
  tests/test_backtest_stream_date_guard.py tests/backtest/test_factor_default_range.py \
  tests/test_factor_api.py tests/test_regime_builder.py tests/test_market_mainline.py -q
```

把服务器本地日期与北京日期错开，验证信号、因子、策略回测及三类 SSE 入口、
因子试算和保存前校验、市场状态与主线重算、日线修复使用 `cn_today()`。
显式日期保持原有语义；北京当日应通过未来日期检查，北京次日须被拒绝。

P2 缓存换代、固定自动卖出和板块分钟质量回归：

```bash
uv run pytest tests/test_enriched_warmup_retry.py tests/test_paper_auto.py \
  tests/test_sector_minute_quality.py tests/test_sector_rotation.py \
  tests/test_dimension_intraday.py -q
```

覆盖预热换代后恢复、持续换代上限、读取失败、重复预热，以及固定卖出封顶、
T+1、待卖占用、账户隔离和事件重放。分钟样本覆盖除权前收、历史 ST、
新股无涨跌幅限制、未知规则、异常缺口、全异常和 API 空值序列化；
前端同时检查质量提示、覆盖率和不可计算状态。
最终合并回归及源码范围见
[主线整合记录](../reports/main-consolidation-20261018/verification.md)。

P2 参数交互补充验证 `test_strategy_param_normalize.py` 的 API 保存/读取，
前端 `strategyParams.test.ts` 和 `paramSweep.test.tsx` 的分组、递归显隐、
预设填值、隐藏值保留和扫描排除。页面联调需验证设置保存后在回测回显同一值，
控制条件切换后组合数变化，以及浅/深色窄屏可操作性。
合并结果见[主线整合记录](../reports/main-consolidation-20261018/verification.md)。

策略质量、成交正确性和入场风险回归：

```bash
cd backend
uv run --frozen pytest -q \
  tests/backtest/test_strategy_quality_inputs.py \
  tests/backtest/test_quality_entry_variants.py \
  tests/backtest/test_open_limit_execution.py \
  tests/backtest/test_portfolio_execution_order.py \
  tests/backtest/test_entry_risk_policy.py \
  tests/backtest/test_trade_quality_stats.py \
  tests/backtest/test_walkforward_boundaries.py \
  tests/test_paper_risk_policy.py tests/test_strategy_monitor_events.py
```

至少覆盖原始价资格、缺评分排除、开盘封板、最低佣金、原始申报数量、买单过期、
未平仓、开盘资金阶段、交易日滚动窗口、冻结 ATR/信号低点和权益风险预算。
研究脚本的现金核算、账户勾稽、输入/源码身份和候选判定属于额外证据，不能替代
领域回归。该分支最终合并两批逐用例 ID 后为 `704 passed, 6 skipped`；前端
TypeScript/Vite 构建通过。完整实现与全市场研究限制见
[策略升级实施记录](../reports/strategy-optimization-20261019/implementation.md)。

策略组协调器、共享计算与组合裁决回归：

```bash
cd backend
uv run --frozen pytest -q \
  tests/test_strategy_group_coordinator.py \
  tests/test_strategy_group_monitor_runtime.py \
  tests/test_strategy_portfolio_arbitration.py \
  tests/test_strategy_monitor_events.py \
  tests/test_paper_auto.py tests/test_paper_risk_policy.py \
  tests/test_paper_trading.py tests/test_full_paper.py
```

专项必须证明容量 1 最新快照、周期唯一性、失败隔离、规则变更丢弃、通用规则逐快照、
共享历史/矩阵、同策略唯一求值、同股去重、退出优先、账户最保守预算、持仓归因及
`partial/stale`周期禁止入场。当前相关监控、模拟交易和行情调用链扩大回归为
`271 passed, 41 skipped`；跳过项主要依赖未提供的一次性 PostgreSQL 环境，
不能据此宣称多租户真实数据库验收完成。阶段证据见
[五策略实施记录](../reports/five-strategy-parallel-20261019/implementation.md)。

本次主线集成前最终门禁为：后端全量 `3853 passed, 120 skipped, 89 warnings`
（232.54 秒）；前端 40 个 Vitest 文件共 `191 passed`；ESLint 0 错误、75 条
既有警告；TypeScript/Vite 生产构建通过并保留既有大分块提示。数据库相关跳过项
仍须按下文提供一次性 PostgreSQL 后单独验收，不能把本地全量通过解释为该环境已覆盖。
与 `0.4.10` Windows 原生运行主线合并后的集成态再次执行后端全量，结果仍为
`3853 passed, 120 skipped, 89 warnings`（249.42 秒）；前端再次得到 40 个文件
`191 passed`、ESLint 0 错误/75 条既有警告且生产构建通过。

完整单机多租户验收使用 `backend/tests/test_full_*.py` 和独立 PostgreSQL：
通过 `TSP_TEST_POSTGRES_DSN` 指向可创建/删除临时数据库与角色的测试服务。
未配置 DSN 时跳过的数据库用例不算通过。需覆盖身份/RBAC、RLS、任务/事件、
模拟交易事务、迁移与平台边界；发布验收见[一期记录](../reports/document-consolidation-20261024/historical-evidence.md#phase1)。

单机容器化可靠性回归：

```bash
cd backend
uv run pytest -q \
  tests/test_kline_daily_latest.py tests/test_full_platform.py \
  tests/test_full_administration.py tests/test_continuous_acceptance.py \
  tests/test_worker_process.py tests/test_full_compute.py \
  tests/test_full_compose_config.py tests/test_full_host_maintenance.py \
  tests/test_candidate_soak_observer.py
```

该组覆盖：

- 多租户 API/Platform 空历史快速 503，且不调用 Provider。
- 行情代理短超时、管理代理长超时及稳定错误契约。
- stock/ETF/index 最新原始日分区与 enriched symbol 覆盖门槛。
- 隔离违规与同租户一致性违规分离、旧验收状态兼容。
- Worker 按类型 freshness、真实 Broker 沙箱探针、有界等待和 stderr 尾部。
- 16 GiB Compose 预算、36 条双库连接上界。
- 双库备份恢复、release 引用保护、保守 Docker 清理和告警状态转换。

本批相关回归实际结果为 `86 passed, 15 skipped`。跳过项来自测试容器没有
`TSP_TEST_POSTGRES_DSN` 或 Docker CLI；Compose 在宿主机另行完成解析，双库备份脚本
在一次性 PostgreSQL 16 中实际完成 dump、restore 和 schema v6 校验。相关文件 Ruff
和 `git diff --check` 通过。`kline.py` 保留既有全文件 Ruff 技术债，不能把本批
未新增告警写成全文件 Ruff 通过。

当前长时验收以固定提交 `47b1738d` 和同源镜像执行：

- Active/Passive：2026-10-18 18:37:24 至 2026-10-19 18:37:24。
- 100 个独立用户每 20 秒执行真实登录、自选写读和日线请求，保持 100 条 SSE。
- 私有探针每分钟注入，研究任务每 15 分钟轮换提交。
- 完整交易日：2026-10-19 09:15 至 15:40，单独检查秒级归档、分钟和公共事件。
- 24 小时通过不设置 `certified_72h_100_active_users=true`，也不替代原生
  Windows/AMD64 或生产恢复验收。

实时状态与证据位置见
[单机持续验收记录](../reports/single-host/continuous-acceptance.md)。

二期第一步的本地百人关键链路验收使用同类一次性 PostgreSQL 服务：

```bash
cd backend
TSP_TEST_POSTGRES_DSN='<一次性 PostgreSQL 根 DSN>' \
  uv run python -m scripts.acceptance.accept_phase2_step1 \
  --users 100 --rounds 5 --pool-size 32 \
  --output ../reports/phase2-step1-20261016/local-100-user-acceptance.json
```

该脚本会创建并删除临时两库和运行角色，覆盖独立登录、私有业务读写、策略读取、
流换票/重放拒绝、租户事件 LISTEN/RLS 隔离、公共 marker 及真实撤权通知。
请求在进程内 ASGI 执行，不经过 Nginx、容器网络和真实行情源，不能替代候选主机
容量、SSE 长连接或交易时段验收。

二期第二步的本地长台账与 100 用户混合负载验收：

```bash
cd backend
TSP_TEST_POSTGRES_DSN='<一次性 PostgreSQL 根 DSN>' \
  uv run python -m scripts.acceptance.accept_phase2_step2 \
  --users 100 --rounds 2 --pool-size 32 \
  --ledger-rows 240 --nav-rows 180 \
  --research-concurrency 4 --steady-request-interval 5 \
  --output ../reports/phase2-step2-20261016/local-100-user-mixed-acceptance.json
```

该脚本为每个租户构造模拟交易长历史、禁用通知渠道和研究任务，同时运行私有事件、
公共 marker、研究 Worker、100 人同步突发和每用户五秒一次的错峰稳态请求。
通知不会外发。脚本记录逐请求分位数、研究完成时间、进程 RSS/CPU 和 PostgreSQL
连接峰值；它仍是进程内 ASGI 与快速研究替身，不能替代外部 SSE、真实行情、
完整交易日、当前 24 小时持续窗口、更高等级 72 小时认证、重启或恢复验收。

发布与健康检查故障回归:

```bash
uv run --frozen pytest tests/test_enriched_generation.py tests/test_enriched_full_rebuild.py tests/test_enriched_recovery.py tests/test_data_release.py tests/test_health.py -q
```

覆盖属主死亡后保持不可读、局部股票/ETF 写入不得接管、恢复取消/第二分区写入
失败后的重试、替换后目录 fsync 失败保持不可读、全量历史覆盖检查、
嵌套 manifest 损坏、首次 generation 漂移、
元数据采集及最终写入边界的版本竞态, 以及单项健康检查异常隔离。
新鲜度用人工交易日历验证长假、少一个交易日和盘后宽限; 明确测试日历不完整
时返回 unknown 且不会访问 Provider。日期样本不作为现实交易所休市日历。

<a id="42-前端"></a>
### 4.2 前端

```bash
cd frontend
pnpm test
pnpm lint
pnpm build
```

`pnpm build` 同时执行 TypeScript 项目构建和 Vite 生产构建。只通过单个组件测试不
足以替代 lint 和 build。
设置导航调整须检查“模型设置”和“超时设置”名称、扩展页面位于菜单设置后，
以及展开/收起菜单和原 `tab=ai`、`tab=timeout` 链接仍能打开对应面板。

涉及实时刷新、账户范围或 SSE 的前端回归至少覆盖：60 秒票据到期换票且不调用
整页重载，`events_reset` 只失效指定查询根，账户/租户切换取消并清除旧范围查询，
设置保存只刷新活动查询，以及行情只在交易时段默认最新视图启用 5 秒兜底。分时图回归
必须包含 OHLCV 全空、部分空和非法轴标签值，确认页面显示未知且不抛异常。

<a id="43-仓库检查"></a>
### 4.3 仓库检查

```bash
git diff --check
git status --short
git branch -vv
```

提交前还应检查最终 diff 中是否包含密钥、真实用户数据、绝对路径、调试输出或
无关生成文件。

版本候选还必须记录完整 commit SHA，并校验四个应用版本输入一致：

```bash
python3 - <<'PY'
import json
import pathlib
import re

root = pathlib.Path(".")
frontend = json.loads((root / "frontend/package.json").read_text())["version"]
backend = re.search(r'^version = "([^"]+)"', (root / "backend/pyproject.toml").read_text(), re.M).group(1)
fallback = re.search(r'_FALLBACK_VERSION = "([^"]+)"', (root / "backend/app/__init__.py").read_text()).group(1)
version_file = (root / "VERSION").read_text().strip().removeprefix("v")
assert len({frontend, backend, fallback, version_file}) == 1
print(frontend)
PY
git rev-parse HEAD
```

语义版本一致只证明包元数据一致，不证明源码、标签和生产部署相同；验收报告仍须
记录 commit、schema、产物摘要和环境。

版本规则测试：

```bash
python3 scripts/check_version.py --current VERSION
cd backend
uv run --frozen --no-sync pytest ../tests/tooling tests/test_version_source.py -q
```

本地与 PR 允许版本不变，只校验格式和事实源一致性。`check_version.py --previous`
保留为显式的相邻版本审计工具，要求精确后继，不用于本地或 PR 的日常门禁。
测试覆盖格式范围、进位和上限、全部当前版本文件同步、历史报告及依赖版本保留、
非法输入不写文件，并使用临时 bare Git 仓库验证远程合并后递增、重跑去重、并发
冲突重试、权限拒绝和未推送提交的保护。workflow 测试还验证质量门与镜像构建使用
同一 SHA，以及版本 artifact 缺失或非法时停止构建。
自动标签测试使用临时 bare Git 仓库验证精确提交、主线归属、版本一致性、
重跑和并发幂等、已有 annotated tag 复用、同名冲突不覆盖及权限拒绝。

<a id="5-当前-ci-门禁"></a>
## 5. 当前 CI 门禁

`.github/workflows/ci.yml` 在推送到 `main`、面向 `main` 的 PR 和 merge_group 检查运行：

| Job | 环境 | 命令 |
| --- | --- | --- |
| Version | Ubuntu、Python 3 | PR 校验分支未人工改号；merge_group 只读检查；远程 `main` push 自动递增并输出实际待测 SHA |
| Backend | Ubuntu、Python 3.12 | `uv sync --extra dev --extra cell --frozen`；`uv run pytest tests -q` |
| Frontend | Ubuntu、Node 20、pnpm 9 | `pnpm test`；`pnpm lint`；`pnpm build` |
| Tag | Ubuntu、Python 3 | 仅主线 push，Version/Backend/Frontend 全部成功后运行 `tag_version.py`，创建同一 SHA 的版本标签 |

Backend 和 Frontend 均依赖 Version，检出其输出的 SHA。每个主线 push 保留独立
运行，PR 可取消旧检查。自动版本提交使用 `GITHUB_TOKEN`，不触发递归 CI；
版本写回失败会阻止后续质量门，质量门失败不会撤销已分配的版本。
Tag 失败会使 CI 失败并阻止后续 Docker 构建。重跑复用同一提交的现有标签；
冲突标签保持原样，流程不使用强制推送。自动标签不表示桌面包或生产验收完成。

后端全量测试包含 Tier A OpenAPI 契约门禁，检查：

- 开放端点的方法和路径集合。
- 每个成功响应是否具备 OpenAPI schema。
- 仅包含 Tier A 可达组件的请求、响应 schema SHA-256 指纹。

普通 Tier A JSON 端点必须声明 `response_model`；SSE 必须显式声明
`text/event-stream` schema。响应模型允许透传未冻结的加法字段，但不能向实际
JSON 注入处理函数未返回的缺省字段。快照失败时必须确认兼容性影响，再同步更新
指纹、测试和开放平台文档。

`.github/workflows/docker.yml` 的 `main` 构建只接受主线 push 的成功 CI
`workflow_run`，从该次运行的 `ci-revision` artifact 读取实际通过测试的 SHA，
不使用触发 CI 时尚未加号的 `head_sha`。tag 和手动构建会在 workflow 内重新运行
同等的后端与前端质量门。镜像依赖安装使用 frozen lockfile，锁文件漂移直接失败。
Actions 使用 `GITHUB_TOKEN` 创建 Git 标签不会触发 tag push 构建；
主线成功 CI 的现有构建链路写入提交 SHA 和 `v<应用版本>` 镜像标签。
`latest` 独立串行提升，只有提升时仍为当前 main 的构建 SHA 可更新。

`.github/workflows/release.yml` 的手动桌面发版也有独立质量门：后端执行 frozen
依赖安装和全量 pytest，前端执行 frozen 安装、Vitest、ESLint 与生产构建。只有
版本校验和质量门都通过后，Windows、macOS、Linux 打包矩阵才会启动。Windows
runner 会实际运行 PyInstaller 便携版，并对 Inno Setup 产物执行静默安装、启动、
健康检查、默认 rustdx 插件检查、卸载及用户数据保留验证。
所有 Job 固定到所选既有 tag 的完整 SHA，所选平台全部成功后才能发布冻结版本。
协作回归覆盖旧分支基线、人工改号、版本回退、来源 trailer 伪造、自动提交再触发、
旧 tag 发布、非法平台选择和过期镜像指针提升；规则模板不等于远程已生效。

当前 CI 不自动执行以下检查，发布负责人需按变更风险补充：

- 后端全仓 Ruff。
- 浏览器端到端测试和视觉回归。
- 真实 Provider 网络、额度、覆盖率和 09:25 终态可靠性。
- Docker 多架构镜像运行烟测。
- 桌面 GUI 交互、覆盖升级，以及 Windows 10/11 消费版真机验证。
- 生产规模性能、长时间稳定性、备份恢复和故障演练。

<a id="6-按改动选择测试"></a>
## 6. 按改动选择测试

| 改动 | 最低测试 |
| --- | --- |
| Provider 或能力矩阵 | Provider 契约、字段单位、空数据、缺能力、路由和设置页状态 |
| 日线/分钟/实时数据 | 日期时区、资产路由、分区合并、完整性、分钟空值和缓存失效 |
| enriched/指标 | 固定数值、复权、停牌、历史股本、增量与全量一致性 |
| 策略/信号 | 参数、候选、评分完整性、入场政策、排序、缓存、日线与分钟周期 |
| 回测 | 信号/成交时间、原始/复权价格、T+1、费用、申报数量、滑点、涨跌停、买单过期和未平仓 |
| 监控/通知 | 规则逻辑、逐快照通用规则、策略组快照/共享计算、去重、冷却、重启状态和通知失败隔离 |
| 模拟交易 | 资金持仓守恒、订单状态、T+1、幂等结算、策略组同股裁决、账户预算和并发 |
| API/Token/MCP | 成功、无数据、错误、scope 越权、响应模型、端点快照和 schema 指纹 |
| 邮箱账户认证 | 验证码发送失败、重发冷却、过期、错误次数、一次性消费、注册登录和旧账户迁移 |
| HTTP 观测 | 请求 ID 继承/重生、日志上下文、路由模板聚合、401/403、异常 5xx、认证边界和重置 |
| 前端页面 | 加载、空、错、禁用、切换、窄屏、票据轮换、账户范围和局部刷新恢复 |
| 调度/任务 | 合法状态转换、旧 `pending` 兼容、重复触发、两阶段取消、重启中断、写盘门闸和失败终态 |
| 历史证券状态 | SCD2 区间、同日重跑、名称/ST 变化、退市 tombstone 和首次采集前未知 |
| 文件/迁移 | 原子写、权限失败、旧 schema、路径边界和回滚 |
| 文档 | 相对链接、命令、版本、入口和实际 UI/配置一致性 |

<a id="7-金融正确性必测项"></a>
## 7. 金融正确性必测项

<a id="71-防未来函数"></a>
### 7.1 防未来函数

- 财务和股本按公告可见时间过滤。
- 信号生成只能读取决策时点之前可用的数据。
- 日线和分钟成交分别验证下一可成交时点。
- 样本外验证不能复用验证区间拟合出的方向或参数。
- 历史证券状态从首次成功目录同步开始积累；`instrument_status_known=false` 的
  旧日期仍要说明限制，不能视为严格 Point-in-Time 样本。
- 指定财务观察时点早于首次留档时返回未知，不得回退当前值；原公告时间与采集时间
  分别核对。报告中的未知比例须明确分母，历史退市股票池分母缺失时不能标成 0%。
- 单机回测、优化、步进优化验证完整固定输入：暖机/退出/全部折文件哈希、
  原数据更正后的旧版本重放、运行期修改拒绝、源码/依赖/日历变化、取消及保留容量。
  具体边界见[研究配置](reference/research.md#research)。

<a id="72-单位和价格"></a>
### 7.2 单位和价格

- `change_pct` 在每个边界明确是小数还是百分数值。
- `turnover_rate` 的 Provider 输入与 enriched 输出按各自契约断言。
- 五档数量由“手”换算金额时乘以每手股数。
- 技术指标用复权价；价格限制和可成交判断用原始价。
- 金额、数量、费用和净值的小数精度及舍入规则固定。

<a id="73-交易日和时区"></a>
### 7.3 交易日和时区

- 使用北京时间，不受测试机时区影响。
- 午休、收盘、周末、节假日和跨日重启有覆盖。
- 09:25 竞价窗口、15:35 盘后任务使用业务日期。
- 分钟数据时间戳归一为北京时间墙钟并正确归属资产类型。

<a id="8-provider-真实烟测"></a>
## 8. Provider 真实烟测

真实烟测与自动化契约测试分开，避免外部网络波动阻断普通 PR。发布声明涉及某个
Provider 时，至少记录：

1. Provider 名称、版本和测试日期。
2. 使用的数据能力，不记录密钥。
3. 一个股票、ETF、指数的代表性代码和数据日期。
4. 返回行数、时间范围、必要字段和单位抽查。
5. 空数据、超时、限流和不支持能力的表现。
6. 结果是否落盘，以及重启后能否读取。

rustdx 的专项验证见[rustdx 数据源说明](reference/integrations.md#rustdx)。

<a id="9-人工端到端验收"></a>
## 9. 人工端到端验收

<a id="91-基础启动"></a>
### 9.1 基础启动

- `GET /health` 返回 `status=ok` 和预期版本。
- 邮箱注册、登录、退出、改密和错误凭据行为正确。
- 旧版访问密码可绑定邮箱完成迁移，原有业务数据保持不变。
- 刷新页面后会话和主要持久化状态符合预期。
- 数据目录挂载到预期位置，重建容器后数据仍在。

<a id="92-数据闭环"></a>
### 9.2 数据闭环

- 设置页重新检测后，能力矩阵的候选源、生效源和可用状态正确。
- 运行盘后管道，任务依次结束且数据页范围更新。
- 选股、回测和监控读取相同日期和数据口径。
- 人为制造 Provider 失败时，旧数据不被空结果覆盖。

<a id="93-研究闭环"></a>
### 9.3 研究闭环

- 运行一个日线策略并查看候选。
- 用同一策略运行短区间回测，检查原始入场价格/数量、费用、未平仓和拒单原因。
- 编辑可选入场风险后重新打开，确认溢价、ATR/低点和计划风险回显一致；切换策略
  不继承上一策略配置。
- 导出结果、保存候选并载入复测。
- 创建两条策略监控规则，验证成功周期共享结果且通用价格规则仍逐快照触发。
- 建立模拟账户，完成一笔可成交和一笔应拒绝订单；同周期两策略命中同股只产生
  一单，买卖冲突只推进退出。

<a id="94-开放闭环"></a>
### 9.4 开放闭环

- 创建最小 scope Token。
- 验证允许接口成功、越权接口拒绝。
- 验证 SSE 建票、连接和断线重连。
- 保持页面超过 60 秒，确认票据轮换后页面不整页刷新，游标连续且无关查询不重拉。
- 使用至少一个 MCP 查询工具；动作工具必须出现确认步骤。

<a id="95-页面与兼容"></a>
### 9.5 页面与兼容

- 常用桌面宽度和窄屏下检查导航、表格、弹窗和滚动。
- 检查加载、空数据、错误、无权限和任务运行中状态。
- 在行情、自选、策略、回测、因子和设置页分别等待实时更新，确认只更新受影响区域。
- 切换账户或目标租户，确认旧范围请求被取消、旧数据不闪现且应用内导航状态正常。
- 深色/浅色主题受影响页面无不可读内容。
- 升级场景使用一份脱敏旧版本数据副本验证读取和迁移。

<a id="10-发布验收矩阵"></a>
## 10. 发布验收矩阵

| 产物 | 必测项 |
| --- | --- |
| GHCR `linux/amd64` | 拉取、启动、健康检查、数据卷、重启 |
| GHCR `linux/arm64` | 拉取或构建、启动、健康检查、数据卷 |
| Windows x64 | CI 自动安装、启动、健康检查、数据目录、卸载后数据策略；人工 GUI 与覆盖升级 |
| macOS ARM64 | DMG 打开、拖入 Applications、首次启动、数据目录 |
| Linux x64 桌面包 | 解压、启动、健康检查、数据目录 |

没有实际构建的平台不能在发布说明中标记为已验证。Release 的 `latest.json` 只列
出实际存在的产物，也应检查版本、URL、大小和 SHA-256。

构建/升级清单回归：

```bash
uv run --project backend --no-sync pytest backend/tests/test_build_manifest.py \
  backend/tests/test_upgrade_check.py backend/tests/test_desktop_packaging.py \
  tests/tooling/test_version_workflows.py -q
```

覆盖文件增删改、内部/外部链接、容量限制、备份期间变更、真实 Git 的暂存/
未暂存/二进制差异、提交身份与上传前核验。实际平台仍须运行
`build_manifest.py verify`，检查 Docker `/app/build-manifest.json` 或安装包
`.manifest.json`。离线工具测试不代替完整镜像、安装包和升级后启动验收；
恢复步骤见[构建清单与离线升级备份](reference/engineering.md#build)。

<a id="11-缺陷严重级别与放行"></a>
## 11. 缺陷严重级别与放行

| 级别 | 示例 | 放行规则 |
| --- | --- | --- |
| `P0` | 数据破坏、密钥泄露、严重错误交易结果 | 禁止发布 |
| `P1` | 核心流程不可用、未来函数、广泛回归 | 禁止发布 |
| `P2` | 明确功能错误、兼容性或明显性能问题 | 修复后发布，或明确缩小发布范围 |
| `P3` | 局部体验、维护性或文档问题 | 可记录后发布，但必须有负责人和处理结论 |

发布不能只依据“CI 绿色”。若真实数据、平台安装或恢复路径是本次变更的一部分，
对应人工验收也是门禁。

<a id="12-测试记录模板"></a>
## 12. 证据与签收记录

所有自动化、真实源、平台与生产验证使用下方统一模板；原始证据必须能定位到提交、环境、时间窗口和实际失败。

<a id="acceptance-1-验收单位与前置输入"></a>
### 1. 验收单位与前置输入

每份记录绑定：完整源码SHA或dirty清单、应用版本、产物digest、schema、配置摘要、
部署模式/平台/架构、数据版本/范围/许可、目标阶段与负载、开始/结束时间及负责人。
确定预期成功、业务拒绝、限流、降级、超时和失败的分母，原始失败样本必须保留。
回放、mock、进程内ASGI、真实Provider、真实网络、真机和生产证据分别记录。

当前S0验收不能自动签收S1–S3或二期第三步。20页面小档、历史百账号、500SSE、
十年日线或隔离SQL夹具都不能外推万人、全市场1Hz、十年秒级、跨节点HA或区域灾备。
目录/文档检查只验工程一致性。

<a id="acceptance-5-证据缺陷与签收模板"></a>
### 5. 证据、缺陷与签收模板

```text
记录ID / 需求ID / 包 / 工作编号:
目标 / 阶段 / 判定阈值:
完整SHA或dirty清单 / 应用版本 / digest / schema:
模式 / OS / 架构 / 机器 / 依赖锁 / 配额:
数据版本 / 名单 / 精度 / 窗口 / 来源可信度:
负载比例 / 到达曲线 / 实际执行量:
开始 / 结束时间（含时区）:
命令 / 退出码 / 原始证据 / 哈希:
预期 / 实际 / 失败及影响范围:
通过 / 未通过 / 未执行 / 不适用（附原因）:
增量水位 / 备份 / 恢复对账 / RPO / RTO:
缺陷严重级别 / 负责人 / 关闭证据:
签收范围 / 剩余风险 / 发布结论:
```

只在实现与对应环境证据齐备后更新状态，签收人不能用历史计数填本候选记录。
生产放行继续遵循[发布流程](release-operations.md)；此模板不自动安排压测或切流。

<a id="acceptance"></a>
<a id="13-秒级行情与规模化研究的目标验收"></a>
## 13. 功能、规模化研究与生产联合验收

以下为验收标准，不是通过记录。S0现有回归与S1–S3目标分别签收，二期3.1–3.6与万人DFX使用同一联合矩阵。

<a id="acceptance-2-功能与数据验收包"></a>
### 2. 功能与数据验收包

| 包 | 需求范围 | 正常 / 边界 / 故障证据 | 放行结果 |
| --- | --- | --- | --- |
| A 接入与金融口径 | DATA/EXT、跨模块规则 | 八路由、缺能力/空值/单位、原价与复权、日期/时区、来源/证券分母、分页截断/修订 | 合格数据才写/发布；失败不空覆盖或跨源 |
| B 行情工作台 | REALTIME/RESEARCH/STATS/DASHBOARD/WATCHLIST/UI | 自选导入先确认、分组M:N、布局持久化、详情/身份切换、最新与历史、预热/陈旧、同版统计 | 保留有效值/未知与质量；旧身份结果不得进入新空间 |
| C 监控与模拟 | MONITOR/PAPER/CORP/SIGNAL/LOTS/REVIEW | 逐bar/穿越/缺口、周期仲裁、资金/可卖量/待单、T+1/费用、登记/应收/到账/上市/缴款、通知错误/重试 | 一次事实、虚拟账务可勾稽，未知权益阻断；提醒不改账本 |
| D 历史发布与引用 | HISTORY、NFR-22/26/28/29 | 重复/半导入、缺副本/分片、跨表屏障、修订旧快照、活动引用、冷热恢复 | 失败不开放目标版本，不删在用输入 |
| E 分层筛选 | SCREEN、FACTOR-04、NFR-24/27 | 基准全明细对照、不同秒最值、日终不可用于盘中、未知边界、预热/退出、冷热与完整候选 | 优化不改变因果或误排，截断与续取显式 |
| F 研究与回测 | BACKTEST/STRATEGY/FACTOR/MINING | 参数/公式版本、草稿、晋级门槛、固定输入、训练/测试、信号/成交、跨块资金/权益、取消/重复、全部终态 | 同输入一致、无未来函数、完整性与统计可核对 |
| G 身份与开放 | ENTRY/OPEN/AI/ADMIN/AUTH-LOCAL | 模式/角色/初始化失败、直接越权、切空间迟到、Token撤销、票据/游标、动作确认/断流、会话冲突 | 授权和范围正确，不恢复未确认动作 |
| H 运行与发布 | NFR-01–17/20/21/28 | 锁/版本/产物清单、各目标平台、磁盘/inode、预算、存活/就绪、停写备份、恢复/增量对账 | 固定产物匹配代码/数据，声明适用平台与剩余风险 |
| I 万人与DFX | NFR-31–42及A–H | 独立活跃业务、后台/数据/研究混合压力、最大故障域、跨节点撤权/单写/区域恢复/成本 | 逐项目标签收，未执行不算通过 |

测试至少覆盖沪深股票、ETF、固定指数各自适用规则；管理员、普通租户、独立服务、
Windows管理员个人空间和macOS/Linux冻结桌面分别执行适用入口。
性能成功分位数与全部尝试/超时单列，P0/P1不放行；P2修复或显式缩小范围并重新验收。

<a id="acceptance-3-二期第三步3136联合矩阵"></a>
### 3. 二期第三步3.1–3.6联合矩阵

| 工作包 | 必测失败点 | 正确性出口 |
| --- | --- | --- |
| 3.1 观测/上线治理 | 热发布/归档/研究同时慢、半发布、灰度超门槛、旧新schema共存 | request/task/capture/snapshot/Cell可关联，停止推广/排空保留新写 |
| 3.2 多节点可靠性 | Gateway/API/身份/Worker退出，PG仲裁/分区、旧主恢复，Kafka/CH/Redis/KMS故障 | 确认水位、旧主拒绝、可读副本完整、最大故障域后仍有容量 |
| 3.3 安全/强Compute | 广播中断、旧票据/旧权限、敏感写撤权竞态、跨租户对象、元数据出口、进程逃逸/超限 | 常规≤60秒收敛；敏感提交权限可判定；无网络凭据，进程树退出/配额回收 |
| 3.4 跨节点状态任务 | 消息丢/重、SQL提交后ACK丢、租约失效/取消、旧attempt输出、对象存在但未发布 | SQL事实/幂等、可补扫；输入/续租/manifest/CAS/结果边界拒旧；不双裁决 |
| 3.5 Cell迁移 | PREPARE/DRAIN/FENCED/CAS/ENABLE各状态中断、旧源重启、目标新写后回退 | 源fencing→目录CAS→目标enable；最多单写、epoch递增、增量与密钥/产物对账 |
| 3.6 万人/恢复/成本 | 登录/重连/轻查询洪峰、慢连接、高订阅、长台账、后台/冷恢复、最大故障域/区域失效 | 独立身份实际业务量、完整错误、RPO/RTO、水位/账本/任务/对象/密钥与成本 |

<a id="acceptance-4-负载持续窗口与恢复目标"></a>
### 4. 负载、持续窗口与恢复目标

在线试点：6,000证券/秒规划、20并发页面每页≤200可见证券和一个受限研究任务为
待固定的小档假设。按PRD分别测采集/完成间隔/可读/指标/统计/归档，不等同源1秒新鲜度。
研究分别量测完整筛选、明细复核、组合回测和全部参数终态；记录扫描/解压/计算/输出、
冷热缓存、CPU/RSS/暂存、重试及实际成本。

万人按100→1,000→3,000→10,000活跃身份渐增，初始建议15,000连接、
2,000RPS稳态/5,000突发、万人同时轻查询全部≤30秒；
事先固定请求比例、突发曲线/时长、规则复杂度、长台账和后台配额。
轻API目标P95≤300ms、P99≤1秒、有效5xx/超时<0.1%，拒绝/排队/降级单列。
同一负载复测行情、历史扫描、归档和研究隔离，不能万人各空挂一个连接。

| 范围 | 目标性质 | 必须补齐的证据 |
| --- | --- | --- |
| 单机阶段 | RPO≤24小时、RTO≤2小时为既有建议 | 固定单机备份、隔离恢复与身份/业务/对象一致 |
| 第三步核心 | 月可用性99.95%、节点/AZ RTO≤5分钟为设计目标 | 核心功能定义/错误预算、故障域退出与容量/实际恢复 |
| 关键账本 | 条件RPO=0 | 同步落盘确认、提升水位、旧主隔离；失同步不得静默降异步 |
| 区域灾备 | RPO≤5分钟、RTO≤60分钟为设计目标 | 身份/目录/账本/任务/对象版本/密钥联合恢复和对账 |

当前没有已签收的上述SLA保证。固定24小时混合负载与完整交易日分别记录，
涵盖竞价/开收盘/结算/备份/后台；72小时单独认证，月度可用性不能由24小时证明。

<a id="132-复用现有定向测试"></a>
### 13.2 复用现有定向测试

以下测试是基础回归入口；通过这些现有测试不能签收新建集群、十年秒级容量或
全市场真实 1Hz。实现发生后还须补齐新路径及故障用例。

```bash
cd backend
uv run --frozen pytest -q \
  tests/test_quote_poll_cadence.py tests/test_quote_interval_min.py \
  tests/test_second_quote_archive.py tests/test_second_quotes_api.py \
  tests/test_second_acceptance_observer.py tests/test_research_snapshot.py \
  tests/test_strategy_group_coordinator.py \
  tests/backtest/test_second_quote_replay.py \
  tests/backtest/test_second_execution.py \
  tests/backtest/test_walkforward_boundaries.py
```

未来的 ClickHouse reader 与现有 Parquet reader 必须对同一固定样本核对
价量、NULL、时间、版本、候选及回测结果；独立 SQL 引擎的小夹具验证只能证明
其实际覆盖的 SQL 行为，不能替代 Keeper、复制、跨 shard、发布屏障或性能验收。

<a id="133-真实数据与负载记录"></a>
### 13.3 真实数据与负载记录

在线验收至少固定：证券名单及日期、Provider 版本和能力、规则／指标依赖、
页面与用户并发、受限研究任务、硬件与资源预算。分别记录成功请求分位数、
全部尝试与失败／超时、实际完成轮次间隔、源端时效证据、每证券质量、
归档丢轮／积压、热发布与页面读取、进程内存及磁盘增长。

采集基准可复用 `backend/scripts/performance/benchmark_rustdx_quotes.py`，
只读秒级观测可复用 `backend/scripts/acceptance/observe_second_acceptance.py`；
实际启动或采集按会话授权和独立运行目录执行。本节不安排后台任务。

研究性能分别测日频十年筛选、分钟候选／秒级复核、单次组合回测、参数批量研究
和必要的全范围扫描。时限与并发先按需求基线待决项固定，再量测读取、解压、
计算、完整输出、缓存、峰值资源与成本。不能把 LIMIT 100 的首屏响应时间当作
完整研究完成时间，不能用合成数据压缩率预算真实十年容量。

至少覆盖：原数据重复／回退、源日期推断、缺退市池、历史状态／财务修订未知、
一分钟内不同秒分别达到最大值、源缺口后的穿越、跨交易日／午休、
新旧快照共存、未完成分钟、输入或权限变化、节点丢失及研究任务强制中断。

<a id="134-签收边界"></a>
### 13.4 签收边界

报告明确标注源码／产物、数据版本、阶段、目标平台与实际执行范围。
1 秒调度、页面刷新、源端 1 秒新鲜度与严格秒级回放合格分别判定；
分位数达标不自动放行不合格历史窗口。

已有百用户／SSE 或日线历史回填记录不覆盖新目标；小负载通过不推断万人容量。
只有实现、正确性、真实负载和适用恢复证据齐备的阶段，才更新需求状态为
已实现或签收，并同步当前架构、功能／操作、配置和运维文档。

<a id="14-文档一致性复核"></a>
## 14. 文档一致性复核

文档更新先核对[当前实现索引](architecture.md#capabilities)的页面、配置、能力、
API 及测试事实源，再检查：

1. 当前使用说明中的本地文件链接、标题锚点、脚本路径和模块名有效。
2. 示例端口与所用部署模式一致，普通 REST 不误指向独立 Gateway。
3. 环境变量存在读取实现；持久偏好与启动环境的优先级分开说明。
4. API 路径存在且所属会话/Token 权限明确，不能把管理接口写成 Token 示例。
5. 当前功能、设计目标、历史测试和最后已知生产记录分别带范围与日期。
6. 主应用版本事实源一致，原需求编号不因文档整理而丢失。

`make check`覆盖目录、文档本地链接/锚点/围栏、八模块/需求编号、版本和whitespace；不访问外部网站或替代行为验收。
本轮链接及源码引用检查结果保存在
[文档同步记录](../reports/documentation-sync-20261023/verification.md)；
历史报告保留当时命令和目录，当前 Runbook 使用新路径。

2026-10-24按当前主线/目标清理后，保留证据以[报告索引](../reports/README.md)导航，
适用门禁结果见[清理验证](../reports/repository-cleanup-20261024/verification.md)。
可额外执行`python scripts/check_docs.py --include-reports`检查历史报告的导航。
已缺失的历史临时日志保留原路径并注明不可用，不保留失效下载链接；
缺少版本/环境元数据的旧截图不作为当前验收证据。

<a id="rustdx-benchmark"></a>

<a id="benchmark-rustdx-行情连接数压测"></a>
## 15. rustdx连接数基准方法

脚本 [`backend/scripts/performance/benchmark_rustdx_quotes.py`](../backend/scripts/performance/benchmark_rustdx_quotes.py)
对同一份本地股票池逐档测量 8–30 条持久连接的全量行情耗时。默认每个整数档位均测试，
每档单独创建进程与连接池，关闭后再运行下一档。

<a id="benchmark-运行"></a>
### 运行

在已安装 rustdx 原生扩展的源码环境中，从项目根目录执行：

```bash
cd backend

# 参数和股票池预检，不连接服务器、不写结果
uv run --no-sync python scripts/performance/benchmark_rustdx_quotes.py --dry-run

# 23 个档位；每档冷启动 1 轮、预热 5 轮、正式采样 100 轮
uv run --no-sync python scripts/performance/benchmark_rustdx_quotes.py --connections 8-30

# 以 1000ms 为 P95 验收上限
uv run --no-sync python scripts/performance/benchmark_rustdx_quotes.py --connections 8-30 --max-p95-ms 1000

# 先做少量真实网络冒烟验证，不作为 P95 验收
uv run --no-sync python scripts/performance/benchmark_rustdx_quotes.py --connections 8,30 --rounds 3 --warmup 1
```

默认轮次开始间隔为 1 秒，完整运行约 41 分钟起；慢轮次会增加总时间。上一轮完成后
才开始下一轮，绝不重叠。`--interval 0` 用于连续压测；其他档位可写为
`--connections 8,12,16,20,24,30`。需要更多尾部样本时设置 `--rounds 300`。

输入固定为 `data/instruments/instruments.parquet`，使用现有的股票身份与退市过滤规则，
保留有效沪深主板、科创板、创业板股票，包含 ST 和停牌股票；排除指数、ETF、B 股和
不支持的市场。`--data-dir` 指定另一个数据目录。股票池在父进程一次性读取并去重，
所有档位传入相同列表；不会在压测中重新请求证券目录。

`--server IP:PORT` 固定全部档位的行情节点，默认读取当前 `RUSTDX_SERVER`，未配置时
使用内置节点。`--socket-timeout` 默认为 8 秒，`--round-timeout` 默认为整轮 60 秒；
整轮超时后终止该档进程、保留已完成样本，再继续下一档。Ctrl+C 会回收当前进程并保存结果。
建议在相同机器、节点、股票池和时段下比较；同时运行的其他采集进程会影响节点负载。

脚本仅发起独立行情请求并写压测报告，不启动后端服务、调度器或修改用户采集配置，
也不落盘行情、股票池和历史数据。源码 CLI 可在 macOS、Linux、Windows 上运行，
各平台需安装对应的 rustdx 原生扩展。

<a id="benchmark-测量口径"></a>
### 测量口径

测量 `RustdxProvider.get_auction_snapshot(固定股票列表)` 调用。这与实时行情使用相同的
原生分批采集与字段标准化路径，额外保留盘口和单票事件时间。耗时包含网络、冷启动时
的建连、原生返回、JSON 解码和 Provider 标准化；不包含目录同步、股票名称查询、
覆盖率检查、磁盘写入、SSE、前端及策略计算。冷启动和预热不计入正式分位数。

正式轮次要求全部股票唯一返回、价格非空且有限，且无多余记录。停牌或尚未成交股票
允许零价。缺股票、重复股票、无效价格、调用异常都不能算成功：

- `p50_ms`、`p95_ms`、`p99_ms`：仅对完整成功的正式轮次计算，使用 nearest-rank，
  即排序后取第 `ceil(样本数 × 分位比例)` 个值，不做插值。
- `all_attempts_p95_ms`：包含失败正式轮次的耗时。超时样本只有耗时下界，
  因而 `all_attempts_censored=true` 时不能将这个数当作完整尾部耗时。
- `success_rate`：成功正式轮次 / 请求的正式轮数；进程中断而未尝试的轮数单独记录。
- `min_coverage` 与 `min_valid_coverage`：分别为返回的唯一股票覆盖率、有效价格覆盖率。
- `observed_connections_min/max`：实际存在的持久连接数量；同时保存每轮原生池统计和
  `connection_config_matches`，区分配置上限与实际建连数。
- `snapshot_capacity_per_second`：`1000 / mean_ms`，仅是调用速度换算值，
  不是指定采样间隔下的实际发布频率。

少于 100 个成功正式样本时 `sample_sufficient=false`。100 个样本只是本脚本的最低
验收样本数，不能代替交易高峰和多次重复测试。指定 `--max-p95-ms` 时，只有每档
成功完成全部正式轮次、连接配置一致、至少 100 个成功样本且 P95 不超过上限才通过。

`source_time_values` 保留源端单票事件时间。该字段不代表批次时间，也不用于新鲜度
判定。盘后测得的耗时可用于检查连接复用效率；交易时段 P95 需要在对应时段重新采样。

<a id="benchmark-结果"></a>
### 结果

默认输出到新的 `data/research/rustdx-quotes-<北京时间>/` 目录：

| 文件 | 内容 |
| --- | --- |
| `summary.csv` | 每档冷启动、P50/P95/P99、覆盖率、失败率和连接统计 |
| `results.json` | 环境、固定股票池哈希和日期、参数、逐档结果、逐轮原始数据 |
| `samples.jsonl` | 含冷启动、预热、正式轮次及失败信息的逐轮记录 |

使用 `--output <新目录>` 可更改路径，已有目录会被拒绝。每完成一档即保存一次；
Ctrl+C 保留已经接收的当前档样本。数据报告应保留在忽略目录中，不提交到 Git。

退出码：`0` 表示全部档位完成且无失败；指定上限时还须通过验收。`1` 表示失败、
整轮超时、连接配置不符或验收未通过；`2` 表示参数/输入错误；`130` 表示用户中断。

<a id="benchmark-离线验证"></a>
### 离线验证

```bash
cd backend
uv run --no-sync pytest tests/test_rustdx_quote_benchmark.py tests/test_rustdx_provider.py -q
uv run --no-sync ruff check scripts/performance/benchmark_rustdx_quotes.py tests/test_rustdx_quote_benchmark.py
```

离线测试覆盖股票池过滤、分位数算法、失败与超时样本、连接数进程隔离、资源释放、
报告内容和预检不访问网络。

2026-10-10 盘后在 macOS arm64、固定节点 `117.34.114.13:7709` 完成真实冒烟，
股票池 5,226 只，8 和 30 连接各 3 个正式样本均完整成功，实际连接数与配置一致。
8 连接三轮耗时为 925.8 / 1126.0 / 935.3ms，30 连接为 411.9 / 599.9 / 445.7ms。
这是脚本连通性及报告输出验证，样本量不足以得出正式 P95 结论；尚未执行全部档位
各 100 轮的正式压测。
