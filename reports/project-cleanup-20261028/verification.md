# 项目冗余分析与精简记录

日期：2026-10-28。源码基线为`18f6f5cbea5bb651dc5fc09f289016bd01b9b6de`，应用版本`0.4.34`。
本批针对源码、依赖与工程内容冗余执行清理；没有提交、推送、部署或递增版本。

## 1. 盘点范围与结论

基线有1,727个Git跟踪文件，共40,548,841字节（38.67 MiB）。
盘点覆盖前后端入口、静态/动态引用、依赖与锁文件、测试、构建/CI、部署、示例及维护文档。
方法为文件清单与字节重复检测、Python/TypeScript语法树、全仓引用搜索及真实回归交叉核对。
字节完全相同且超过80字节的跟踪文件未发现重复；相似业务代码不能据此判为可删除。

| 范围 | 基线文件数 | 判断与处理 |
| --- | ---: | --- |
| 后端 | 825 | 模块化单体与独立角色共用领域服务；删除API旧聚合副本、无调用私有函数及闲置名称 |
| 前端 | 375 | 页面按需路由，源码扩展通过glob加载；删除6个无入口/调用的旧文件 |
| 文档 | 15 | 六类主手册与专题已有统一归属；本批仅增加简短维护记录链接 |
| 报告 | 312 | 17,549,631字节，包含原始研究、发布及恢复证据；保留原内容 |
| 品牌/功能截图 | 70 | 包含运行静态资源、文档展示与打包输入；保留 |
| 部署/打包 | 45 | 不同角色/平台有独立入口；只移除闲置依赖的打包元数据 |
| 工程脚本/工具测试 | 31 | 当前检查、版本、恢复、升级与验收入口继续使用；保留 |
| 示例/MCP | 15 | 独立可运行接口样例及MCP包；保留其独立版本与依赖 |

