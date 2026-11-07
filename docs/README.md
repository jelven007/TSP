# Tick Stock Platform（TSP）文档中心

2026-11-08最新源码、六类手册及双端发布门禁见[统一候选](upgrade-20261108.md)。
脱敏历史正文与用户原始交付分开保存，当前正式发布和运行状态由本轮回执认证。

2026-11-08自动跟单参数融合已实现并隔离验证，当前为未部署源码候选。
五组表单、回测草稿、完整候选池序、双侧成交及账户费用扩展的六类文档映射见
[实施回执](../reports/engineering/paper-auto-rule-parameters-20261108/report.md)。

2026-11-08自动跟单规则修改已安装本地与线上0.4.52，入口为模拟账户规则行的“修改”。
需求、架构、二开、测试、运营与运维状态及双库恢复/重启证据见
[双端安装验收](../reports/operations/paper-rule-edit-20261108/report.md)。

2026-11-07当前需求、方案、开发、测试、运营及运维变化集中见
[统一升级候选](upgrade-20261107.md)，固定工具见[发布流水线](release-pipeline.md)。
本地暂停、当前100万元/页面实际参数规则和历史研究边界分别记录。

最新统一候选与六类文档映射见[2026-11-06升级](../reports/releases/unified-upgrade-20261106/verification.md)。
以下历次热补丁部署和计数保留原身份；正式版本及双端状态分别验收。

