# 项目目录优化方案

日期：2026-11-03。状态：已按用户确认的方案完成目录迁移与本地验证；未提交或发布。

## 1. 结论与范围

保留现有业务包与启动路径，先整理历史报告、补齐目录导航和新增文件归属。
第一批将本方案附录中的118个报告条目、649个已跟踪文件归入六类目录。
现有主手册、当前任务、用户交付、运行数据及恢复点保持原位置。
目录优化不改变业务功能、策略参数、数据库、依赖锁或应用版本，也不启动回测或部署。

选择历史批次是为了避开正在修改的内容，不代表旧资料可以删除。
未完成调查和失败记录保留；分类以内容职责为依据。
实施前重新核对文件状态和任务引用，出现新的并行修改时暂缓对应整组迁移并记录原因。

## 2. 已核对的现状

以下数字属于本次盘点快照，不能用来替代实施后的重新检查。

| 区域 | 实际情况 | 本次处理 |
| --- | --- | --- |
| 仓库根 | 44个可见及隐藏条目；17个已跟踪的根文件 | 保留启动器、配置与项目入口，说明生成目录归属 |
| 业务包 | `backend/`、`frontend/`、`mcp-server/` | 保持独立包和发布边界 |
| `reports/` | 201个顶层条目、885个已跟踪文件，涉及178个已跟踪顶层条目 | 将明确列出的历史材料按用途分组 |
| `deliverables/` | 33个顶层条目，约838 MiB，均未纳入Git | 补齐索引；现有交付路径保留，规范新增交付 |
| `backend/tests/` | 466个已跟踪文件，其中414个Python文件直接平铺 | 提供领域定位表；既有测试不批量搬迁 |
| 工程脚本 | 根`scripts/`与`backend/scripts/`已按职责区分 | 保留CLI、Python包及CI调用路径 |
| 并行开发 | 23个工作目录；共享工作区已有业务和文档修改 | 在独立工作目录准备变更，合回时保留已有修改 |

已完成的基线验证：

- `make check`：通过；87个必需路径、37篇维护文档、8个需求模块、版本一致性及diff空白检查通过。
- `python3 scripts/check_docs.py --include-reports`：通过；261篇Markdown及保留需求基准检查通过。
- 候选报告分类目标没有现存路径冲突。
- 没有运行业务测试、前端构建或新回测；这些基线结果不代表目录迁移已经通过验证。

## 3. 方案比较

| 方案 | 内容 | 取舍 |
| --- | --- | --- |
| A：报告归类与入口整理，推荐 | 历史报告实质归类，补齐导航，明确新增交付和测试归属 | 改善资料查找；需要同步相对链接，业务路径不受影响 |
| B：只补导航 | 所有文件原地保留，只完善README | 改动最少，但历史报告仍长期堆积在同一层 |
| C：全面搬迁业务包与测试 | 引入`apps/`、`infra/`等新顶层并批量迁移测试 | 涉及Docker、桌面打包、原生桥接、导入、CI和启动器，当前收益不足以支持范围扩大 |

本次建议执行A。大型业务文件拆分与测试全面重组不属于本次目录整理。

## 4. 目标布局

```text
TSP/
├── backend/                 后端应用、原生桥接、后端脚本和测试
├── frontend/                前端应用和就近测试
├── mcp-server/              独立MCP包
├── deploy/                  部署形态与配置模板
├── packaging/               桌面打包
├── scripts/                 仓库检查、版本、发布和研究工具
├── tests/tooling/            仓库工具测试
├── docs/                    现行主手册及领域参考，入口保持
├── .claude/artifacts/        设计、实施计划和缺陷记录，沿用现有约定
├── examples/                可运行样例
├── brand/                   产品品牌资源
├── screenshots/             用户手册功能截图
├── reports/
│   ├── README.md            当前证据入口与分类导航
│   ├── releases/            历史发布、升级与恢复签收
│   ├── market-data/         行情、数据接入、修复和数据性能验证
│   ├── strategies/          策略、回测与研究证据
│   ├── ui/                  页面、控件和交互验证
│   ├── engineering/         架构、文档、目录与工程决策证据
│   ├── operations/          存储、清理、安全与保留政策证据
│   └── 原有固定及活跃条目    本批保留；原因见第5节
├── deliverables/             本地用户交付，不进入Git或镜像
└── data/                     运行数据，由DATA_DIR管理
```

