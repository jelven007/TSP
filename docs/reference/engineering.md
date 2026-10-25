# 工程与交付参考

2026-10-26六周期画像为L3聚焦变更。需求见[需求补充](../requirements.md)，
方案见[当前架构](../architecture.md)，开发入口为 `services/kline_periods.py` 与
`data_providers/*/get_klines`；前端复用 `KlineSyncConfig`、共享查询键和设置弹窗。
测试映射见[验收指南](../testing.md)，运营入口见[发布运营](../release-operations.md)，
备份、旧分钟基准及公共目录见[运维](../operations.md)，用户操作见
[六周期设置](../../操作说明书.md#227-分钟-k-设置)。
新增表仅用于数据画像和同步，不把它们自动注册为策略/回测可选周期。
版本沿用当前主线，合并时对基线三方应用，保留其他本地修改。

本附录按领域维护实现方法、数据契约和可执行示例。通用开发要求只在[CONTRIBUTING](../../CONTRIBUTING.md)定义；需求与当前/目标边界见[需求](../requirements.md)和[系统方案](../architecture.md#current)。带日期的结果仅证明原版本、环境和窗口，不是本次重新执行的验证。

一级页面顶栏统一复用 `frontend/src/components/PageHeader.tsx`，以策略页为样式基准：
桌面20px标题、10px说明、最小64px高度和全宽底部分隔线；标题侧可加状态，操作区保持紧凑，
窄屏单独一行横向滚动并避开菜单按钮。共享规则在 `index.css` 的 `.page-header-actions`，
页面不再覆盖顶栏间距或重复实现卡片式标题。正文图表和领域操作保留各自结构；
行情的加载/失败状态也使用同一标题组件。对应需求 `FR-UI-05`。

- [1. 仓库布局与文件归属](#layout)
- [2. 从需求到生产的工程闭环](#engineering)
- [3. 构建身份与离线升级材料](#build)
- [4. 多人协作与远程权限](#collaboration)
- [5. 需求与生命周期追踪](#trace)

<a id="layout"></a>

<a id="layout-项目目录与工程边界"></a>
## 1. 仓库布局与文件归属

生产开发闭环见[生产工程](#engineering)，全部本地变更见[生命周期追踪](#trace)。

更新日期：2026-10-25。本文定义中型生产系统的实际仓库布局、文件归属和目录迁移规则。
当前系统采用模块化单体与独立运行角色，源码目录不等同部署进程。
实现架构见[当前架构](../architecture.md#current)，开发规则见[贡献指南](../../CONTRIBUTING.md)。

<a id="layout-1-完整目录"></a>
### 1. 完整目录

下列源码与工程目录已存在。`data/`、依赖缓存和构建输出由运行或构建生成，不要求在干净检出中存在。

```text
TSP/
├── AGENTS.md                       # AI 开发入口
├── CONTRIBUTING.md                 # 数据契约、质量门与协作规范
├── README.md / LICENSE / VERSION    # 项目入口、许可、应用版本
├── Makefile                        # 跨模块检查与测试入口
├── .github/
│   ├── workflows/                  # CI、容器、桌面与 Windows 检查
│   └── rulesets/                   # 主线、集成与版本标签保护
├── .env.example / tiers.yaml        # 配置模板与套餐配置事实源
├── Dockerfile / docker-compose.yml  # 独立运行镜像与 Compose 入口
├── dev.sh / dev.ps1 / update.ps1    # 本地启动与 Windows 更新入口
├── backend/
│   ├── pyproject.toml / uv.lock     # 后端依赖与冻结锁
│   ├── app/
│   │   ├── main.py / config.py      # 应用组装、生命周期与配置
│   │   ├── api/                    # HTTP、SSE 与响应契约
│   │   ├── services/               # 行情、同步、通知及业务编排
│   │   ├── jobs/                   # 盘后管道等调度任务
│   │   ├── data_providers/         # 数据集接口与能力路由
│   │   ├── plugins/                # 插件加载及供应商适配
│   │   ├── tickflow/               # 仓库、Parquet/DuckDB 与既有接入
│   │   ├── indicators/             # 指标与 enriched 流水线
│   │   ├── strategy/               # 策略、信号、评分与监控
│   │   ├── factors/                # 因子 DSL、注册与存储
│   │   ├── backtest/               # 回测、优化、回放与计算 worker
│   │   ├── multitenancy/           # 租户业务、网关、平台与 worker
│   │   ├── cell/
│   │   │   └── sql/               # control/data v1–v6 有序迁移
│   │   ├── extensions/            # 已实现的受控扩展契约
│   │   └── custom/                # 自定义模块、AI 助手与模板
│   ├── native/rustdx_native/        # Rust/PyO3 原生桥接与 Cargo 锁
│   ├── scripts/
│   │   ├── acceptance/             # 隔离验收、恢复和持续观察
│   │   ├── performance/            # 性能量测与并发压测
│   │   ├── operations/             # 备份、维护、回填和数据修正
│   │   ├── research/               # 离线分析与治理回测
│   │   ├── diagnostics/            # Provider 能力探测
│   │   └── demos/                  # 可复现行为演示
│   └── tests/
│       ├── backtest/               # 回测领域测试
│       ├── fixtures/               # 小型固定契约与测试夹具
│       └── test_*.py               # 现有服务、API、数据与租户测试
├── frontend/
│   ├── package.json / pnpm-lock.yaml
│   ├── vite.config.ts / tsconfig*.json
│   ├── public/                     # 前端静态资源
│   └── src/
│       ├── main.tsx / router.tsx    # 应用与路由入口
│       ├── pages/                  # 页面及就近测试
│       ├── components/             # 共享组件及就近测试
│       ├── lib/                    # API、查询键、hooks 与公共工具
│       ├── extensions/             # 路由、导航与插槽注册
│       └── custom/                 # 前端二开模块与模板
├── mcp-server/
│   ├── pyproject.toml              # 独立包元数据与版本
│   ├── src/tsp_mcp_server/          # MCP 适配实现
│   └── smoke_test.py               # 接入冒烟入口
├── deploy/
│   ├── full/                       # 多租户角色、Compute/Broker 与资源预算
│   ├── edge/                       # Nginx、HTTPS 与证书维护
│   ├── cell/pg/                    # PostgreSQL 部署与引导
│   └── windows/                    # 原生 Windows 服务与资源管理
├── packaging/                      # PyInstaller、安装器与桌面打包资源
├── scripts/
│   ├── check_repository.py          # 目录与关键入口检查
│   ├── check_docs.py                # 文档链接、锚点、围栏与需求检查
│   ├── check_version.py             # 应用版本一致性检查
│   ├── advance_version.py           # 远程 main CI 自动递增
│   ├── tag_version.py               # 主线质量门通过后创建标签
│   ├── versioning.py                # 版本规则
│   ├── prepare_release.py           # 已有标签的桌面发布准备
│   ├── build_manifest.py            # 构建清单与离线核验
│   ├── upgrade_check.py             # 上游升级只读预检
│   ├── acceptance/                 # 候选镜像构建与验收启动
│   ├── release/source_recovery.py  # 最新及五个历史源码归档与离线核验
│   ├── performance/                # 跨工作区性能对比
│   └── research/                   # 策略试验与结果汇总
├── tests/tooling/                   # 仓库工具、版本与工作流测试
├── docs/
│   ├── README.md                   # 六类主手册与专题导航
│   ├── requirements.md             # 163条FR（八业务章节）与42条NFR
│   ├── architecture.md             # 当前实现、代码事实源与统一系统目标
│   ├── testing.md                  # 测试命令、联合验收与基准方法
│   ├── operations.md               # 部署、配置、账户、巡检与恢复
│   ├── release-operations.md       # 发布、服务开通与用户支持
│   ├── secondary-development.md    # 受控扩展与上游兼容
│   ├── version-baseline.md         # 源码、标签、产物与生产状态
│   ├── reference/                  # engineering/market-data/research/
│   │                              # factor-system/trading-ai/integrations
│   └── planning/phase2-implementation-plan.md # 当前执行工作包
├── examples/
│   ├── README.md                   # 可运行示例与前置条件
│   ├── custom-data-source/         # 合成行情 HTTP 联调服务和 YAML
│   └── open-api/                   # 外部调用示例
├── brand/                          # 品牌 SVG、PNG 与图标
├── screenshots/                    # 面向用户的功能截图
├── reports/
│   ├── README.md                   # 保留证据及其与当前主线/目标的关系
│   ├── design-inputs-20261023/       # 原融合/CH设计与参考图的冻结依据
│   ├── page-data-analysis-20261024/  # 页面、菜单、数据血缘和来源分析
│   ├── production-integration-20261024/ # 完整变更、验证与历史版本恢复点
│   └── <专题-日期>/                # 版本与环境明确的验证证据
├── deliverables/                   # 当前目标离线阅读包；不进入 Git/镜像
├── 操作说明书.md                    # 中文操作入口
└── data/                           # 用户运行数据；运行时生成，不进入 Git/镜像
```

<a id="layout-2-文件归属与依赖方向"></a>
### 2. 文件归属与依赖方向

| 分类 | 维护角色 | 唯一归属与放置原则 |
| --- | --- | --- |
| 产品与需求 | 产品负责人 | 205条需求在 `docs/requirements.md`；FR按八章组织，历史依据在 `reports/` |
| 业务源码 | 后端／前端负责人 | 就近维护领域模块；API 和页面不承担仓库、Provider 的职责 |
| 原生依赖 | 数据接入负责人 | `backend/native/`；保持 Python 接入与锁定 revision 一致 |
| 数据库迁移 | 后端／数据负责人 | `backend/app/cell/sql/` 随应用打包；不复制到另一个顶层 SQL 目录 |
| 部署配置 | 平台／运维负责人 | `deploy/` 按部署形态组织；环境预算随对应 Compose 保存 |
| 配置 | 应用与运维负责人 | 根 `.env.example`、`tiers.yaml` 和 `deploy/full/*.env`；真实密钥只存本地受控文件 |
| 自动化与脚本 | 对应模块负责人 | 不依赖后端的仓库工具在 `scripts/`；依赖 `app` 的命令在 `backend/scripts/` |
| 测试 | 开发与测试负责人 | 后端测试在 `backend/tests/`，前端测试与组件就近，仓库工具测试在 `tests/tooling/` |
| 验证与交付 | 测试／发布负责人 | 可复核历史证据在 `reports/`；本地 Word、ZIP、生成方案在 `deliverables/` |
| 运行数据 | 部署者／数据管理员 | `DATA_DIR` 由应用管理，日志、策略、研究快照与行情不写入源码或测试目录 |

依赖沿“API／页面 → 业务服务 → 领域计算与仓库 → Provider／存储”向下；
脚本调用现有服务与契约，不形成第二套业务实现。网关、平台与四类 worker
复用 `backend/app/multitenancy/`，不能因进程独立就复制成多个服务源码包。

后端现有平铺测试与前端就近测试保持可发现。新增后端专题测试可以按现有
`backtest/` 模式建领域子目录，但不能按文件名猜测后批量移动已有测试。

2026-10-25 将 `docs/examples/custom-data-source/` 的三个文件迁至
`examples/custom-data-source/`，规范与可运行示例各归其位。新增恢复工具归
`scripts/release/`，测试归 `tests/tooling/`；既有 CI 版本工具保留稳定路径。
新股票目录沿 `data_providers/official_stock_catalog.py` →
`services/instrument_sync.py` / `instrument_catalog.py` → `api/instruments.py` 分层，
前端领域弹窗归 `components/data/`，统一顶栏归共享组件。

<a id="layout-4-开发与检查入口"></a>
### 4. 开发与检查入口

```bash
# 根目录，无需下载依赖或启动服务
make help
make check

# 已按冻结锁安装依赖后，执行对应范围
make test-tooling
make test-backend
make test-frontend
make build-frontend

# 直接命令适用于没有 make 的 Windows 环境
python scripts/check_repository.py
python scripts/check_version.py --current VERSION
uv run --project backend --frozen --no-sync pytest tests/tooling -q
```

`make check` 检查结构、文档本地链接/锚点/围栏、八模块/需求编号、版本和diff空白，不替代业务测试或部署验收。
生产命令见[部署入口](../../deploy/README.md)；测试分工见[测试目录](../../tests/README.md)。
脚本执行前按原说明准备依赖、一次性环境与参数，归档不授予生产写入权限。

<a id="layout-5-与后续规模化需求的关系"></a>
### 5. 与后续规模化需求的关系

S1–S3 的功能范围以[需求基线](../requirements.md)为准。
ClickHouse、对象存储、Ray 及消息流方案尚未接入当前应用，不创建假实现目录或
不可执行的生产配置。开始实现时，数据访问适配落在后端数据层，部署文件落在
`deploy/` 对应组件目录，迁移／查询契约与定向测试随模块提交。
只有真实独立发布单元出现时才新建顶层服务包。

<a id="layout-6-兼容与维护"></a>
### 6. 兼容与维护

- 根启动器、Dockerfile、配置事实源、前后端包路径、SQL 迁移装载路径继续有效。
- 应用版本由远程 main CI 管理；本地目录整理不手工递增。
- 当前需求、架构、配置与操作文档保留稳定入口；历史分析逐步归档，更新所有当前引用。
- `deliverables/`、`data/`、`.tools/`、缓存和构建输出不进入运行镜像。
- 目录调整只更新路径和包接线，不改变 API、数据单位、缓存键、证券规则或部署角色。
- 回退本次目录变更时，需将调用方与文件位置一起回退；用户数据和数据库无需迁移。

<a id="layout-7-当前内容保留规则"></a>
### 7. 当前内容保留规则

2026-10-24 清理开始时，远程`main`为`cc837b95f471e5b299bf5e8618332d500bff7e3a`、
应用0.4.17。清理以该主线、当前需求和统一目标为依据；完整本地变更已纳入
[整合记录](../../reports/production-integration-20261024/verification.md)，
后续主线版本与标签由CI分配并校验。

- 保留真实发布单元、全部现有业务源码/测试、依赖锁、配置模板、部署/升级入口。
- 保留203条需求、系统目标、三步计划、领域契约和数据源中立路线。
- 保留当前生产、schema兼容、未解决调查、阶段签收和目标设计引用的原始证据，
  详见[报告索引](../../reports/README.md)；不按日期直接删除。
- 已被上述规范替代、无当前入口引用的旧过程回执，及重复全文/源码快照移出工作区。
  原图/SQL等目标依据已经在`reports/design-inputs-20261023/`留唯一仓库副本。
- 离线交付只保留当前统一系统目标阅读包，新增交付不再嵌套复制整个仓库。
- 缓存、调试临时产物和分享副本不进入源码。删除前核对引用和归属，记录路径、
  哈希、原因与可恢复位置；用户运行数据、凭据、Git历史及未归并业务修改独立保护。

本次移出内容及验证见[清理记录](../../reports/repository-cleanup-20261024/verification.md)。
2026-10-23的100文件迁移清单继续作为当时事实保留；其中后续清理项按新记录解释，
不得把旧清单当作当前目录的存在性保证。

<a id="engineering"></a>

<a id="engineering-生产系统工程规范"></a>
## 2. 从需求到生产的工程闭环

更新日期：2026-10-23。本文适用于当前仓库及后续系统目标建设。
目录规则见[项目布局](#layout)，领域红线见[贡献指南](../../CONTRIBUTING.md)，
当前实现和目标分别见[当前架构](../architecture.md#current)、[系统目标](../architecture.md#target)。

<a id="engineering-1-工程与运行边界"></a>
### 1. 工程与运行边界

| 单元 | 当前源码 / 发布输入 | 职责与依赖 |
| --- | --- | --- |
| 后端应用 | `backend/app/`、pyproject/uv.lock、原生Cargo锁 | API、Platform、Gateway和四类Worker复用领域代码；不同角色不是七份源码 |
| 前端应用 | `frontend/src/`、package/pnpm锁 | 一个完整产品外壳，按模式/角色授权；生产dist随API或桌面包托管 |
| MCP适配 | `mcp-server/` | 独立包和版本，复用已开放HTTP授权，不扩大交易能力 |
| 原生/桌面 | `backend/native/`、`packaging/` | 桥接由目标架构构建；安装、覆盖升级和数据保留独立验证 |
| 部署编排 | `deploy/`与根Docker/Compose | 独立单主写与完整多角色分别部署；真实配置不进入文档/镜像 |
| 自动化 | `scripts/`、`backend/scripts/`、`tests/tooling/` | 调用现有契约，有界验收/维护/研究；无第二套业务实现 |

独立模式是一个API/调度主进程，允许受控计算子进程。完整多租户是Platform、
API、Gateway、Research/Realtime/Notifications/Scheduled四类Worker，另有PG、
Edge与主机Broker/临时Compute。API默认四进程，16GiB配置两进程；当前Gateway
单副本、Platform单写。不能将“一实例”推广成完整部署只启动一个进程。

新增组件尚未实现，不为Kafka/Flink/CH/TOS/Ray创建空服务或不可执行生产配置。
真实独立发布单元形成后才扩顶层包。

<a id="engineering-2-需求到生产的工作闭环"></a>
### 2. 需求到生产的工作闭环

1. 固定需求编号、阶段、触发/输入/输出/权限/失败行为及当前基线；记录未签收项。
2. 沿页面/API→服务→领域→仓库/Provider确认调用链、缓存和现有测试。
3. 设计接口、单位/时间/空值、状态/幂等、依赖预算与回退。二开先确认真实扩展入口。
4. 在隔离数据/依赖中建立失败或一致性样本；实施最小接线并同步所有调用方。
5. 执行对应测试矩阵和工程检查，记录实际命令、退出码、失败/跳过与未执行范围。
6. 在同一变更中更新需求、方案、开发、测试、运营和运维，见[变更映射](#trace)。
7. 按现有PR复审与CI进入固定产物候选；产物、目标平台、恢复和观察通过后独立放行。
8. 上线记录提交/镜像/schema/配置/数据版本与增量水位，反馈闭环回到需求或缺陷。

代码已实现、自动化通过、产物可发布、生产已部署分别有证据。
文档整理不替代真实Provider、目标平台、持续业务或万人验收。

<a id="engineering-3-后续目标的实现落点"></a>
### 3. 后续目标的实现落点

下表是工程归属规范，**不是已存在的新API**。

| 目标 | 归属 | 必须连同交付 |
| --- | --- | --- |
| Kafka采集日志 / fencing | 数据接入与Platform现有调用链 | 标准化、capture/epoch、持久确认/满盘、旧主拒绝、双路同输入对照 |
| Flink统计/受控规则 | 经批准的流计算适配及对应部署 | 与现有领域语义对照、预热/生效边界、checkpoint、乱序/缺口/副作用 |
| Redis版本缓存 | 后端读模型适配 | 完整版本发布、分页同版、丢缓存恢复；不作唯一账务/任务 |
| CH/TOS与历史目录 | 仓库/历史输入适配及对应部署 | 八表/段/manifest、导入台账、各读副本屏障、引用/许可/恢复 |
| 跨节点任务 / JetStream | multitenancy与持久任务/Outbox链 | attempt/epoch/权限/租约/取消在输入和提交边界复查 |
| HA / PgBouncer / Cell | 现有状态层、可信路由与部署 | 事务/RLS/领域锁、专用监听、连接总预算、同步水位/旧主隔离 |
| 强Compute / 条件Ray | 主机输入准备/Broker/受限执行器 | 无网络无凭据、硬限额、兼容探针、进程树回收、旧结果拒绝 |
| 前端目标页面 | pages/components/lib及实际扩展注册 | api.ts/查询键、质量/成本/状态、身份切换、窄屏及对应测试 |

规模化只能调整存储/调度，不改变原价/复权、单位、交易日、历史可得性、费用、
T+1、订单占用、同周期仲裁、因子统计和任务取消语义。

<a id="engineering-4-配置锁文件与数据迁移"></a>
### 4. 配置、锁文件与数据迁移

根`.env.example`是启动模板，用户已存偏好和密钥按[配置规则](../operations.md#configuration)读取；
新增目标参数在实际实现前不进入可用配置模板。生产记录脱敏配置摘要，不记录凭据。
Python、前端、Cargo依赖分别使用冻结锁，原生产物核对版本/来源/接口，不跨架构复制。

数据库有序迁移继续在`backend/app/cell/sql/`，已发布SQL校验和不重写。
新字段优先expand/contract；旧新应用共同读取时不提前contract。JSON/Parquet缺列、
合法已存关闭值和旧模式保持兼容。模式切换不能自动合并或删除用户资产。

<a id="engineering-5-目录文档与ci门禁"></a>
### 5. 目录、文档与CI门禁

```bash
make check
make test-tooling
# 按改动范围：
make test-backend
make test-frontend
make build-frontend
```

`make check`使用标准库检查目录、Markdown本地链接/锚点/围栏、模块/需求唯一编号、
版本和diff空白；不访问网站/供应商，不启动或修改业务数据。
CI后端质量门执行同类目录/文档检查与整个`tests/tooling/`。
工作流变更另外使用`actionlint -shellcheck= -pyflakes=`检查Actions表达式及上下文范围；
一次性测试`DATA_DIR`在步骤中由`$RUNNER_TEMP`写入`$GITHUB_ENV`，
不能在Job级`env`引用`runner`。该语义检查不替代远程执行或Shell/Python验证。
脚本迁移同步模块import、根路径定位、运行目录拷贝和CI；文档迁移同步相对链接与当前命令。
历史报告保留当时的命令、数字和版本，仅维护可导航的本地超链接。

<a id="engineering-6-发布恢复与增量保护"></a>
### 6. 发布、恢复与增量保护

固定源码SHA、产物digest、schema、数据/规则版本、epoch/offset和备份水位。
影子对照关闭通知/订单副作用；灰度范围内只保留一个副作用执行者。
发布阈值由验收计划事先固定，超限停止推广并保留现场。

仅代码可兼容时切回旧产物，保留当前数据。新写或新账本与旧程序不兼容时，先排空/停写，
保存当前完整数据、增量与水位，在隔离环境验证前向修复或携带增量恢复，再切换和对账。
不能默认恢复发布前快照丢弃新增成交、权益、退出意图或任务。Cell反向迁移递增epoch，
依序源fencing→目录CAS→目标enable。数据重放不自动重发历史通知或新建订单。

本地/功能分支不递增应用版本；远程main集成后CI同步一次并固定质量门SHA。
发布和生产操作见[发布流程](../release-operations.md)、[运维手册](../operations.md)。

<a id="build"></a>

<a id="build-构建清单与离线升级备份"></a>
## 3. 构建身份与离线升级材料

构建清单把源码提交、应用版本和文件 SHA-256 关联起来；升级备份保存二开差异及
人工合并材料。工具不修改索引、合并分支、恢复运行代码或重启服务。
Python 3.9+ 可运行，使用标准库；本地修改不递增应用版本。

<a id="build-构建身份"></a>
### 构建身份

| 产物 | 清单位置与内容 |
| --- | --- |
| Docker API | `/app/build-manifest.json`：app、static、tiers、VERSION、锁文件、原生 wheel 与核验脚本 |
| Compute | 先验证基础镜像清单，再以当前源码重建清单；构建层先删除旧 app，避免已删除模块残留 |
| 桌面 | 包内 `source-manifest.json`：参与打包的源码指纹、Git SHA、工作区 dirty 状态 |
| 桌面安装包 | `<安装包名>.manifest.json`：安装包 SHA-256、完整运行目录文件索引和源码身份 |

`format=1` 的清单包含 `scopes`、每个文件的类型/大小/哈希和整个文件索引的
`files_sha256`。内部相对符号链接只记录链接目标，外部或悬空链接拒绝。
不扫描运行数据、`.env*`、Git、日志和缓存。Docker 的依赖闭包由 `uv.lock` 固定，
清单不声称覆盖整个基础操作系统或安装后的所有第三方依赖；原生 wheel 保留并核验。

Docker CI 把实际通过质量门的 SHA 传入 `SOURCE_COMMIT`，与镜像 label 相同。
本地构建不传时记录 `commit=null`，不会把未知源码冒充已提交版本。
传入 SHA 是构建者声明，容器构建中没有 Git 可供独立认证；应结合干净检出和镜像
digest 使用。桌面从实际 Git 检出读取 SHA 和 dirty 状态；发布封装检查拒绝未知、
dirty 或与 workflow SHA 不同的源码。散列不是数字签名，清单应从可信发布渠道取得。

本地示例：

```bash
python3 scripts/build_manifest.py create --root . --profile source \
  --version-file VERSION --git-root . --output .dbg/source-manifest.json

docker build --build-arg SOURCE_COMMIT="$(git rev-parse HEAD)" -t tsp:candidate .
docker run --rm --network none --entrypoint python tsp:candidate \
  /app/build_manifest.py verify --root /app --manifest /app/build-manifest.json
```

`create` 不覆盖已有输出。源码配置只索引构建输入；额外部署文件可通过显式
`--include <相对路径>` 创建清单。桌面外置运行清单的根是包含 `TSP/` 或 `TSP.app/`
的目录；在该目录核验，不能以 `.app` 内部子目录为根。

`verify` 返回 `changed`、`missing`、`added`：无差异退出 0，有差异退出 2，
输入无效、越界或读取失败退出 1。读取时验证内容哈希，不依赖 mtime 缓存。
清单最多 100,000 文件；此操作是显式离线工具，不进入启动、HTTP 或行情热路径。

<a id="build-git-二开升级材料"></a>
### Git 二开升级材料

```bash
# 只读预演，保留原命令
python3 scripts/upgrade_check.py <目标Tag或commit>

# 可选：写入一个尚不存在的备份目录
python3 scripts/upgrade_check.py <目标Tag或commit> --bundle .dbg/upgrade-review
```

`report.json` 记录共同基线、当前/目标完整 SHA、双方改动路径、可识别的文本冲突、
排除路径及各备份文件哈希。`sources.zip` 分别保存 `base/`、`current/`、
`target/`、`index/`、`working/`，二进制内容原样保存；删除通过对应版本无条目表示。
文件名含空格或中文可用。三个 patch 分别是基线至当前提交、当前至暂存区、
暂存区至工作区；未跟踪的源码在 `working/` 中保存。

备份限定已声明的源码/文档目录；数据、密钥常见路径、构建输出、日志和符号链接
不作为源码材料。它不是用户数据备份，也不能识别误写在普通源码中的秘密；
生成物含私有二开代码，不应提交或自动上传。最多 5,000 路径、256 MiB 内容，
超限或已有未解决索引冲突会失败。源码、索引或 HEAD 在备份期间变化时，不生成
完成标记。备份目录必须新建；POSIX 使用 0700，Windows 仍受目录 ACL 管理。

三方文本预演只针对**已提交的树**；未提交修改虽已备份，仍应在临时分支形成提交后
重新预演。二进制重叠、重命名语义和金融口径都需人工复核。没有文本冲突不表示
兼容。需要恢复时，在当前提交的独立检出中先核对哈希，再依次审核并应用暂存/
工作区 patch，检查 `working/` 中的新增文件；不要把归档整体解压到运行目录。

<a id="build-已安装产物的差异备份"></a>
### 已安装产物的差异备份

保留旧清单及可信候选清单，把安装目录作为只读输入，在升级前运行：

```bash
python3 scripts/build_manifest.py backup --root <旧安装目录的清单根> \
  --manifest <旧清单.json> --candidate <候选清单.json> \
  --output <新的备份目录>
```

按旧清单的范围扫描本地变更，并生成 `local-files.zip`、两份清单和 `report.json`。
目录必须在被扫描范围外，拒绝符号链接输出路径；单次备份最多 256 MiB。
本地删除在报告保留 tombstone；本地符号链接仅保存目标文本，不自动重建链接。

| 分类 | 含义 |
| --- | --- |
| `candidate_unchanged` | 候选保留旧基线，本地有修改 |
| `local_added` / `local_deleted` | 本地新增 / 本地删除且候选仍是旧基线 |
| `matches_candidate` | 本地内容与候选一致 |
| `conflict` | 本地与候选均偏离旧基线，须人工处理 |

`report.json` 最后写入，只有 `complete=true` 才代表备份流程完成。扫描或复制期间
文件变化会失败；未完成目录可保留用于排错，不能当作恢复依据。它不是文件系统
事务快照，升级时应先停止源码写入。旧/候选原始文件可按记录的 SHA 从可信源码或
原始安装包取得；只有哈希的清单无法还原旧文件字节。

生产恢复通过二开源码合并、完整回归和重新构建固定镜像/安装包完成。数据与配置按
[发布流程](../release-operations.md)单独备份和恢复，切换后验证 readiness、
登录、核心业务与源码/镜像身份。工具不提供在线覆盖程序或一键重启入口。

<a id="collaboration"></a>

<a id="collaboration-多人协作与版本管理"></a>
## 4. 多人协作与远程权限

`main` 是唯一集成主线。每项任务使用独立短期分支；多人或多个 AI 同时开发时，
各自使用 clone 或 worktree，不能在同一工作目录切换分支或覆盖对方的未提交内容。
分支版本表示起点，交付身份必须同时包含应用版本和完整提交 SHA。

<a id="collaboration-开发与合并"></a>
### 开发与合并

从最新 `origin/main` 建立 `feat/<任务>`、`fix/<任务>`、`docs/<任务>` 或
`chore/<任务>`，一个分支解决一个问题。共享分支通过普通 merge 更新，不 rebase 或
强推他人的提交；个人尚未共享的提交可本地整理。已归并的分支不再用于新任务。
删除分支前核对 PR 已归并且工作目录没有尚未提交或未推送的工作；不自动清理现场。

本地提交和功能分支推送不改应用版本。PR 会检查实际 head 与其和 main 的共同祖先
的 `VERSION` 一致，防止人工加号或旧分支带回旧号。main 前进不要求纯业务分支为
更新版本主动改十个文件；合并最新 main 后，分支自然继承新的版本基线。
发生版本元数据冲突时，以最新 main 的版本为基准，同时保留本任务新增的依赖及
正文；不能对整份锁文件或文档直接使用 ours/theirs。

普通贡献者通过 PR，至少一次独立复审、所有讨论解决、最新版本通过
`version`、`backend`、`frontend` 检查后归并。严格检查要求分支更新到最新 main；
提供 `merge_group` 检查入口，仓库开启 merge queue 后可使用，当前不宣称已开启。
允许 merge 与 squash，不允许 rebase merge 反复搬运多人共享历史。
管理员可仅在 PR 上处理单人维护时的复审例外，不能绕过质量检查或直接推 main。

<a id="collaboration-远程规则与权限"></a>
### 远程规则与权限

规则模板位于 `.github/rulesets/`，文件存在不等于远程生效：

| 规则 | 范围 | 权限 |
| --- | --- | --- |
| `main-history` | main 禁止删除和非快进 | 无 bypass，包括自动化 |
| `main-integration` | PR、独立复审、讨论解决 | 管理员仅 PR 例外；版本写回的自动化例外 |
| `main-quality` | 最新 main 的三个必需检查，绑定 Actions App | 人员无 bypass；版本写回的自动化例外 |
| `version-tags` | `v*` 禁止更新和删除 | 无 bypass；不禁止创建 |

自动版本写回发生在质量门前，因此不能对它要求预先存在的同 SHA 检查。
自动化 bypass 必须限于集成与质量规则，历史和标签规则始终独立生效。
GitHub App 例外覆盖该 App 的所有 workflow，不能限制到某个 Job；工作流变更应
独立复审，默认 token 只读，仅 version/tag/发布步骤获得必要写权限。
个人仓库是否允许内置 Actions App 成为 bypass actor，以实际 API 结果为准。
若平台拒绝，保留已生效的历史和标签保护，不套用会让版本写回失败的规则；
PR/复审/质量规则需专用已安装 GitHub App 才能完整生效。不可因此给所有开发者
直接写 main 的 bypass，也不可把模板描述为已强制执行。

远程核验：

```bash
gh api repos/jelven007/TSP/rulesets
gh api repos/jelven007/TSP/rules/branches/main
git ls-remote --tags origin 'refs/tags/v*'
```

实施结果与平台限制见本次验收报告，不根据旧聊天或模板猜测远程状态。

版本格式、计数、重跑、自动标签与固定 SHA 规则统一见[CONTRIBUTING 第10.1节](../../CONTRIBUTING.md#101-主线版本规则)。日常支持保留最新版本与最近五个历史版本，源码标签仍不可变；源代码回退通过新 PR，生产回退固定旧 digest。恢复标签、清单与缺失历史标签的处理见[版本保留](../release-operations.md#31-版本保留)。

<a id="trace"></a>

<a id="trace-本地变更与全生命周期追踪"></a>
## 5. 需求与生命周期追踪

更新日期：2026-10-25。本次基线为 `31cf1a553dbc806aac6e6f93f30f4f41b646aa3e` / 0.4.19，
全部有效本地修改的范围和验证见[本批记录](../../reports/current-state-release-20261025/verification.md)。

当前主手册163FR+42NFR=205条，保留原203编号并新增FR-UI-05/06。
本批逐项归属包括策略结果归档、全局资源预算、目录仅读同步、两步向导、
标准弹窗及研究界面；原始153文件冻结，独立整合分支验证后经PR归并。
文档责任/事实源/每月复核与漂移信号见
[文档清单](../../reports/current-state-release-20261025/verification.md#文档事实源与更新责任)。
本地与线上均从同一通过质量门的提交更新Runtime/Compute及Broker，
每个阶段独立核对版本、静态资源、完整功能健康、配置和业务数据保留。
下列2026-10-23/24记录保留当时的范围和结果：
逐文件新增、修改、删除、迁移和SHA-256清单见
[整合证据](../../reports/production-system-consolidation-20261023/verification.md)。
现状核对基线为`cc837b95f471e5b299bf5e8618332d500bff7e3a`，应用版本保持0.4.17。

2026-10-24补充：上述清单是2026-10-23冻结快照；随后按用户要求清理被替代的旧材料、
重复交付与缓存，当前文件变化以[清理记录](../../reports/repository-cleanup-20261024/verification.md)
及其移出清单为增量。需求/系统目标正文保持，原全生命周期同步结论不代表被清理文件仍存在。

本轮完整逐文件清单、最终验证及提交前复审以
[2026-10-24整合记录](../../reports/production-integration-20261024/verification.md)为准。
旧清单中的业务“未改动”结论仅适用于其原时点；本轮包含下列已存在的业务修改。

<a id="trace-1-状态与责任"></a>
### 1. 状态与责任

“已同步”只表示文档覆盖本地变更；“已实现”依据源码/契约；
“通过”必须带本次实际测试/环境/版本证据；生产状态见[版本基线](../version-baseline.md)。
下表维护角色是职责，未指定实际人员；小团队可兼任，发布前记录实际责任人。

| 变更组 / 责任 | 需求 | 方案 / 开发 | 测试 | 运营 / 运维影响 |
| --- | --- | --- | --- | --- |
| 工程目录 / 开发负责人 | NFR-01/04/06/10 | [布局](#layout)、[生产工程](#engineering)、ADR0001/0002 | tooling、脚本import/CLI、CI、文档门禁 | 外部作业同步新路径，模式/端口/数据目录不随源码迁移 |
| 需求基线与八模块 / 产品负责人 | 全163FR、42NFR | [需求总纲](../requirements.md)、[当前索引](../architecture.md#capabilities) | 保留203个原编号、新增FR-UI-05/06；唯一性、状态与链接 | 用户说明只写当前能力，阶段/生产/目标独立 |
| 融合系统目标 / 架构负责人 | FR-REALTIME/STATS/HISTORY/SCREEN/BACKTEST、NFR-23–42 | [系统目标](../architecture.md#target)、[二期计划](../planning/phase2-implementation-plan.md) | A–I、3.1–3.6联合矩阵，未实现项未执行 | 容量/成本/可靠性目标未签收，轻量不强装新组件 |
| 数据能力与配置说明 / 数据负责人 | FR-DATA/EXT/ADMIN、FR-CORP | [Provider](integrations.md#plugin)、[公司行为](trading-ai.md#corporate)、[配置](../operations.md#configuration) | 能力/日期/账务/旧模式/配置测试 | usable不认证日期；失败保护旧结果，变更源配置拒绝旧请求 |
| 策略、因子与研究行为补齐 / 研究负责人 | FR-STRATEGY/SIGNAL/FACTOR/MINING/BACKTEST | [策略](research.md#strategy)、[因子](factor-system.md#factor-current)、[研究](research.md#research) | 原价/费用/T+1/固定输入/样本外/候选门槛/缓存 | 研究产物不等于收益承诺；缺历史/时点证据显式受限 |
| 目标股票与自选导入 / 应用及研究负责人 | FR-STRATEGY-06、FR-WATCHLIST-02/03、FR-ADMIN-06 | targets/config→快照→Polars/矩阵/监控；角色并发与CSV新组事务，L3 | 目标/ST/ETF、动态成员与旧历史、真实SQL隔离/回滚/竞态、API权限、前端两档/多选/CSV/删除提示 | 本地候选未发布；API/Worker/Compute须同版，回退前停用新目标策略，禁止直接删除被引用分组 |
| 监控、模拟与AI说明 / 业务负责人 | FR-MONITOR/PAPER/CORP/AI/REVIEW | [模拟交易](trading-ai.md#paper)、[会话](trading-ai.md#sessions)、功能手册 | 周期仲裁、权益/占用/幂等、确认/取消、会话冲突 | 触发/投递/成交分开；恢复不重放写动作，SMTP配置只读 |
| 验收/性能/维护脚本归档 / 对应开发与测试 | NFR-14–17/22/28、相关FR | backend/scripts六分类、scripts三分类及各README | 所有受影响脚本测试、沙箱/打包/拷贝路径 | 从backend用新模块名；主机已安装maintenance脚本不自动改名 |
| 工具测试/工作流 / 发布负责人 | NFR-04/06/10 | Makefile、pyproject测试发现、CI/Windows工作流、版本工具 | tooling版本/工作流、路径/包配置回归 | 质量门SHA与产物一致；本地不加号，远程规则仍需实际核验 |
| 运行配置模板 / 应用与运维 | FR-ADMIN-04–06、NFR-03/15/20/21 | .env.example、配置/部署/Windows规范 | 字段读入口、角色/预算及默认兼容 | 仅模板/说明变更，不覆盖已存值、不修改真实配置 |
| 归档/构建范围 / 发布负责人 | NFR-01/04/06/10 | .gitignore/.dockerignore、历史报告与截图归reports | 生成物排除、引用/素材路径、目录门禁 | data/备份/deliverables/缓存不进入Git或镜像 |
| 恢复与服务运营 / 运维和运营负责人 | NFR-08/16/28/31–42 | 运维/发布、[服务运营](../release-operations.md#service-2-开通与用户生命周期) | 现有恢复脚本回归；新HA/灾备是目标 | 保护新写，SLO/RTO/RPO分档，不把观察已启动写成终态通过 |
| 首次同步与调度 / 数据负责人 | FR-ENTRY/ADMIN/DATA、NFR-03/15 | preferences → initial_sync/integrity/depth → scheduler；financial run_now/发布；settings API/数据总览 | 缺省开启、已保存关闭、改时间保持暂停、初始化重试/单飞、财务线程/互斥/写锁发布、完整/独立模式 | 新安装自动创建首次同步；实时/分钟/扩展/秒级分别控制，遵守能力与执行窗口；财务周期检查总开关，默认启用不承诺立即有完整数据 |
| 管理员/认证启动 / 应用负责人 | FR-ENTRY/AUTH-LOCAL/ADMIN、NFR-09/20 | bootstrap_defaults、auth、Cell admin、full lifespan、loopback cookie | 哈希/幂等、旧账户/改密保留、失败关闭、跨平台默认、真实SQL权限 | 非Windows默认api/full需双库/平台/Worker；远程HTTPS与本地HTTP会话分别处理 |
| 邮件配置与认证 / 应用和运维 | FR-ADMIN/MONITOR/REVIEW、NFR-09 | 环境 → 旧系统配置 → Cell；共享SMTP AUTH LOGIN | 未配置/缺密码、优先级、无秘密响应、LOGIN及通知/注册收件人 | 实际授权码仅受控配置；本地旧值已保留，不携带进Git/镜像，新部署必须配置 |
| 菜单/亮色/数据源设置 / 前端负责人 | FR-UI/DASHBOARD/DATA/ADMIN、NFR-02 | theme首屏、Layout/MenuSettings、capabilities权威顺序、DataSources派生列表 | 前端全量/lint/build、路由冲突、既有偏好和页面状态 | 行情/环境/信号指标/模拟交易/数据总览等名称更新；设置源rustdx→fuyao→自定义，保留旧TickFlow生效路由 |
| 分析材料/版本保留 / 发布负责人 | 全FR、NFR-01/04/06/08/10 | 页面血缘分析归reports，Excel归deliverables；最新+5恢复点 | 相对链接、203编号、文件清单/哈希、恢复归档 | 文档快照与当前行为分别维护；不删除Git历史、冻结标签或生产备份 |
| 官方个股目录 / 数据负责人 | FR-DATA-04/05/08、FR-ADMIN | [当前架构](../architecture.md#current)、[目录契约](market-data.md)、instrument_sync/catalog及管理API | 官方响应完整性、CRUD/权限、SCD2、原子写失败、缓存发布、启动及09:00调度 | 固定官方源，不受行情总开关控制；旧时间偏好失效；手工修订可能被后续官方名单覆盖 |
| 管理员个人空间 / 应用负责人 | FR-ENTRY、FR-ADMIN、NFR-09/20 | identity预留→持久任务→业务初始化→默认空间激活 | 真实PG并发/容量/双库重试、停用保护、租户隔离、启动接线 | 已有租户与密码保留；失败时管理登录仍可用，业务待空间就绪 |
| 顶栏与数据总览 / 前端负责人 | FR-UI-05、FR-DATA-07 | PageHeader、Data、Watchlist弹层及共享状态查询 | Vitest/build/lint、桌面与窄屏、浅深色、搜索弹层和目录管理 | 顶栏统一；数据页分盘前/中/后，旧按钮与筛选保持可用 |
| 示例与源码恢复 / 工程负责人 | NFR-01/04/06/08/10 | examples统一、scripts/release/source_recovery.py | 目录/链接、样例接口、真实Git祖先/标签、损坏/越界/覆盖拒绝 | 只归档固定源码，保留最新+5；生产数据与依赖另行备份 |

<a id="trace-2-全部文件的追踪方式"></a>
### 2. 全部文件的追踪方式

逐文件清单读取`git diff HEAD`和未跟踪工程路径，删除与新路径同时记录，不忽略
未跟踪模块/报告/工具。每个文件关联本表变更组、需求范围和六类生命周期入口。
哈希标识实际本地文件；相对HEAD的修改不是新提交SHA，不能冒充可部署产物。

历史验证报告保留原版本、计数、PID与窗口。历史hyperlink可维护；旧离线HTML/ZIP
交付不改写为新结论，已被替代的交付按当前[保留规则](#layout-7-当前内容保留规则)移出。
目录迁移清单映射当时来源与目标，读取旧报告复跑时使用当前脚本README。
2026-10-23的34份专项文档清单是历史快照；当前正文合并、消重和迁移以[本轮记录](../../reports/document-consolidation-20261024/verification.md)为准。

<a id="trace-3-本次完成与未完成"></a>
### 3. 本次完成与未完成

本次交付范围：工程/示例目录整理、204条需求结构化、全生命周期文档同步、
目录/文档自动门禁与适用回归、逐文件证据和规范包。
本轮包括上述初始化、认证、展示与调度业务变更；没有执行数据库迁移或生产切流。

二期第二步持续/完整交易日终态仍待，第三步未开始。
CH应用接入、Kafka/Flink/Redis/TOS、多节点HA/强Compute、Cell及万人能力仍为目标。
Win10真机、安装包、真实数据覆盖/许可/时钟和区域恢复另行签收。
实际已执行检查与未执行范围仅以本次[验证报告](../../reports/production-baseline-20261025/verification.md)为准。

<a id="trace-4-后续维护规则"></a>
### 4. 后续维护规则

每项变更记录需求/阶段→设计→文件→测试→用户/运营/运维影响→证据。
金额/数量/权限/历史时点/保留/部署/恢复改变时单独列兼容和失败处理。
标“无影响”也说明核对范围，禁止仅改功能手册而遗漏需求、配置或Runbook。
归并前核对整个diff；追加提交后重新复审全部变更和上一轮阻断项。

本轮清理对应：需求与方案范围不变；开发更新目录保留规则和忽略项；
测试更新证据索引、目录门禁并执行文档/工具回归；运营只引用当前规范与目标阅读包；
运维保留生产/恢复/未解决调查和当前Windows前置要求，不触碰运行数据或部署。

<a id="decisions"></a>
## 6. 工程决策记录

| 决策 | 状态与日期 | 决策及后果 |
| --- | --- | --- |
| ADR0001：按发布单元与职责组织仓库 | 已接受，2026-10-23 | 保留backend/frontend/MCP，脚本分类、tooling集中、SQL随包；部署角色不拆重复源码；文档/证据目录细化由下一项承接 |
| ADR0002：文档分层与需求追踪 | 2026-10-23接受，2026-10-24更新正文组织 | 保留203编号及六类生命周期追踪；八需求模块改为单手册内章节，专题文件合并为六份附录，历史证据统一归reports |
| ADR0003：示例和恢复材料分离 | 已接受，2026-10-25 | 可执行示例统一到examples，源码恢复工具归scripts/release；六份归档只由不可变主线版本生成，不修改Git历史或混入运行数据 |

本轮更新的理由是分目录仍留下重复正文和多套维护入口。需求、方案、开发、测试、
发布运营和运维各有一个主入口；操作说明书只描述当前用户行为。
同一规则在唯一归属维护，其他章节引用；技术字段/示例不能只剩摘要。
变更同步链接、锚点、代码内文档入口及检查器；不提供重复正文转发文件。
回退时文档位置与检查器一起恢复，业务数据不迁移。
