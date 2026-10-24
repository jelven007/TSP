# 2026-10-23 当前功能文档同步与验证

任务：基于当前项目功能全面更新相关文档。
核对源码为 `cc837b95f471e5b299bf5e8618332d500bff7e3a`，工作分支为
`docs/requirements-realtime-research-20261023`；应用版本保持 `0.4.17`，
MCP 独立包保持 `0.3.2`。此前需求与目录迁移仍在工作区，本轮未提交或推送。

入口：[文档中心](../../docs/README.md)、
[当前功能与实现索引](../../docs/architecture.md#capabilities)、
[操作说明书](../../操作说明书.md#tsp-a-股智能量化工作台操作说明书)。

## 1. 同步范围

本轮修改 26 份现有 Markdown 文档及 `.env.example`，新增当前功能索引和本报告。
未改业务实现、测试、依赖锁或应用版本；保留已有工程目录变更、历史报告、
原方案依据与全部 124 个需求编号。

| 领域 | 本轮更新 |
| --- | --- |
| 产品与入口 | `README.md`、`docs/README.md`、`docs/features.md`、`docs/requirements.md`、`操作说明书.md` |
| 架构与状态 | `docs/architecture.md`、`docs/version-baseline.md`、`docs/technical-validation.md` |
| 配置与部署 | `.env.example`、`docs/configuration.md`、`docs/deployment.md` |
| 运维与放行 | `docs/operations.md`、`docs/testing.md`、`docs/release-operations.md` |
| 数据源与开发 | `CONTRIBUTING.md`、`docs/secondary-development.md`、`docs/plugin-development.md`、`docs/custom-data-source.md`、`docs/rustdx-data-source.md` |
| 因子与研究 | `docs/factor-platform-plan.md`、`docs/factor-system-design.md`、`docs/mining.md`、`docs/strategy-iteration.md`、`docs/second-level-backtest-upgrade.md` |
| 外部接入 | `docs/open-platform-plan.md`、`mcp-server/README.md`、`examples/open-api/README.md` |

其余公司行为、AI 协议、会话同步、扩展配方、策略、市场阶段和部署专项作为领域
契约核对与链接目标；无需改写已经正确的内容。带日期的生产/测试记录不覆盖为当前结论。

## 2. 主要修正与依据

| 原文档偏差 | 同步后的事实 | 实现依据 |
| --- | --- | --- |
| 多份文档写七项能力 | 八项独立路由，新增公司行为；HTTP YAML 支持除五档外七项 | [能力注册表](../../backend/app/data_providers/capabilities.py) |
| 模拟盘现金分红不入账 | 新账户事件账务；旧账户保留因子模式；应收/到账/上市分开，日期不足暂停 | [公司行为契约](../../docs/reference/trading-ai.md#corporate)、[API](../../backend/app/api/paper.py) |
| 因子设计总声明尚未实现 | 注册表、DSL、组合、五 tab 已有；巡检/中性化等仍是目标 | [注册表](../../backend/app/factors/registry.py)、[因子页](../../frontend/src/pages/Factors.tsx) |
| 有界 AI 迭代遗漏 | 创建窗口最多四轮，独立草稿与显式保存；完整工作台未实现 | [迭代器](../../backend/app/strategy/ai_iterator.py)、[创建窗口](../../frontend/src/components/screener/StrategyBuilderDialog.tsx) |
| 所有模式描述为单 API worker / 全部数据本地 | 独立单主进程；完整多租户双库、Platform/Gateway/四 Worker，API 默认四进程 | [主入口](../../backend/app/main.py)、[Compose](../../deploy/full/compose.yml) |
| 16 GiB 预算被当作默认四进程 | 该配置明确覆盖 API 两进程、单 Compute，与默认资源配置分开 | [production-16g.env](../../deploy/full/production-16g.env) |
| 配置优先级冲突 | 启动环境高于 dotenv；有效页面密钥/AI保存值高于 Settings 后备；偏好独立 | [Settings](../../backend/app/config.py)、[secrets_store](../../backend/app/secrets_store.py) |
| 每日 AI Token 预算声称限额 | 旧变量没有读取与累计限额；模板使用真实输出/上下文/轮次配置 | [AI 配置](../../backend/app/config.py) |
| 默认全量分钟按每秒运行解释 | 偏好 1 秒；rustdx 生效周期至少 60 秒，不能推断源端 1 Hz | [rustdx Provider](../../backend/app/plugins/rustdx/provider.py) |
| 秒级接口示例允许普通 Token | 设置和秒级研究不属于 Tier A，使用有权面板会话 | [开放规则](../../backend/app/services/api_gateway.py) |
| 旧端口、EventStream、设置 tab 和生产 SHA | 示例端口 3018、EventSource、九个设置 tab；生产值明确最后已知记录 | [设置页](../../frontend/src/pages/Settings.tsx)、[版本基线](../../docs/version-baseline.md) |
| 原挖掘测试数写成当前结果 / 财务门控当严格 PIT | 保留历史日期；原公告及修订可信度独立于公告字段和固定输入 | [研究配置](../../docs/reference/research.md#research) |

当前 ClickHouse、对象存储、Ray、Kafka/Flink 没有接入应用；S1–S3 为后续建设。
十年日线回填不认证十年分钟/秒级、退市池或历史财务可得性。

## 3. 实际验证

后端使用临时隔离 CPython 3.12.15 / Polars 1.44.2 环境，包含所需测试依赖，
不是本轮通过 `uv sync --frozen` 构建的完整原生安装环境。用例使用临时目录与夹具，
未启动应用服务、连接真实 Provider、迁移数据库或操作生产。

| 检查 | 实际结果 |
| --- | --- |
| 核心契约：能力、公司行为、因子/DSL、开放面、AI 迭代、会话、秒级 API | **112 passed**，19.04 秒 |
| 扩展快照/分页/因子、Windows 管理员、资源预算、release、平台与 rustdx | **150 passed, 1 skipped, 1 warning**，33.07 秒 |
| 文档版本、CI 自动版本同步与工作流 | **57 passed**，12.10 秒 |
| 本轮合计 | **319 passed, 1 skipped**；三批文件不重复 |
| 目录、版本与 whitespace | `make check` 通过：68 个入口；版本 `0.4.17` 一致；`git diff --check` 通过 |
| 源码元数据核对 | 19 个基础菜单、9 个设置 tab、5 个因子 tab、8 项能力、77 个内置因子、25 个 DSL 算子、69 项 Tier A、12 个 MCP 工具 |
| 配置模板 | 24 个 Settings 环境字段均有定义；剔除无读取的旧每日预算；八个能力偏好均有 getter |
| 需求编号 | 全部 124 个编号唯一，顺序和集合与本轮开始前一致 |
| 本地链接/锚点 | 63 份当前文档的 609 个本地链接、55 个 Markdown 标题锚点全部有效 |

唯一跳过为 `test_native_bridge_clamps_direct_constructor_to_35`：
隔离环境没有 `tsp_rustdx_native`，通过 `pytest.importorskip` 跳过，
不能计为原生桥接测试通过。唯一警告为既有 `watchlist.py` 的 `datetime.utcnow()` 弃用提示。

执行文件组（在对应目录使用隔离解释器 `python -m pytest ... -q -p no:cacheprovider`）：

```text
backend:
tests/test_capability_matrix.py tests/test_corporate_action_provider.py
tests/test_paper_corporate_actions.py tests/test_factor_registry.py
tests/test_factor_dsl.py tests/test_openapi_contract.py tests/test_ai_iterate.py
tests/test_assistant_sessions.py tests/test_second_quotes_api.py

backend:
tests/test_ext_pull_complete.py tests/test_ext_pull_pagination.py
tests/test_ext_factors.py tests/test_local_admin.py tests/test_resource_budget.py
tests/test_data_release.py tests/test_full_platform.py tests/test_rustdx_provider.py

root:
tests/tooling/test_versioning.py tests/tooling/test_advance_version.py
tests/tooling/test_version_workflows.py
```

链接检查只读扫描当前 root/docs/deploy/示例/开发入口文档，跳过代码围栏中的演示内容，
核对本地文件与 Markdown 标题锚点；源码核对检查数量、偏好 getter、模板字段与需求编号。
校验器及开始前文档副本位于临时工作目录，未纳入应用源码。

## 4. 未执行范围

本轮没有业务代码、前端组件或样式变化，未执行前端全量构建或浏览器页面验收；
未执行后端全量、真实 PostgreSQL/Compute/原生桥接、Win10 真机、付费模型、
开盘 1 Hz、十年历史容量或远程 CI/生产验证。
上述范围仍按[测试指南](../../docs/testing.md)和各阶段验收独立进行。
生产连接池超时、Windows 真机与大型研究目标保持已有未签收状态。

本轮交付仅同步文档与配置模板，不改变上述验证/部署结论。