2026-11-05盘后预选的本地/线上0.4.49安装、配置保留及首批定时观察状态见
[双端部署回执](../reports/releases/unified-upgrade-20261106/verification.md#historical-hotfixes)；
源码版本保持0.4.46。

同日随后授权补生成11月5日的选名单，两端各574只；当前运行身份、来源、
页面及买入引用验证见[补生成回执](../reports/releases/unified-upgrade-20261106/verification.md#historical-hotfixes)。

本轮[最新现场兼容升级](../reports/releases/current-upgrade-20261103/verification.md)汇总
固定过户费、部署内置退役、六类文档及独立发布门禁；撤回候选与历史研究分别保留。

2026-11-02后续补丁统一发布的需求／方案／开发／测试／运营／运维映射见
[本轮整合](../reports/release-consolidation-20261102-followup.md)，
当前版本和两端状态仍由[版本基线](version-baseline.md)维护。

s02参数、每日三Tab与回测历史查询见
[策略参考](reference/s02.md)。

应用语义版本为 `0.4.53`。内容核对日期：2026-11-02。
本目录面向完整项目工程，覆盖使用、需求、架构、开发、测试、发布、运维与扩展。
源码版本、已发布产物和最后已知运行状态统一见[版本基线](version-baseline.md)。

## 从这里开始

| 读者与任务 | 阅读顺序 |
| --- | --- |
| 首次安装或本地开发 | [开始使用](getting-started.md) → [操作说明书](../操作说明书.md) |
| 日常看盘、选股、研究与模拟观察 | [操作说明书](../操作说明书.md) → 对应专题参考 |
| 管理数据、账号与服务 | [部署与运维](operations.md) → [部署目录](../deploy/README.md) |
| 修改或审查代码 | [AGENTS](../AGENTS.md) → [CONTRIBUTING](../CONTRIBUTING.md) → [系统方案](architecture.md#current) → [测试指南](testing.md) |
| 二次开发或接入外部系统 | [二次开发](secondary-development.md) → [API 导航](api.md) → [接入协议](reference/integrations.md) |
| 准备发布、升级与恢复 | [发布运营](release-operations.md) → [发布流水线](release-pipeline.md) → [运维手册](operations.md) → [版本基线](version-baseline.md) |
| 定位代码、脚本或测试 | [工程目录](reference/engineering.md#layout) → [测试定位](../tests/README.md#按领域定位现有测试) |
| 维护文档或查找历史证据 | [文档维护与清理规则](maintenance.md) → [报告分类](../reports/README.md#按用途查找) |

## 项目文档地图

```text
README.md                         项目介绍与入口
AGENTS.md                         AI 开发入口
CONTRIBUTING.md                    开发、数据契约与复审规范
操作说明书.md                     用户功能手册
docs/
  README.md                       文档地图
  getting-started.md               环境、安装、首次配置与验证
  requirements.md                 唯一需求定义与八业务模块
  architecture.md                 当前架构、功能血缘与后续系统目标
  api.md                          会话、开放 API、SSE 与 MCP 导航
  secondary-development.md        实际扩展点及升级兼容
  testing.md                      验证矩阵、命令与签收边界
  release-operations.md           产物、发布、支持与运营
  release-pipeline.md             固定执行入口、并行构建、回执与缓存
  operations.md                   配置、调度、备份、恢复与故障处理
  version-baseline.md             源码、发布和运行版本分类
  maintenance.md                  文档归属、清理范围与检查方法
  reference/
    engineering.md                目录、构建身份、协作与追踪
    market-data.md                行情、K线、目录与扩展数据
    research.md                   策略、固定输入、回测与挖掘
    s01.md                        隔夜分钟策略参数、成交与使用边界
    s02.md                        动量策略、每日组合与回测查询
    factor-system.md              因子实现及明确标记的后续设计
    trading-ai.md                 模拟账务、公司行为与AI协议
    integrations.md               Provider、HTTP源与开放授权
  rustdx-task-routing.md           三组连接、额度与窗口核验
  planning/
    phase2-implementation-plan.md  阶段工作包与待签收能力
```

## 六类主手册

| 生命周期 | 唯一主入口 | 维护内容 |
| --- | --- | --- |
| 需求 | [需求基线](requirements.md) | 角色、对象、八业务模块、164 条 FR、42 条 NFR、状态与验收条件 |
| 方案 | [系统技术方案](architecture.md) | 当前运行拓扑、源码事实源、数据和缓存链路、明确标记的目标架构 |
| 开发 | [贡献规范](../CONTRIBUTING.md) | 分层、金融口径、Provider、并发、兼容、质量门和 PR 复审 |
| 测试 | [测试与验收](testing.md) | 隔离环境、定向回归、工程检查、平台与生产签收 |
| 运营 | [发布与服务运营](release-operations.md) | 发布身份、渠道、放行、支持、成本与反馈 |
| 运维 | [部署与运维](operations.md) | 模式安装、配置、权限、健康、调度、备份、恢复与值班 |

用户操作只在操作说明书定义，协议细节归专题参考。其他文件链接到主章节，
避免重复维护第二套需求、配置默认值或发布状态。

<a id="source-map"></a>
## 当前功能与工程入口

| 领域 | 文档 | 主要实现入口 |
| --- | --- | --- |
| 六周期股票 K 线、指数日/周/月 K 线、采集与缺口 | [行情数据](reference/market-data.md) | [周期服务](../backend/app/services/kline_periods.py)、[指数 API](../backend/app/api/indices.py) |
| 数据源路由、实时、五档、财务与公司行为 | [接入参考](reference/integrations.md) | [能力注册表](../backend/app/data_providers/capabilities.py) |
| 自选、策略与持久结果、历史研究 | [用户手册](../操作说明书.md)、[研究契约](reference/research.md) | [页面路由](../frontend/src/router.tsx)、[策略结果仓库](../backend/app/services/strategy_results.py) |
| 因子、信号与挖掘 | [因子体系](reference/factor-system.md)、[研究契约](reference/research.md#mining) | [因子模块](../backend/app/factors/)、[挖掘 API](../backend/app/api/mining.py) |
| 监控、模拟交易、AI 与复盘 | [账务和 AI](reference/trading-ai.md) | [监控 API](../backend/app/api/monitor_rules.py)、[模拟交易 API](../backend/app/api/paper.py) |
| 认证、API、SSE 与 MCP | [API 导航](api.md) | [开放网关](../backend/app/services/api_gateway.py)、[MCP 包](../mcp-server/README.md) |

运行模板见[部署目录](../deploy/README.md)，仓库命令见[脚本入口](../scripts/README.md)
和[后端操作模块](../backend/scripts/README.md)，可运行样例见[examples](../examples/README.md)。
设计、实施计划和缺陷记录沿`.claude/artifacts/`归属；现行手册保留稳定入口。
历史报告按六类导航，新增用户交付按用途和专题存放，具体规则见[文档维护](maintenance.md#4-证据目录)。

<a id="maintenance"></a>
## 事实源与维护

当前行为以源码、配置读取规则和相邻契约测试为依据；生产状态必须附固定提交、
产物哈希或 digest、schema 和实际切换证据。代码实现、测试通过、发布和部署分别判断。
历史任务成功不等于历史行情全部补齐，规划容量不等于已经通过负载验收。

每次行为、接口、拓扑或配置变化同步相应主手册与专题参考，至少每月复核一次。
正文保留现行规则，发布过程与原始证据归[报告目录](../reports/README.md)。
完整维护责任与清理规则见[maintenance.md](maintenance.md)。

从仓库根执行 `make check`。历史资料引用可另执行
`python3 scripts/check_docs.py --include-reports`；这些检查不替代业务、真实数据或生产验收。
