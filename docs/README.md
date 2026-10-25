# TSP 文档中心

应用语义版本为 `0.4.23`。内容核对日期：2026-10-25。
源码、标签、安装包和最后已知生产状态统一见[版本基线](version-baseline.md)。
文档覆盖、代码实现、测试通过、产物发布和生产切流分别判定。

本批最新本地改动、文档责任、验证与发布状态见
[最新整合记录](../reports/current-state-release-20261025/verification.md)；
页面/菜单/上游分析统一归[分析索引](../reports/page-data-analysis-20261024/README.md)。

## 六类主手册

| 工作 | 唯一主入口 | 内容与完成依据 |
| --- | --- | --- |
| 需求 | [需求基线](requirements.md) | 用户、角色、对象、八业务章节、163条FR与42条NFR、阶段和验收条件 |
| 方案 | [系统技术方案](architecture.md) | 当前运行架构、页面/能力/源码事实源、统一系统目标、DFX与容量设计 |
| 开发 | [CONTRIBUTING](../CONTRIBUTING.md) | 调用链、数据口径、缓存、插件、质量门、版本、PR与复审 |
| 测试 | [测试与联合签收](testing.md) | 环境/命令、领域回归、产物平台、A–I、3.1–3.6、负载/恢复、统一证据模板 |
| 运营 | [发布与服务运营](release-operations.md) | 发布角色/产物/放行、开通、公告、质量成本、支持与反馈 |
| 运维 | [部署与运维](operations.md) | 模式安装、配置优先级、账户/Windows、健康、调度、备份、恢复、故障与交接 |

用户日常操作统一见[操作说明书](../操作说明书.md)。
代码二次开发仍必须阅读[扩展与上游兼容指南](secondary-development.md)；
真实执行状态只在[二期工作包](planning/phase2-implementation-plan.md)维护。
一期实施和旧验证已归入[历史依据](../reports/document-consolidation-20261024/historical-evidence.md)。

## 专题参考

| 专题 | 单一维护文件 | 保留的细节 |
| --- | --- | --- |
| 工程交付 | [工程参考](reference/engineering.md) | 实际目录、依赖方向、构建清单、离线升级、协作权限、生命周期追踪与决策 |
| 行情质量 | [行情与扩展数据](reference/market-data.md) | 收盘保护、五档证据、市场阶段、完整拉取/恢复、秒级归档与执行验证 |
| 研究 | [策略与研究](reference/research.md) | 策略参数/交易规则、固定输入、试验族、样本外、挖掘与AI迭代 |
| 因子 | [因子体系](reference/factor-system.md) | 当前五视图与生命周期、注册/DSL/统计/组合、明确标记的后续规格 |
| 交易与AI | [账务与AI协议](reference/trading-ai.md) | 模拟撮合、公司行为/应收/权益、完整工具响应、会话revision与恢复 |
| 接入 | [数据源与开放接口](reference/integrations.md) | HTTP/YAML、Provider、rustdx、TickFlow预算/探测、Token/Tier A与网关 |

实际部署文件和模式步骤见[deploy入口](../deploy/README.md)；
脚本命令见[仓库脚本](../scripts/README.md)与[后端脚本](../backend/scripts/README.md)。
可运行样例见[HTTP数据源](../examples/custom-data-source/README.md)、
[Open API](../examples/open-api/README.md)与[MCP](../mcp-server/README.md)。

<a id="maintenance"></a>
## 事实源与维护规则

当前行为先核对源码、运行配置和相邻契约测试；字段/单位/空值遵循CONTRIBUTING。
目标只在需求和方案声明，验收只引用相同版本、环境和范围的实际记录；
生产状态只引用固定提交、digest、schema与切流证据。历史数字不能填入新验收记录。

每项变更按“需求/阶段→方案→文件→测试→用户/运营/运维影响→实际证据”同步。
同一规则只在上表的唯一归属定义，其他文档链接到具体章节。
专题附录保留字段、代码样例、缓存键和失败契约；用户手册不把目标写为可用功能。
删除/移动正文先保留需求编号与独有约束，再同步所有文件链接、锚点和代码内入口。

运行 `make check` 及受影响测试；历史导航可执行
`python scripts/check_docs.py --include-reports`。检查不联网，也不替代业务或生产验收。
源码与运行数据、凭据、构建输出、冻结交付分开维护，历史证据见[报告索引](../reports/README.md)。
本轮合并范围、迁移映射与实际验证见[合并记录](../reports/document-consolidation-20261024/verification.md)。

文档由表中领域负责人和变更作者共同维护，每次行为、接口、拓扑、配置或部署变更
立即核对，至少每月复核一次。超过周期或出现源码/运行状态漂移时标记待复核，
完成验证后再用于发布。历史材料保留原日期与结果，不将旧部署作为当前状态。
本次具体责任、事实源和失效信号见[文档清单](../reports/current-state-release-20261025/verification.md#文档事实源与更新责任)。