新报告采用`reports/<分类>/<专题-日期>/`，正文与必要附件在同组保存。
本批历史条目保留原名称和内部结构，不再嵌套一层年份，也不合并不同专题的结果。
每个分类只增加导航README，不重复复制主手册或历史报告全文。

新增离线交付使用`deliverables/{research,releases,operations,designs}/<专题-日期>/`；
仅在实际产生对应交付时创建目录。研究交付仍为一份统一Excel，详细账本保存在同一文件内。
历史Excel与原资金统计不改写；现有交付文件及其用户链接保持。
私有运行回执不因整理而转入Git，交付README不记录凭据或生产访问信息。

临时截图和一次性脚本使用工作区外的任务目录；依赖与工具缓存继续由原工具管理。
本次不删除缓存，也不移动虚拟环境、node_modules、构建输出或编辑器管理目录。

## 5. 固定路径与暂缓范围

以下内容不在本批迁移清单中：

- `reports/production-system-consolidation-20261023/`：`check_docs.py`及`check_repository.py`依赖其中的需求编号基准。
- `reports/single-host/`：部署文档和验收命令仍使用该路径。
- `reports/s01-current/`：当前研究导航入口。
- `reports/online-timeout-20261021/`：未完成调查及历史恢复证据。
- 所有名称包含`202611`的已跟踪报告条目，以及两张`strategy-history-delete-*`截图。
- 所有未跟踪或被忽略的报告条目，包括现有Excel及当前尾盘90秒、启动延时、行情调度、菜单默认值等任务材料。
- `indices-kline-20261028/`、`rustdx-four32-integrity-repair-20261029/`、
  `rustdx-four32-pipeline-20261029/`、`s01-latest-capital-20261031/`：
  组内有未跟踪日志附件，为避免拆散证据整组保留。
- 用户配置、行情、数据库、策略、运行安装目录、生产备份及其他工作目录。

暂缓内容仍从`reports/README.md`按用途导航，不以复制副本或符号链接实现分类。
实施后若没有新增并行文件，本批预计把报告顶层条目由201个降至89个；
新增验证报告等实际产物另外计入，不承诺固定最终数量。

## 6. 调用、文档与测试定位

| 任务 | 权威位置 | 定位和验证约定 |
| --- | --- | --- |
| HTTP与SSE | `backend/app/api/` | 沿API到服务及领域模块，相关测试仍在`backend/tests/` |
| 业务编排 | `backend/app/services/` | 复用仓库和Provider；脚本不实现第二套业务 |
| Provider入口 | `backend/app/data_providers/registry.py:get_provider` | `custom/loader.py:get_provider`仅为自定义源内部查找，不作为通用入口 |
| K线仓库 | `backend/app/tickflow/repository.py:KlineRepository` | 按符号查找对应仓库测试，不从报告反推API |
| 当日池 | `backend/app/strategy/daily_pool.py:build_pool` | 定位`test_daily_stock_pools.py`和调度测试，完整池口径保持 |
| 公共发布 | `backend/app/multitenancy/publication.py:PublicPublisher` | 发布绑定、回收和Compute测试就近搜索 |
| 前端查询键 | `frontend/src/lib/queryKeys.ts:QK` | 页面和组件测试继续就近放置 |
| 工程检查 | 根`scripts/` → `tests/tooling/` | 保持标准库检查和Python导入路径 |
| 后端操作命令 | `backend/scripts/<分类>/` | 继续从backend运行`python -m scripts.<分类>.<模块>` |

目录定位抽查：`KlineRepository`、`PublicPublisher`、`QK`各只有一个定义命中；
`get_provider`有两个定义，需要完整模块路径区分。四项抽查中三项可以仅凭符号唯一定位，
该75%只描述这四个搜索样本，不是全仓质量评分。

