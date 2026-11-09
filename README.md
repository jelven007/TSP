# Tick Stock Platform（TSP）

2026-11-09本轮统一候选及六类文档、用户可见变化、兼容/验证/部署边界见
[版本整合记录](docs/upgrade-20261109.md)。正式身份与实际双端升级以对应Release验收为准。

2026-11-08 S02已取消20日均成交额≥1亿元条件，当前`overlay_filter=[]`。
预选仍按前日价格4～150元、换手1%～11%、涨幅0.5%～6%、流通市值50～500亿元筛选；
完整池只按范围及状态形成。当前模板与历史规则的边界见[策略参考](docs/reference/s02.md)。

2026-11-08最新实时新入场截止为14:55:00，整点允许、超过拒绝，
不随确认延迟变化；14:30启动和30秒确认保持。已部署热补丁正归入正式主线，
统一版本和双端验收见[本轮记录](docs/upgrade-20261108.md)，历史冻结回测保留。

> 2026-11-07最新生效规则：当日股票池为满足股票范围与状态条件的所有股票，去除流通市值约束；流通市值及其他数值条件仅用于实际策略选股。当前口径`scope_only_v3`，盘前只依赖证券目录与当日状态证据，08:00/09:00旧口径成功标记不阻止新口径生成。两端0.4.52已安装并验证Runtime/Worker/Compute；保存的30秒确认和≥60%保持。历史池、冻结研究与账本保留原口径，以下历史记录按其日期理解。

2026-11-07统一升级候选纳入严格收盘价/T-1校验、回测错误传递、保存建议、结果排序和
同SHA质量门/镜像并行、容量/两版恢复前置、持久OCI缓存。
行为与六类文档见[统一候选](docs/upgrade-20261107.md)，工具见[发布流水线](docs/release-pipeline.md)。
实际部署以最终验收签收，本地继续暂停；后继版本由主线CI分配。

2026-11-06统一候选整合S02历史读取、60秒确认、实时列、分时OHLC及数据调度，
并保留既有配置和归档；六类文档、当前质量门及发布状态见
[本轮升级记录](reports/releases/unified-upgrade-20261106/verification.md)。

最新升级候选保留固定过户费支持与线上内置策略退役，历史结果及用户配置保持；
当前候选与实际发布状态见[最新升级记录](reports/releases/current-upgrade-20261103/verification.md)。

面向沪深股票与指数的数据治理、看盘、策略筛选、因子研究和回测验证工作台，
支持单机运行与完整单机多租户部署。

2026-11-07 S02“选”页默认按ATR14÷昨收降序显示，同值代码升序，缺值在末尾。
手动排序清除后恢复默认；名单、买卖及仓位参数保持。
[本次验证与安装](reports/engineering/s02-default-atr-sort-20261107/report.md)。

2026-11-07 S02“选”页表头及列设置统一为“参考日换手率”，仍使用前交易日数据。
603517的10月19日及600621的10月26、27日均为真实停牌；
两端当前完整范围3048只、21个交易日核查未发现未解释日K遗漏。
历史停牌日保持无K，详细证据与本次安装见
[核查记录](reports/engineering/s02-reference-daily-20261107/report.md)。

2026-11-07 S02卖／选／买统一移除ATR排名列；选页补齐ATR14÷昨收与排名分数，
休市时显示精确最近交易日的真实收盘行情和日期。两端0.4.52已安装，
缺连续日K的股票保留名单并显示“—”。公式、补齐覆盖和验收见
[本次记录](reports/engineering/s02-market-atr-20261107/report.md)。

