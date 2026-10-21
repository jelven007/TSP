# 测试与验收指南

本文档定义 TSP 的测试层次、最低门禁和版本验收流程。功能验收依据
[需求基线](./requirements.md)，工程细则依据[贡献与复审指南](../CONTRIBUTING.md)，
被测源码、生产和标签状态依据[版本基线](./version-baseline.md)。

原生 Windows 10 22H2 x64 / 16 GB 的资源检查、同规模缓存基准和全功能阻断项见
[原生资源优化验收](./windows10-native-16gb.md)。
`windows-native.yml` 在 Windows Server 2022 执行 PowerShell 5.1、资源预算和
worker 回归，不能替代 Win10 真机与隔离 Compute 验收；缺少目标环境的测试须标记
“未执行”，不能把跳过或 Compute readiness 错误视为通过。

## 1. 测试目标

测试不仅验证接口能返回，还要证明：

- 金融字段、单位、复权、时点和交易规则正确。
- Provider 替换后，上层仍使用统一契约。
- 文件发布、缓存失效和前端刷新保持一致。
- 网络失败、空数据、缺少能力和部分写入不会产生伪成功。
- 历史配置、策略和数据在升级后仍可读取，或有明确迁移方案。
- 发布产物可以在目标平台安装、启动、持久化和回退。

## 2. 测试层次

| 层次 | 主要范围 | 典型证据 |
| --- | --- | --- |
| 单元测试 | 纯函数、规则、字段转换、日期和费用 | 固定输入输出断言 |
| 契约测试 | Provider、API、schema、OpenAPI 快照 | 成功、空、错误、缺能力 |
| 服务集成测试 | 同步、仓库、缓存、调度、任务状态 | 临时数据目录和完整调用链 |
| 领域回归测试 | 策略、因子、回测、监控、模拟盘 | A 股边界和历史缺口样例 |
| 前端测试 | 页面状态、交互、类型和查询缓存 | Vitest、ESLint、TypeScript 构建 |
| 端到端烟测 | 启动、登录、同步、核心页面、持久化 | 可复现人工记录 |
| 发布产物测试 | Docker 和桌面安装包 | 架构/平台矩阵、哈希和启动验证 |

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
执行完整策略计算及独立恢复验收。`backend/scripts/accept_market_pipeline.py` 可在新的隔离
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
完整产品的 Compute 镜像还必须通过 `scripts/verify_full_compute.py`；该验收包含
`signal_backtest`，用于证明 Compute 层已经按冻结锁安装 `backtest` extra，而不是
只验证不依赖 `vectorbt` 的矩阵回测、优化和挖掘路径。

## 4. 标准命令

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

模拟盘日线 schema 回归：

```bash
uv run pytest tests/test_paper_daily_scan_schema.py tests/test_paper_trading.py \
  tests/test_paper_api.py tests/test_paper_settle_price_time.py -q
```

模拟盘应通过统一日线存储 schema 读取新旧股票、ETF 与指数日线分区，
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
`partial/stale`周期禁止入场。当前相关监控、模拟盘和行情调用链扩大回归为
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
模拟盘事务、迁移与平台边界；发布验收见[一期记录](./phase1-implementation.md)。

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
  uv run python -m scripts.accept_phase2_step1 \
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
  uv run python -m scripts.accept_phase2_step2 \
  --users 100 --rounds 2 --pool-size 32 \
  --ledger-rows 240 --nav-rows 180 \
  --research-concurrency 4 --steady-request-interval 5 \
  --output ../reports/phase2-step2-20261016/local-100-user-mixed-acceptance.json
```

该脚本为每个租户构造模拟盘长历史、禁用通知渠道和研究任务，同时运行私有事件、
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

### 4.2 前端

```bash
cd frontend
pnpm test
pnpm lint
pnpm build
```

`pnpm build` 同时执行 TypeScript 项目构建和 Vite 生产构建。只通过单个组件测试不
足以替代 lint 和 build。

涉及实时刷新、账户范围或 SSE 的前端回归至少覆盖：60 秒票据到期换票且不调用
整页重载，`events_reset` 只失效指定查询根，账户/租户切换取消并清除旧范围查询，
设置保存只刷新活动查询，以及看板只在交易时段默认最新视图启用 5 秒兜底。分时图回归
必须包含 OHLCV 全空、部分空和非法轴标签值，确认页面显示未知且不抛异常。

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
uv run --frozen --no-sync pytest ../scripts/test_versioning.py ../scripts/test_advance_version.py ../scripts/test_tag_version.py ../scripts/test_version_workflows.py tests/test_version_source.py -q
```