源码规模盘点发现前端`api.ts`5132行、后端`matrix.py`4762行、
`StrategyBacktest.tsx`3315行。以1000行/文件、100行/函数作为诊断阈值，
后端有27个文件和135个函数超过阈值；最大函数是`backtest/strategy.py:run`804行。
阈值不是新质量门；目录搬迁不能解决这些职责问题，本次仅记录，不拆分业务实现。
`run/save/load`分别在19/7/3个后端模块出现，不把同名视为重复逻辑，也不批量改名。

## 7. 具体文档改动

- `docs/reference/engineering.md#layout`：更新真实目录树、分类归属、稳定路径与新增文件规则。
- `reports/README.md`及六个分类README：保留现有入口锚点，更新分类导航与迁移后的链接。
- `docs/maintenance.md`：明确报告、用户交付、临时产物和历史清单的处理方法。
- `docs/README.md`、`tests/README.md`、`scripts/README.md`、
  `backend/scripts/README.md`：补齐统一导航和测试定位规则。
- `deliverables/README.md`：按现存交付补齐用途入口，区分历史成果与当前状态。
- 根README、贡献指南、现行主手册、部署说明及`.claude/artifacts/`中受迁移影响的引用：
  只更新具体链接和目录说明，保留并行任务的内容。

链接按原文所在目录解析目标，再根据迁移表生成新相对路径；
同步迁入文件对源码、文档和其他报告的链接，保留锚点、查询片段与中文文件名。
不使用全仓字符串替换修改历史命令、日期、收益、测试计数或部署结论。
历史JSON、HTML、CSV和冻结清单原样保存，其中的原路径仍是历史事实；
另记迁移前后路径和哈希，不让新位置冒充原采集位置。
修改了导航链接的Markdown记录新旧哈希，二进制附件验证内容哈希不变。

## 8. 执行与验收

1. 在独立工作目录准备目录及链接变更；记录基线HEAD、文件状态、完整迁移表和哈希。
2. 重新检查本附录每组内容：有并行修改、未跟踪附件或活跃任务引用时整组暂缓，不覆盖。
3. 按明确的旧→新路径迁移，目标已存在、大小写冲突、悬空链接或越界时停止该组。
4. 同步入口和相对链接，审阅所有差异；历史数字与业务源码不得混入本次修改。
5. 执行`make check`、`python3 scripts/check_docs.py --include-reports`、
   `make test-tooling`和`git diff --check`；核对二进制附件哈希、迁移清单及目录统计。
   只有实际修改前端或业务代码时才追加对应构建/业务测试，本方案不包含此类修改。
6. 将本次目录变更合回共享工作区前重新读取文件；只应用本次差异，
   当前文件变化时按最新内容重做局部链接改动，不整体覆盖。
7. 交付实际目录树、迁移/暂缓清单、验证结果和回退方法。
   未通过或未执行的检查明确列出；本地不递增版本，不自动提交、推送或发布。

回退按本次清单恢复文件位置与对应导航差异，先核对迁移后是否有新增修改。
不使用`git reset --hard`或覆盖整个工作区，也不迁移或恢复任何用户数据库。

## 9. 实际执行结果

118个历史条目、649个文件已迁入六类，未额外跳过本批条目。
实施快照顶层205项；其他任务随后新增一项，应用前206项、应用后94项。
488个非Markdown附件原样保持；53篇迁入Markdown仅更新导航链接。
现有业务修改、用户交付、固定基准及活跃材料保留，版本保持`v0.4.46`。

当前项目`make check`、269篇Markdown的全报告链接检查和diff检查通过；
PATH无`uv`使`make test-tooling`未启动，已有冻结Python执行同一测试集，
165项通过、0失败。未运行新回测、构建或部署。
完整迁移/保留清单、哈希、验证及回退方法见
[迁移验证](../../../reports/engineering/repository-layout-20261103/verification.md)。

## 附录：第一批精确迁移清单

规则：以下每个条目的`reports/<原名>`迁至`reports/<对应分类>/<原名>`。
目录连同内部已跟踪内容整组迁移；单文件保留原文件名。没有列出的条目保持原位置。
共118个条目、649个已跟踪文件；实施前重新检查并记录实际数量。

### engineering：20项