[![CI](https://github.com/jelven007/TSP/actions/workflows/ci.yml/badge.svg)](https://github.com/jelven007/TSP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

应用语义版本为 `0.4.58`。文档核对日期：2026-11-02。
源码、正式发布和最后已知运行状态见[版本基线](docs/version-baseline.md)。
本次本地功能、六类文档与清理范围见[整合记录](reports/release-consolidation-20261101.md)。

2026-11-06生产S02已补齐11月4～6日建议数据：卖出区间为精确前交易日原始收盘价
的94%～106%，购买手数按保存触发价计算10万元最大整手数，并在后续生成时默认保存。
三日数据与当前0.4.50安装状态见
[候选契约](docs/upgrade-20261107.md#来源与实际行为)。

## 主要能力

2026-11-06通用分钟回测已补齐冻结前交易日资格名单及分区错误传递，
回测表单保留日线成交额叠加，历史任务按实际字段显示滑点。
已安装生产0.4.50，本地服务保持暂停；原零收益记录无效且保留，未新跑收益，
见[本轮整合](docs/upgrade-20261107.md#来源与实际行为)。

| 领域 | 当前功能 |
| --- | --- |
| 数据管理 | 七项 Provider 路由、官方证券目录、SCD2 状态、日 K/分钟采集、财务与公司行为、扩展数据 |
| K 线与指数 | 股票 1m/5m/10m/15m/1d/1w；固定七只指数日/周/月 K 与分时；全量/遗漏获取及持久缺口反馈 |
| 看盘与自选 | 实时行情、集合竞价、异动、连板梯队；一股多组、多策略标记、CSV 导入导出与详情联动 |
| 策略与研究 | 内置、自定义、叠加和 AI 策略；参数、目标股票、信号与评分；按日期、版本和参数归档结果 |
| 因子与回测 | 因子检验、组合、挖掘、策略归因、参数优化与步进优化；明确的成交、费用、T+1 和历史质量约束 |
| 监控与模拟 | 策略/价格/信号监控、通知、多虚拟账户、自动跟单、组合裁决、公司行为权益台账 |
| AI 与复盘 | 兼容模型或 Codex CLI、受控工具助手、会话恢复、显式保存的迭代草稿与复盘 |
| 开放与扩展 | 六种 Token scope、69 个 Tier A 方法/路径、SSE、12 个 MCP 工具；Provider 和源码扩展注册 |

完整页面、能力和源码对应见[功能地图](docs/architecture.md#capabilities)，
日常操作见[操作说明书](操作说明书.md)，接口入口见[API 导航](docs/api.md)。
ETF 已退役。公司行为日期证据不足时暂停相关账务和撮合；
秒级归档只保存启用后的真实快照。

## 开始使用

详细步骤见[开始使用](docs/getting-started.md)。

| 运行方式 | 说明 |
| --- | --- |
| 本地源码 | Python 3.11+、Node.js 20+、uv、pnpm 9、Rust；`./dev.sh` 或 `.\dev.ps1` |
| 根 Docker Compose | 单容器托管前后端；单机模式须显式关闭多租户 |
| 完整多租户 | [deploy/full](deploy/full/README.md)：PostgreSQL 双库、平台、API、Gateway、Worker、Compute/Broker |
| 桌面安装包 | 从 [Releases](https://github.com/jelven007/TSP/releases) 获取目标平台已验收资产 |

非 Windows 源码环境默认多租户，不能只复制环境模板就假定双库与内部服务已准备。
Windows 默认完整单机管理员个人空间，`SERVICE_ROLE=standalone`，
关闭注册、找回与租户切换。桌面认证仍取决于实际运行模式。

单机开发：

```bash
git clone https://github.com/jelven007/TSP.git
cd TSP
cp -n .env.example .env
```

在 `.env` 中设置 `MULTITENANCY_ENABLED=false`、`SERVICE_ROLE=standalone`，
保留已有用户配置，再运行：

```bash
./dev.sh
```

默认前端 `http://localhost:3011`，后端 `http://localhost:3018`。
相同单机配置也可运行 `docker compose up --build -d`，访问端口 3018。
生产安装、Windows 和旧 CPU 兼容见[运维手册](docs/operations.md#installation)。

## 首次配置与数据

全新安装按初始化配置预置管理员；用户指定的账户、注册/找回口令和 SMTP
配置优先，后续修改不因重启重置。真实授权码只保存在受控运行配置。
已有 `.env`、身份、策略和行情不能被初始化步骤覆盖。

先核对数据源能力，完成证券目录、日 K、除权因子与指标准备，再按需要补分钟和财务。
默认使用 rustdx，保留 TickFlow、fuyao、HTTP 和插件接入；
实时、分钟、日 K、除权、公司行为、五档与财务按能力矩阵独立路由。
5/10/15 分 K 只由本地 1 分 K 生成，周 K 只由本地日 K 生成。

1 分 K/日 K 手动采集使用明确起止闭区间。“获取全量数据”更新该范围，
“获取遗漏数据”先查本地并仅补缺失键。任务成功、零请求或 100% 进度
不等于多年历史已经完整；查看剩余计数和持久缺口清单。
当前财务表和历史观察快照分别核对，观察时间不能冒充已认证公告时间。

## 工程结构

```text
backend/app/       API、业务服务、数据仓库、指标、策略、因子、回测与多租户
backend/native/    固定 rustdx 核心与 PyO3 桥接
frontend/src/      React 页面、共享组件、API 类型、查询键与扩展
deploy/           单机多租户、HTTPS、PostgreSQL 与 Windows 模板
packaging/        桌面构建与安装资源
scripts/          工程检查、版本、构建身份、升级和源码恢复
tests/tooling/    仓库工具、文档与发布契约回归
examples/         HTTP 数据源与开放 API 样例
mcp-server/       独立 MCP 包
docs/             当前工程文档与专题参考
reports/          需保留的验收证据与调查
data/             运行时数据，不提交到 Git
```

完整目录归属和构建边界见[工程参考](docs/reference/engineering.md#layout)。
当前是模块化单体与七角色单机实现；
分布式研究、Kafka/Flink/ClickHouse 等组件和万人活跃容量属于
[阶段目标](docs/planning/phase2-implementation-plan.md)，按实际证据分别签收。

## 开发与验证

修改或审查前完整阅读 [AGENTS](AGENTS.md) 和 [CONTRIBUTING](CONTRIBUTING.md)；
二次开发还须阅读[扩展与升级契约](docs/secondary-development.md)。
技术栈为 FastAPI/Pydantic、Polars/DuckDB/Parquet、
React 18/TypeScript/Vite/TanStack Query。pandas 用于回测边界，vectorbt 为可选依赖。

安装冻结开发依赖后按改动范围执行：

```bash
make check
make test-tooling
make test-backend
make test-frontend
make build-frontend
```

命令和隔离要求见[测试指南](docs/testing.md)。
本地和功能分支不递增版本；远程 `main` 集成后由 CI 自动递增一次，
固定同一 SHA 的质量门、标签与产物分别验收。

## 文档与维护

[文档中心](docs/README.md)是完整入口，覆盖需求、方案、开发、测试、运营和运维。
备份与恢复见[运维手册](docs/operations.md)，发布见[运营手册](docs/release-operations.md)，
数据字段和外部接入见[专题参考](docs/reference/integrations.md)。
历史资料按[维护规则](docs/maintenance.md)清理，保留实际验收、未完成调查和所需恢复点。

项目用于数据分析与量化研究，不构成投资建议；历史表现不代表未来收益。
第三方数据使用仍需遵守供应商授权与服务条款。代码采用 [MIT License](LICENSE)。