本地与 PR 允许版本不变，只校验格式和事实源一致性。`check_version.py --previous`
保留为显式的相邻版本审计工具，要求精确后继，不用于本地或 PR 的日常门禁。
测试覆盖格式范围、进位和上限、全部当前版本文件同步、历史报告及依赖版本保留、
非法输入不写文件，并使用临时 bare Git 仓库验证远程合并后递增、重跑去重、并发
冲突重试、权限拒绝和未推送提交的保护。workflow 测试还验证质量门与镜像构建使用
同一 SHA，以及版本 artifact 缺失或非法时停止构建。
自动标签测试使用临时 bare Git 仓库验证精确提交、主线归属、版本一致性、
重跑和并发幂等、已有 annotated tag 复用、同名冲突不覆盖及权限拒绝。

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
- 文档相对链接和代码围栏检查。
- 生产规模性能、长时间稳定性、备份恢复和故障演练。

## 6. 按改动选择测试

| 改动 | 最低测试 |
| --- | --- |
| Provider 或能力矩阵 | Provider 契约、字段单位、空数据、缺能力、路由和设置页状态 |
| 日线/分钟/实时数据 | 日期时区、资产路由、分区合并、完整性、分钟空值和缓存失效 |
| enriched/指标 | 固定数值、复权、停牌、历史股本、增量与全量一致性 |
| 策略/信号 | 参数、候选、评分完整性、入场政策、排序、缓存、日线与分钟周期 |
| 回测 | 信号/成交时间、原始/复权价格、T+1、费用、申报数量、滑点、涨跌停、买单过期和未平仓 |
| 监控/通知 | 规则逻辑、逐快照通用规则、策略组快照/共享计算、去重、冷却、重启状态和通知失败隔离 |
| 模拟盘 | 资金持仓守恒、订单状态、T+1、幂等结算、策略组同股裁决、账户预算和并发 |
| API/Token/MCP | 成功、无数据、错误、scope 越权、响应模型、端点快照和 schema 指纹 |
| 邮箱账户认证 | 验证码发送失败、重发冷却、过期、错误次数、一次性消费、注册登录和旧账户迁移 |
| HTTP 观测 | 请求 ID 继承/重生、日志上下文、路由模板聚合、401/403、异常 5xx、认证边界和重置 |
| 前端页面 | 加载、空、错、禁用、切换、窄屏、票据轮换、账户范围和局部刷新恢复 |
| 调度/任务 | 合法状态转换、旧 `pending` 兼容、重复触发、两阶段取消、重启中断、写盘门闸和失败终态 |
| 历史证券状态 | SCD2 区间、同日重跑、名称/ST 变化、退市 tombstone 和首次采集前未知 |
| 文件/迁移 | 原子写、权限失败、旧 schema、路径边界和回滚 |
| 文档 | 相对链接、命令、版本、入口和实际 UI/配置一致性 |

## 7. 金融正确性必测项

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
  具体边界见[研究配置](./research-configuration.md)。

### 7.2 单位和价格

- `change_pct` 在每个边界明确是小数还是百分数值。
- `turnover_rate` 的 Provider 输入与 enriched 输出按各自契约断言。
- 五档数量由“手”换算金额时乘以每手股数。
- 技术指标用复权价；价格限制和可成交判断用原始价。
- 金额、数量、费用和净值的小数精度及舍入规则固定。

### 7.3 交易日和时区

- 使用北京时间，不受测试机时区影响。
- 午休、收盘、周末、节假日和跨日重启有覆盖。
- 09:25 竞价窗口、15:35 盘后任务使用业务日期。
- 分钟数据时间戳归一为北京时间墙钟并正确归属资产类型。

## 8. Provider 真实烟测

真实烟测与自动化契约测试分开，避免外部网络波动阻断普通 PR。发布声明涉及某个
Provider 时，至少记录：