```text
design-inputs-20261023
document-consolidation-20261024
documentation-refresh-20261030
documentation-sync-20261023
local-consolidation-20261022
main-consolidation-20261018
p2-correctness-port-20261017
p2-remaining-20261017
page-data-analysis-20261024
phase2-architecture-20261016
phase2-step1-20261016
phase2-step2-20261016
prd-completion-20261023
production-baseline-20261025
production-integration-20261024
project-optimization-20261026
project-rename-20261030
system-target-20261023
upstream-comparison-20261017
upstream-ports-20261018
```

### market-data：49项

```text
asset-retirement-20261028
daily-coverage-20201102-20261029
data-collection-unification-20261028
data-status-timeout-20261028
data-sync-disclosure-20261027
ecs-index-cutoff-20261029
eltdx-3.3.0-interface-mapping-20261025.md
financial-backfill-20261029
financial-history-unify-20261030
financial-storage-20261030
historical-calendar-20261026
history-backfill-20261020
index-parity-20261030
local-derived-minutes-20261026
local-kline-description-runtime-20261027
local-minute-june1-16-20261030
may-minute-alternatives-20261030
may-minute-recheck-20261030
minute-1130-20261026-20261028-20261029
minute-301110-20261029
minute-aggregation-rustdx-validation-20261026
minute-backfill-20260330-20261028-20261029
minute-backfill-remaining-20261029
minute-collection-reliability-20261029
minute-coverage-20260330-20261028-20261029
minute-coverage-current-20261029
minute-history-performance-20261028
minute-local-online-20260727-20261028-20261029
minute-local-to-online-20261026-20261027-20261029
minute-range-repair-20261029
minute-task-feedback-20261029
online-extension-pulls-20261028
online-minute-june15-16-20261030
online-refresh-20261027
repair-date-range-20261027
rustdx-business-20261029
rustdx-concurrency-assessment-20261029.json
rustdx-concurrency-assessment-20261029.md
rustdx-concurrency-performance-20261029
rustdx-four-station32-performance-20261029
rustdx-may-99-20261030
rustdx-minute-diagnosis-20261029
rustdx-task-affinity-20261029
rustdx-task-routing-performance-20261029
rustdx-v203-20261028
rustdx-v204-20261028
tdx-daily-full-20261029
unified-minute-source-20261025
weekly-parquet-schema-20261028
```

### operations：23项

```text
cancelled-minute-cleanup-20261028
deliverables-version-cleanup-20261030
document-security-20261031-manifest.json
document-security-20261031.md
ecs-repair-temporary-cleanup-20261028
historical-image-cache-cleanup-20261031.md
image-cache-cleanup-20261031
june-first-half-minute-cleanup-20261030
local-data-cleanup-20261031
local-disk-cleanup-20261031.md
local-disk-governance-20261031.md
local-kline-cleanup-20261027
may-minute-cleanup-20261030
minute-20260327-cleanup-20261029
minute-cache-cleanup-20261027
minute-march-june-cleanup-20261030
project-cleanup-20261028
project-file-cleanup-20261030
repository-cleanup-20261024
strategy-results-cleanup-20261030
trae-prompt-history-audit-20261029.md
trae-prompt-history-cleanup-20261030.md
version-retention-20261031.md
```

### releases：13项

```text
complete-release-20261027-2
complete-release-20261028
complete-release-20261028-2
complete-release-20261028-3
current-release-20261030
current-state-release-20261025
current-state-release-20261026
deployment-20261015-414cc483
kline-release-20261026
latest-release-20261027
online-upgrade-20261018
online-upgrade-20261020
repair-missing-release-20261026
```

### strategies：9项

```text
backtest-a877-exit1125-20261030
default-strategies-five-year-20261017
five-strategy-parallel-20261019
s01-clean-raw-20261031
s01-exit1330-tp4-20261031
s01-latest-rules-full-20261031
strategy-half-resource-20261028
strategy-optimization-20261019
strategy-safety-port-20261017
```

### ui：4项

```text
data-overview-layout-20261027
index-page-ui-20261028
local-watchlist-update-20261027
system-settings-20261027
```