依据为[工程归属](../../docs/reference/engineering.md#layout)、
[真实路由](../../frontend/src/router.tsx)、
[扩展加载](../../frontend/src/extensions/bootstrap.ts)、
[CI矩阵](../../.github/workflows/ci.yml)与[清理约束](../../CONTRIBUTING.md#13-最小范围修改)。

## 2. 已删除内容及替代依据

六个前端文件合计49,998字节、1,146行。
它们既不在实际路由/组件调用链中，也不匹配扩展发现规则；现有测试没有依赖这些旧文件。

| 删除文件 | 行数 | 当前实现或删除依据 |
| --- | ---: | --- |
| `frontend/src/pages/Analysis.tsx` | 300 | 扩展分析管理已在[设置面板](../../frontend/src/pages/settings/ExtPages.tsx)，旧`/analysis`重定向继续保留 |
| `frontend/src/pages/settings/Keys.tsx` | 317 | 当前[数据源页面](../../frontend/src/pages/settings/DataSources.tsx)独立管理Key与能力路由，旧组件无引用 |
| `frontend/src/components/data/MinuteSyncConfig.tsx` | 222 | 由[六周期配置](../../frontend/src/components/data/KlineSyncConfig.tsx)承接，旧重复effect及表单一并删除 |
| `frontend/src/components/data/ExtendHistoryPanel.tsx` | 98 | 日K历史扩展已使用同一六周期面板与原API |
| `frontend/src/components/CandlestickChart.tsx` | 168 | 个股K线使用[当前图表](../../frontend/src/components/EChartsCandlestick.tsx)，旧图表及重复数字格式化无调用 |
| `frontend/src/lib/ocrInstallHint.ts` | 41 | 旧前端平台提示函数无调用，现有OCR服务、上传入口和失败反馈保持 |

[市场总览API](../../backend/app/api/overview.py)由350行缩至43行，删除14个旧聚合/工具函数。
真实聚合继续使用[共享服务](../../backend/app/services/market_overview_builder.py)，
保留的`_build_overview`与`market_overview`经AST逐项比较，函数参数、装饰器及函数体均与基线一致。
数据目录/日期缓存键、TTL与`invalidate_overview_cache`重导出继续保留。

另外删除6个无生产调用的私有函数：`ext_data._bare_symbol_expr`、`strategy._get_monitor`、
`pipeline._math_half_up`、`daily_pipeline._refresh_instruments_view`、
`monitor._is_signal_field`及`policy._compute_label`。
只引用失效维表刷新函数的测试monkeypatch一并移除，真实快照保持用例继续通过。
删除关联的闲置常量、未使用导入/变量及重复导入；有副作用的调用保留，例如`run_pipeline()`和Provider测试注入。
企业微信连接循环仅去掉原样重抛异常的外层包装，消息、心跳、断线与取消行为实测保持。

源码、依赖及打包合计33个跟踪文件变化，15行新增、1,552行删除，净减少1,537行；
这组数字不含文档链接与本报告，也不表示运行性能或已安装产物体积提升。

## 3. 依赖及兼容核对

后端直接依赖由27项减至26项，唯一移除项为`platformdirs`。
[配置](../../backend/app/config.py)已直接按环境、冻结资源目录及项目根目录定位数据；
该依赖仅剩旧说明和打包元数据，没有运行导入。
同步删除[依赖声明](../../backend/pyproject.toml)、[锁文件](../../backend/uv.lock)中的包及直接边，
以及[PyInstaller元数据收集](../../packaging/tsp.spec)中的对应项。
离线冻结锁检查通过；逐包对比只删除`platformdirs`并调整主包依赖，其余第三方版本、来源及产物哈希均一致。

前端依赖全部有真实TypeScript导入。`lightweight-charts`仍供成分股图表使用，
`fastexcel`由Polars读取Excel动态加载，空`rustdx/cell`extras仍承接旧安装命令，继续保留。
不按导入数量删除动态依赖、原生桥接、配置模板或公开API。
`frontend/vite.config.d.ts`虽由构建输出，但有LF与清洁发布源码回归契约，继续保留。
`_set_repo/_reset_repo`、扩展数据flatten函数与Provider目录方法均有测试或跨模块调用。

无需迁移策略、API、Parquet、SQL或用户偏好，不改变账户、口令、SMTP和数据源配置。
原工作区未提交的研究/部署记录不属于本批；历史证据、用户数据与Git历史均不清理。
回退可从上述基线逐文件取回删除内容，并在覆盖前比较后续修改；锁文件、声明和打包列表应成组恢复。

## 4. 实际验证

测试使用隔离工作目录、`TICKFLOW_ENV_FILE=/dev/null`、独立`DATA_DIR`及固定源码`PYTHONPATH`。
后端Python依赖使用已有冻结环境并加`--no-sync`，避免重建或修改原工作区运行环境。

| 检查 | 实际结果 |
| --- | --- |
| 清理前对应后端模块 | 112 passed；涵盖周期、历史进度、股票池、总览、策略只读数据与公共缓存 |
| 清理前前端全量测试、lint、build | 437 passed，lint/build通过 |
| `uv run --frozen --no-sync pytest tests -q` | 4,581 passed，162 skipped，180 warnings，277.59秒 |
| `uv run --frozen --no-sync pytest ../tests/tooling -q` | 134 passed，62.43秒 |
| `pnpm test` | 83个文件、437个用例全部通过 |
| `pnpm lint` / `pnpm build` | 均通过；构建仍提示现有大chunk |
| `uv lock --check --offline` | 125个包解析通过，锁文件与声明一致 |
| Ruff `F401/F811/F841` 全后端检查 | 26项闲置/重复名称诊断降至0 |
| `check_ruff_delta.py --base HEAD` | 24个Python文件，486→444项历史诊断，新增0 |
| WebSocket隔离行为核对 | 消息不额外ping、超时ping、连接错误及取消向上抛出均通过 |
| TypeScript AST引用复核 | 仅扩展入口无静态引用，且被现有glob加载；所有前端直接依赖有调用 |
| PyInstaller spec与总览函数核对 | spec语法通过，保留API函数AST一致 |
| 最终仓库依赖行为回归 | 28 passed，涵盖仓库缓存、日K复用与股票池快照 |
| 共享工作区最终检查 | `make check`及含历史报告的131份文档检查通过；版本仍为0.4.34 |
| 回写及已有工作保护 | 44个跟踪文件与已测工作目录逐字节一致；742个原未提交研究/部署文件SHA-256均保持 |

上述全后端结果包含环境条件跳过项；未执行真实PostgreSQL集成、Windows/macOS安装包构建、
生产数据源或线上升级验收。测试通过只对应本次源码及实际执行环境。

## 5. 文档维护与持续精简

需求、方案、开发、测试、运营、运维及二开指南均引用本记录；
用户操作没有变化，[操作说明书](../../操作说明书.md)保持原内容。
两份历史页面报告只更新已删除源码的3条导航并注明日期，原始数量与分析结论保留。
维护继续沿现有[增量Ruff门禁](../../scripts/check_ruff_delta.py)、
TypeScript未使用名称检查及全量CI，避免用批量格式化掩盖已有诊断。
依赖/死代码在功能退役或月度维护时核对，更新依赖时单独审查锁文件与平台验证；
本批不混入依赖升级或新的架构抽象。

后续若清理研究/发布材料，应先核对保留责任、当前引用、未解决问题和恢复用途。
本批未证明可删除的相似页面、Provider适配、部署单元与历史证据继续保留。