1. Provider 名称、版本和测试日期。
2. 使用的数据能力，不记录密钥。
3. 一个股票、ETF、指数的代表性代码和数据日期。
4. 返回行数、时间范围、必要字段和单位抽查。
5. 空数据、超时、限流和不支持能力的表现。
6. 结果是否落盘，以及重启后能否读取。

rustdx 的专项验证见[rustdx 数据源说明](./rustdx-data-source.md)。

## 9. 人工端到端验收

### 9.1 基础启动

- `GET /health` 返回 `status=ok` 和预期版本。
- 邮箱注册、登录、退出、改密和错误凭据行为正确。
- 旧版访问密码可绑定邮箱完成迁移，原有业务数据保持不变。
- 刷新页面后会话和主要持久化状态符合预期。
- 数据目录挂载到预期位置，重建容器后数据仍在。

### 9.2 数据闭环

- 设置页重新检测后，能力矩阵的候选源、生效源和可用状态正确。
- 运行盘后管道，任务依次结束且数据页范围更新。
- 选股、回测和监控读取相同日期和数据口径。
- 人为制造 Provider 失败时，旧数据不被空结果覆盖。

### 9.3 研究闭环

- 运行一个日线策略并查看候选。
- 用同一策略运行短区间回测，检查原始入场价格/数量、费用、未平仓和拒单原因。
- 编辑可选入场风险后重新打开，确认溢价、ATR/低点和计划风险回显一致；切换策略
  不继承上一策略配置。
- 导出结果、保存候选并载入复测。
- 创建两条策略监控规则，验证成功周期共享结果且通用价格规则仍逐快照触发。
- 建立模拟账户，完成一笔可成交和一笔应拒绝订单；同周期两策略命中同股只产生
  一单，买卖冲突只推进退出。

### 9.4 开放闭环

- 创建最小 scope Token。
- 验证允许接口成功、越权接口拒绝。
- 验证 SSE 建票、连接和断线重连。
- 保持页面超过 60 秒，确认票据轮换后页面不整页刷新，游标连续且无关查询不重拉。
- 使用至少一个 MCP 查询工具；动作工具必须出现确认步骤。

### 9.5 页面与兼容

- 常用桌面宽度和窄屏下检查导航、表格、弹窗和滚动。
- 检查加载、空数据、错误、无权限和任务运行中状态。
- 在看板、自选、策略、回测、因子和设置页分别等待实时更新，确认只更新受影响区域。
- 切换账户或目标租户，确认旧范围请求被取消、旧数据不闪现且应用内导航状态正常。
- 深色/浅色主题受影响页面无不可读内容。
- 升级场景使用一份脱敏旧版本数据副本验证读取和迁移。

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
  scripts/test_version_workflows.py -q
```

覆盖文件增删改、内部/外部链接、容量限制、备份期间变更、真实 Git 的暂存/
未暂存/二进制差异、提交身份与上传前核验。实际平台仍须运行
`build_manifest.py verify`，检查 Docker `/app/build-manifest.json` 或安装包
`.manifest.json`。离线工具测试不代替完整镜像、安装包和升级后启动验收；
恢复步骤见[构建清单与离线升级备份](./build-upgrade-evidence.md)。

## 11. 缺陷严重级别与放行

| 级别 | 示例 | 放行规则 |
| --- | --- | --- |
| `P0` | 数据破坏、密钥泄露、严重错误交易结果 | 禁止发布 |
| `P1` | 核心流程不可用、未来函数、广泛回归 | 禁止发布 |
| `P2` | 明确功能错误、兼容性或明显性能问题 | 修复后发布，或明确缩小发布范围 |
| `P3` | 局部体验、维护性或文档问题 | 可记录后发布，但必须有负责人和处理结论 |

发布不能只依据“CI 绿色”。若真实数据、平台安装或恢复路径是本次变更的一部分，
对应人工验收也是门禁。

## 12. 测试记录模板

```text
版本/提交:
环境:
数据源及版本:
数据窗口:
变更范围:

自动化:
- 命令:
- 结果:

人工验收:
- 场景:
- 预期:
- 结果:

未执行:
- 项目:
- 原因:
- 剩余风险:

缺陷与结论:
- P0/P1/P2/P3:
- 发布结论: 通过 / 限定范围通过 / 不通过
```

历史专项验证记录放在[技术验证记录](./technical-validation.md)。该文件记录某次
环境下的证据，不能替代当前版本重新验收。
