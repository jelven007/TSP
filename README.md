# TSP

生产系统规范入口：[需求总纲与八模块](docs/requirements.md)、
[工程目录](docs/reference/engineering.md#layout)、[生产开发](docs/reference/engineering.md#engineering)、
[联合验收](docs/testing.md#acceptance)、[服务运营](docs/release-operations.md#service-2-开通与用户生命周期)。
全部本地变更与证据见[生命周期追踪](docs/reference/engineering.md#trace)。
2026-10-25最新整合策略日期归档/全局并发预算、两步向导、共享弹窗、
目录只读行情同步、16:00盘后默认和研究界面；
具体范围、全量验证与环境更新见[整合记录](reports/current-state-release-20261025/verification.md)。

面向沪深市场研究、数据治理、策略筛选和回测验证的一体化量化工作台。项目运行时
沿用 `TSP` 作为应用标识和 API Token 前缀。

[![CI](https://github.com/jelven007/TSP/actions/workflows/ci.yml/badge.svg)](https://github.com/jelven007/TSP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

> 本系统用于数据分析和量化研究，不提供投资建议。回测结果不代表未来收益，
> 使用者需要自行核验数据授权、数据质量和交易风险。

## 版本状态

仓库集成线为 `main`，本文所在源码应用语义版本为 `0.4.37`。
2026-10-25升级前实测新ECS `https://shanao.asia` 已运行
`7823b84b` / v0.4.21 / schema v6。本批源码候选与随后部署结果分别留证；
旧实例`092bd79c`/v0.4.11及2016起回填属于历史证据。
稳定标签、生产提交、源码候选和开源观察对象的完整分类见
[版本基线](docs/version-baseline.md)。

2026-10-22 源码整合补齐五档封板证据、收盘临时价保护、公共版本回收和 Windows
单机资源控制，详见[全生命周期变更](reports/local-consolidation-20261022/verification.md#local-contract-2026-10-22-本地变更整合与生命周期基线)。
自选接口连接池超时仍在调查；本次源码整合不表示生产升级或永久修复完成。

## 项目定位

TSP 是持续维护的专用版本，当前重点是：

- 默认使用 `rustdx` 建立沪深行情数据层，保留 TickFlow、fuyao 作为备选 Provider。
- 将选股、信号、因子、回测、监控和复盘统一到同一份标准化数据口径。
- 对策略结果提供可追溯的配置、数据版本、费用和风险指标。
- 同时支持本地桌面运行和服务端部署，并明确区分两种环境的认证边界。
- 保持生产级测试、发布、备份、升级和回滚文档。

本仓库仅保留产品、工程、部署和合规所需内容，不包含与运行无关的推广或个人入口。

## 主要能力

| 领域 | 能力 |
| --- | --- |
| 数据 | 七项独立路由：日 K、除权因子、公司行为、实时、分钟、五档、财务；分钟历史与盘中落盘共用选源；股票、核心指数与扩展数据 |
| 数据治理 | 标的 SCD2、历史 ST/风险警示治理、数据完整性检查、增量与盘后同步 |
| 研究 | 22个公开内置策略、入场主题/风险政策、自定义策略/信号/因子；结果按日期、版本和参数归档，可查看旧历史 |
| 回测 | 原始价成交约束、T+1、最低佣金、印花税、滑点、申报数量、未平仓、归因与导出 |
| 盘中 | 自选、实时行情、集合竞价、异动、连板梯队、共享快照策略监控与通知 |
| 交易辅助 | 持仓提醒、模拟交易、多账户对比、自动跟单、组合裁决；带日期证据门禁的分红/送转/配股账务 |
| AI | 兼容服务与 Codex CLI；Chat Completions / Responses、受控工具助手、会话同步与显式保存的策略迭代草稿 |
| 开放能力 | 六个 Token scope、69 个 Tier A 方法/路径、SSE、12 个 MCP 工具 |

页面、功能、源码及限制的对应关系见[当前功能索引](docs/architecture.md#capabilities)。
公司行为日期不完整时暂停相关账务及撮合；可选秒级归档只保存启用后的真实快照。
十年历史筛选、ClickHouse 和分布式研究属于[后续需求](docs/requirements.md)。

## 运行模式

### 本地桌面版

Windows 桌面版与原生源码版使用单机管理员模式，暂停多租户：

- 默认账号 `admin`，初始密码 `Admin123`；登录后拥有个人空间和完整业务功能。
- 在「设置 → 账户」修改密码，重启不会重置；不开放注册和租户切换。
- 数据和敏感配置保存在本机。

macOS 和 Linux 桌面包保留免登录模式。Windows 10 / 16 GB 的启动、资源配置和
旧多租户切换步骤见[单机管理员说明](docs/operations.md#windows)。

桌面产物以当前仓库的
[GitHub Releases](https://github.com/jelven007/TSP/releases) 为准。

### 服务端部署

完整单机多租户版本已完成一期部署，支持管理员和租户角色、邮箱验证码注册、
自动创建个人租户、PostgreSQL 业务隔离以及 100 个独立用户混合负载。
部署入口见[deploy/full](deploy/full/README.md)，发布记录见
[一期实施与验收](reports/document-consolidation-20261024/historical-evidence.md#phase1)。生产部署状态以
[版本基线](docs/version-baseline.md)和对应不可变发布记录为准。

非 Windows 首次初始化默认开启多租户，注册和找回密码同时启用；
Windows 默认使用单机管理员模式：

- 默认管理员 `admin/Admin123`，默认注册口令 `天王盖地虎`；注册、找回及邮件通知需配置系统 SMTP。
- 多租户须准备 PostgreSQL 双库、运行角色、平台和 Worker；入口见[完整部署](deploy/full/README.md)。
- 已修改密码、注册口令和自定义邮件配置不因启动重置。
- 公网部署必须使用 HTTPS，并限制 `.env`、数据目录和备份权限。

## 快速开始

### Docker Compose

默认完整多租户需先按[完整部署](deploy/full/README.md)准备双库、私密环境和内部服务，
再使用 `deploy/full/compose.yml` 启动。仅复制 `.env.example` 不足以准备这些依赖。

根目录 Compose 仍可作为显式旧独立模式使用，保留多邮箱共享空间：

```bash
git clone https://github.com/jelven007/TSP.git
cd TSP
cp .env.example .env
# 仅旧独立模式需要显式关闭多租户
printf '\nMULTITENANCY_ENABLED=false\n' >> .env
docker compose up --build -d
```

打开 <http://localhost:3018>。

当前文档默认从源码构建镜像，不依赖未验证的公共镜像标签。生产环境应从经过验收的
提交或版本标签构建，并记录最终镜像 digest。

### 开发模式

前置要求：

- Python 3.11+
- Node.js 20+
- [`uv`](https://docs.astral.sh/uv/)
- pnpm 9
- [Rust 编译工具链](https://www.rust-lang.org/tools/install)（源码安装默认原生桥接）

macOS / Linux 默认多租户：先准备双库 schema v6、平台与 Worker，
将运行 DSN 和内部服务配置写入私密 `.env`（见[完整部署](deploy/full/README.md)），再启动 API 和前端：

```bash
cp .env.example .env
./dev.sh
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
.\dev.ps1
```

默认地址：

- 前端开发服务：<http://localhost:3011>
- 后端服务：<http://localhost:3018>

完整部署、反向代理、老 CPU 兼容和账户初始化说明见
[部署指南](docs/operations.md#installation)。

## 首次配置

1. 打开“设置 → 数据源”，确认默认 rustdx 及其运行状态。未就绪时仍可选择并保存路由，页面显示依赖安装说明。
2. 首次启动默认自动同步；打开“数据总览”查看任务进度、结果及数据画像，失败后可点击“立即同步”重试。数据任务缺省开启，已有关闭设置保留。
3. 初始化失败后修复数据源，再手动重试；按需开启“同步调度 → 自动同步”恢复增量任务，实时、分钟与扩展自动拉取分别配置。
4. 在“策略”和“回测”页面验证策略结果及费用口径。
5. 按需配置 AI、监控通知、Open API Token 和 MCP。

全新安装的七项数据能力默认路由到 `rustdx`，采用 v2.0.4 固定源码，
单进程共用最多 8 条业务连接及 1 条独立探测连接，
原生桥接随后端依赖默认安装，Docker 和桌面包内置对应平台的扩展；构建和开发
启动会检查桥接是否就绪。已保存的路由保持原值。数据源实际可用性受公开服务器、
网络和协议覆盖影响，系统不会在失败时静默切换到其他数据源。

初始化覆盖沪主板、深主板、创业板、科创板的当前有效股票：一年日线及对应除权、
五张历史财务与股本表、配置窗口内的分钟线（默认 5 天，强制回溯含休市余量）、
当前实时行情和五档盘口。实时与盘口保存在独立快照中，不替代官方历史日线。
详细的恢复和部署行为见 [rustdx 数据源](docs/reference/integrations.md#rustdx-首次启动初始化)。

## 关键配置

从 `.env.example` 创建 `.env`。不要提交真实密钥。

```ini
# 服务
HOST=0.0.0.0
PORT=3018
DATA_DIR=./data

# 可选数据源
TICKFLOW_API_KEY=

# 可选 AI
AI_PROVIDER=openai_compat
AI_PROTOCOL=chat_completions
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=
AI_MODEL=deepseek-chat

# 可选覆盖内置扩展概念/行业上游，留空使用默认接口
EXT_CONCEPT_DATA_URL=
EXT_INDUSTRY_DATA_URL=
```

扩展概念和扩展行业默认可直接拉取，环境变量可独立替换对应上游；调度保持交易日
`09:15-11:30`、`13:00-15:15` 每分钟一次，启动时立即刷新。
旧版未使用的空预设会恢复默认配置；已有用户保存的 URL、主动关闭状态和定制
调度设置保持不变。默认地址及关闭入口见[配置说明](docs/operations.md#config-内置扩展概念与行业)。

配置全集见[配置说明](docs/operations.md#configuration)。

## 数据与调度原则

- 实时行情：交易日 `09:15-11:30`、`13:00-15:15`，轮询偏好 1 秒；实际间隔和源新鲜度需量测。
- 盘中分钟增量：相同交易时段，偏好 1 秒；rustdx 生效周期至少 60 秒，其他 Provider 按能力和下限执行。
- 扩展概念/行业：内置默认上游，交易日双时段每分钟一次，可替换来源或关闭。
- 盘后主管道：同步日线、除权因子、分钟数据并重算 enriched 指标。
- 所有边界采用半开区间，午休期间不拉取。

独立模式的 `data/` 保存行情和私有业务；完整多租户还使用两个 PostgreSQL 库、
独立平台配置与不可变公共版本目录。升级、迁移和回滚按模式完成一致性备份，
见[运维手册](docs/operations.md)。

## 回测正确性

生产研究应至少满足：

- 使用历史时点可见的数据，禁止未来函数。
- 纳入历史 ST/风险警示治理数据。
- 纳入佣金、滑点和卖出印花税。
- 遵守 A 股 T+1、停牌和涨跌停约束。
- 绝对价格、申报数量和涨跌停使用原始价；指标与收益在一致复权尺度计算。
- 开盘封板、最低佣金、同日资金顺序、买单过期和期末未平仓不能被理想化成交掩盖。
- 盈亏比、金额利润因子和单笔净期望使用各自明确定义，不能互换。
- 跨越完整市场周期评估，不以单一年度结果代替稳定性结论。
- 对比结果时确认策略源码、参数和数据 release/config hash 一致。

详细规则见[贡献与复审指南](CONTRIBUTING.md)和
[测试与验收](docs/testing.md)。

## 架构摘要

```text
数据源 / 插件
  -> Provider 能力路由与标准化
  -> Parquet / DuckDB 数据层
  -> enriched 指标流水线
  -> 策略 / 因子 / 回测 / 监控 / 分析服务
  -> FastAPI / SSE / Open API
  -> React 前端 / MCP 客户端
```

后端复用模块化单体引擎。独立模式由一个主进程编排行情与任务；
完整多租户拆为 Platform、API、Gateway 和四类 Worker，计算经主机 Broker 隔离执行。
前端使用 React、TypeScript、Vite、TanStack Query 和 Tailwind CSS。

完整边界见[架构文档](docs/architecture.md#current)。

## 验证

后端：

```bash
cd backend
uv sync --extra dev --extra backtest --frozen
uv run --frozen --no-sync python -m app.plugins.rustdx.client
uv run --no-sync pytest
```

前端：

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm test -- --run
pnpm lint
pnpm build
```

提交前还应执行：

```bash
git diff --check
```

## 文档

| 文档 | 用途 |
| --- | --- |
| [文档中心](docs/README.md) | 文档地图、事实源和更新规则 |
| [当前功能索引](docs/architecture.md#capabilities) | 页面入口、七项能力、实现与测试依据、当前限制 |
| [工程目录](docs/reference/engineering.md#layout) | 中型生产系统目录、职责、脚本迁移与检查入口 |
| [操作说明书](操作说明书.md#tsp-a-股智能量化工作台操作说明书) | 页面操作、维护和排错 |
| [部署指南](docs/operations.md#installation) | Compose、开发和服务端部署 |
| [配置说明](docs/operations.md#configuration) | 环境变量和页面配置 |
| [功能说明](操作说明书.md#1-项目概览) | 当前功能边界 |
| [架构文档](docs/architecture.md#current) | 模块、数据流和故障边界 |
| [运维手册](docs/operations.md) | 巡检、备份、恢复和故障处理 |
| [发布流程](docs/release-operations.md) | 版本、产物、上线和回滚 |
| [二次开发](docs/secondary-development.md) | 扩展点和核心修改规则 |
| [rustdx 数据源](docs/reference/integrations.md#rustdx) | v2.0.4、8业务+1探测和历史财务 |
| [策略开发](docs/reference/research.md#strategy) | 内置、自定义和 AI 策略 |
| [MCP 服务器](mcp-server/README.md) | AI 客户端接入 |

## 第三方组件与数据源

项目会按配置使用第三方依赖和数据服务，包括 `rustdx`、TickFlow、fuyao、OpenAI、
DeepSeek、GLM 等。仓库中的链接用于说明功能或许可证，不代表合作、背书或可用性
承诺。部署者必须自行确认服务条款、数据授权、隐私政策和网络可达性。

第三方许可证和翻译归属属于法定或上游许可信息，保留在相应文件中。

## License

本项目代码按 [MIT License](LICENSE) 发布。第三方依赖及资源适用各自许可证。
