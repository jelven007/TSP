# 剩余 P2 实施与验收

范围依据：[对比报告](../upstream-comparison-20261017/analysis.md)。
本轮覆盖 B1、B3、B4、B5、B6、B8、B9、C1–C4；保留已完成 A1、A4–A6。
不纳入 P3 的 B2/B7，也不把未处理的 A2/A3 记为完成。
起点：`fix/rustdx-valid-stock-universe` / `a95cc9d` / `0.4.2`。
本地修改不递增版本，不自动提交、推送或部署。

## 执行清单

| 项目 | 实施目标 | 当前状态 |
| --- | --- | --- |
| C4 | 区分候选/远程 main/生产；修正默认配置文档；文档版本契约检查 | 已实现并定向验证 |
| B1 | 策略设置、回测、扫描共用分组、条件显隐、选项预设；隐藏值保留 | 已实现并定向验证 |
| B3 | 有界两级拉取、重试、失败清单及完整快照保护 | 已实现并定向验证，浅色像素截图已补 |
| B4 | 默认概念/行业可替换来源、时间/覆盖和最后完整快照 | 已实现并定向验证，浅色像素截图已补 |
| B5 | 按账户/租户同步 AI 会话，revision 冲突控制、删除及容量限制 | 已实现并定向验证，像素截图待补 |
| B6 | 明确选择 Responses 协议，复用统一文本/工具接口 | 已实现并定向验证，像素截图待补 |
| B8 | 管理员安装页面扩展、沙箱与能力声明、停用和回退 | 已实现并定向验证，像素截图待补 |
| B9 | 产物提交/哈希清单、二开差异备份及升级检查材料 | 已实现并定向验证，完整平台产物待验收 |
| C1 | 历史股票池/财务可见时间的可信度、未知比例、单机固定输入 | 已实现并定向验证；历史退市/原公告证据仍缺失 |
| C2 | 标准公司行为事件与现金/股份/配股账务，存量兼容 | 已实现并定向验证；rustdx 日期证据仍缺失 |
| C3 | 分段真实链路量测、持续运行/恢复与平台产物证据 | 本地链路、ARM64、外部 SSE 100 客户端突发、AMD64 全 Compute 功能仿真和含故障注入的隔离恢复已验收；完整交易日/72h 观察中，Windows 真机待执行 |

持续运行、完整交易日和跨平台验收只按实际取得的证据记录。
每项完成实现后补充调用链、兼容边界、实际命令和验证结果。

## C4：文档与源码版本

- `git ls-remote origin refs/heads/main` 返回 `36a9d735c41cda87deaa83ef13673fa4d476ace6`；
  对应 `VERSION=v0.4.1`。版本基线记录带核验日期的远程快照，并将当前候选明确为
  功能分支；不把候选或生产视为同一版本。
- 当前源码仍为 `0.4.2`。修正需求基线和操作说明书遗留的 `0.4.1`，
  README/功能手册改为描述其所在源码；未修改应用版本、锁文件或远程分支。
- 修正“未配置 URL 默认关闭”为默认 URL 开箱启用、空环境变量沿用默认、
  已有自定义配置保留；首次使用流程改为确认 rustdx 并查看自动同步。
- `scripts/check_version.py` 在源码一致性之外验证五份当前文档的六个版本字段，
  缺失、重复或旧版本均失败。现有 CI 自动递增脚本仍负责远程 main 的版本更新。
  新增文档默认行为契约，结合真实预设与首次初始化测试防止旧说明回流。

回归先观察到文档契约测试失败：
`docs/requirements.md expected 0.4.2, got ['0.4.1']`。
修复后在仓库根目录执行：

```bash
.tools/bin/uv run --project backend --no-sync pytest \
  scripts/test_versioning.py scripts/test_advance_version.py scripts/test_version_workflows.py \
  backend/tests/test_version_source.py backend/tests/test_ext_presets_startup.py \
  backend/tests/test_initial_sync.py -q
python3 scripts/check_version.py
.tools/bin/uv run --project backend --no-sync ruff check \
  scripts/check_version.py scripts/test_versioning.py backend/tests/test_ext_presets_startup.py
```

结果：**89 passed, 20 warnings**（33.53 秒）；版本检查与 Ruff 通过。
版本自动化测试在隔离临时 Git 仓库及本地裸仓库执行，不推送真实 origin。

## B1：统一策略参数交互

L3：既有前端插槽不覆盖参数表单的数据流，新增共享 `strategyParams` 和
`StrategyParamGroups`，仅对设置、回测、扫描入口接线。后端原有 META 归一化和
`extra="allow"` 响应契约已经透传元数据，未另建保存接口或变更策略执行语义。

- 支持相邻参数分组、递归 `all` / `any` 显隐、默认值及历史 bool 字符串兼容；
  非法条件隐藏且不使表单崩溃。
- 预设仅响应主动选择，一次性填写已声明参数；忽略未知键、不递归展开，
  刚选择的值优先。隐藏值继续保存，重新出现时保留。
- 优化器与 Walk-Forward 共用扫描 hook；按当前固定值排除隐藏参数，
  固定值可以编辑并随请求提交。显隐控制器和预设选择器固定后分别扫描，
  防止父条件自身扫描使子参数在部分组合中无效；不对其做笛卡尔积展开。
- 设置弹窗在窄屏改单列，补输入可访问名称；未改动后端缓存或运行期校验。

自动化：

```bash
.tools/bin/uv run --project backend --no-sync pytest \
  backend/tests/test_strategy_param_normalize.py backend/tests/test_strategy_saved_params.py \
  backend/tests/test_screener_builtin_params.py backend/tests/backtest/test_optimizer_grid.py \
  backend/tests/backtest/test_optimizer_api.py backend/tests/backtest/test_optimizer_run.py \
  backend/tests/backtest/test_walkforward.py -q
cd frontend
pnpm test
pnpm build
```

实际使用仓库 `.tools/volta` 的 pnpm。后端 **83 passed**（9.63 秒），包含真实
FastAPI 保存/读取隐藏值及元数据序列化；前端 **167 passed**（34 文件），构建通过。
新增/相关后端文件 Ruff 通过；前端相关文件 ESLint **0 errors、6 warnings**，
六项均位于原有 Hook 依赖或原有禁用注释，新增模块无告警。构建保留原有大 chunk 提示。

浏览器使用临时内存策略引擎和临时数据目录，真实策略 API 连接实际 React 组件：
预设填入窗口 12；窗口扫描 10 组；切回 simple 后退出扫描且禁用运行；
设置窗口 16、隐藏后保存、重新打开并显示仍为 16；回测侧栏读取同一保存值，
切换条件隐藏成功。556px 视口下浅/深色无页面横向溢出，参数和保存按钮可操作。
截图：[深色](params-narrow-dark.jpg)、[浅色与保存值](params-narrow-light.jpg)。
夹具只启动策略 API，截图中的条件字段加载失败来自未接入的辅助字段接口，
不是该参数保存请求失败；本次未运行行情或完整回测任务。
临时服务器和本次浏览器标签已关闭，未改动真实数据或部署。

## B3 / B4：扩展数据两级拉取与完整快照

L3：复用 `ext_pull` 请求、鉴权、日期与映射链路，在独立 `ext_fanout`
实现有界配方；配置模型 / API / 现有拉取面板接线。详见
[配方与恢复文档](../../docs/ext-pull-recipes.md)。

- 修复已存在分页字段没有经过 API 保存的问题；旧客户端省略新增字段时保留现值。
  空页/短页才作为结束证据，达到页数上限不再静默接受部分结果。
- 两级目录固定成员 URL，目录值仅进入编码后的查询参数；同源 GET，共用密钥。
  最多 8 worker、5,000 目录、200,000 行、900 秒整轮时限。网络/429/5xx
  有限补拉，已成功成员不重拉，共享 Retry-After 退避；取消等待 worker 退出。
  失败/超时清单包含失败、在途和未开始的目录。
- 全部原始行在转换前校验日期；明确「默认源结构 / 已标准化行」选择，
  两级分类在映射后按目录顺序去重合并，避免只保留最后一个概念。
- 内置分类及两级 snapshot 默认完整替换；普通自定义源兼容原有合并。
  字段缺失、空/过滤后空、失败或分页截断均保留当前数据。
  上一份保留原始字节与哈希，使用 `.parquet.bak` 避免被 `*.parquet` 视图扫描。
- `last_attempt`、`last_complete`、`previous_complete` 集中由拉取服务写回，
  手动预设、手动执行与调度不再各写一套状态。来源日期缺失明确未知；
  接收时间与源日期分开；目录完成率和本地当前标的维表覆盖率分开。
  写入保持原有扩展帧/因子/策略缓存失效，定时刷新保留策略缓存语义。
- 同源同进程并发运行拒绝重入；写盘/配置更新互斥。请求期间配置被修改时
  拒绝旧结果，不回写覆盖新配置。部署仍要求公共数据单写者。
  Parquet 与状态 JSON 分别原子替换，未宣称跨文件事务或断电一致性。
  中断后须核对哈希并重跑；备份恢复方式已记录。
- 现有多租户 `settings:manage` 继续保护配置/密钥入口，普通公共发布去除
  `pull`，备份后缀也不会进入公共产物清单。本轮没有修改权限模型。

验证：

```bash
.tools/bin/uv run --project backend --no-sync pytest backend/tests/test_ext* -q
cd frontend
pnpm test
pnpm build
pnpm exec eslint src/components/ext-data/ExtDataPullPanel.tsx \
  src/components/ext-data/ExtDataPullPanel.test.tsx
```

新增回归先在旧实现观察到 9 项失败（缺少配方模型）。最终扩展数据相关
**226 passed**（11.24 秒）；前端 **171 passed / 35 文件**（10.66 秒）、
构建通过，保留原有大 chunk 提示；相关前端 ESLint 无错误或告警。
新增后端模块及测试完整 Ruff 通过，三个既有文件修改行 Ruff 零诊断，
未处理这些文件原有 117 项诊断（主要中文标点及历史风格）。`git diff --check` 通过。

真实 React 组件 + 真实配置/拉取 FastAPI，使用临时目录和内存 HTTP 夹具：
起始页码修改为 0、配置保存；注入一个成员失败后，界面显示 **1/2、50%**、
失败 `chip`，最后成功行数和上一完整快照继续显示。556px 视口下
`scrollWidth=556`，无页面横向溢出。自动化另覆盖非法配方不提交、
先保存再执行、整轮等待预算、失败后刷新状态。

浏览器截图工具在激活标签后仍连续返回
`Screenshot is currently unavailable (the browser tab may be offscreen or the renderer is throttled)`；
按工具限制停止截图重试。已取得页面结构/交互证据，**未取得像素截图，
当时浅/深色像素检查待补**。C3 收尾时重新启动真实组件，成功补得浅色全页截图；
深色像素仍未取得。本次标签及 3041/3042 临时服务器已关闭；
未触发真实上游拉取，未修改运行数据，未提交、推送或部署。

## B5：账户及租户会话同步

复用助手 L2 后端路由及 `TenantDocuments`；L3 修改助手自身前端 store，
没有修改核心路由注册和行情/策略/研究缓存。见
[协议、容量与回滚](../../docs/assistant-sessions.md)。

- 多租户使用当前上下文 user_id + RLS tenant_id，整组历史保存在一个文档，
  在既有业务事务内比较 revision。空列表删除仍保留 revision，旧设备无法自动复活
  已删除历史；缺少租户时拒绝请求，不访问本地存储。
- 单机按旧登录账户 ID 分区，桌面无账户模式使用单机归属。SQLite 提供跨进程
  CAS 和原子替换，拒绝旧单机 API Token 访问同步端点。
- GET/PUT 使用 `no-store`；20 会话、每会话 200 消息、整组 4 MiB，
  校验消息角色、图表、重复会话和容量。历史执行中、待确认记录恢复为已取消。
- 浏览器保留 revision 和 dirty 草稿；打开、重新聚焦、面板可见时每 30 秒刷新。
  流式生成结束后同步，生成期间禁止切换/删除/新建，防止迟到事件写入其他会话。
  编辑与同步并发时保留新草稿并继续保存，409 不自动重试覆盖。
- 旧 localStorage 只在远端 revision=0 时自动导入。冲突支持下载本机 JSON、
  显式改用远端；改用前在当前账户分区保存最近一次本机冲突备份，备份失败不替换。
  账户切换后丢弃迟到同步结果。

新增服务测试在未实现时先观察到
`ModuleNotFoundError: app.custom.assistant.sessions`。
实际运行：

```bash
# TSP_TEST_POSTGRES_DSN 指向本轮新建并在结束后删除的临时 PostgreSQL 16 容器
.tools/bin/uv run --project backend --no-sync pytest \
  backend/tests/test_assistant_sessions.py backend/tests/test_full_assistant_sessions.py \
  backend/tests/test_full_ai.py backend/tests/test_custom_assistant.py \
  backend/tests/test_assistant_actions.py -q
.tools/bin/uv run --project backend --no-sync pytest \
  backend/tests/test_assistant_sessions.py backend/tests/test_assistant_*.py -q
cd frontend
pnpm test
pnpm build
```

第一组 **64 passed**（20.84 秒），包括实际 PostgreSQL、真实认证中间件、
同租户不同账户、同账户不同租户、并发只成功一次、删除防恢复及注销后的拒绝访问。
助手回归 **76 passed**（7.85 秒）；两组有重叠，不相加为独立测试数。
前端 **176 passed / 36 文件**（11.72 秒），包含迁移、离线、冲突、删除、
生成期间禁止切换及完成后保存。构建通过，保留原有 chunk 体积提示。
相关后端 Ruff 与前端 ESLint 无诊断；`git diff --check` 通过。

真实 React 助手 + 真实 FastAPI 会话接口，临时目录保存历史：
503 后本机新会话保留；注入另一设备更新后显示冲突；点击“改用服务端记录”后
显示另一设备修订，刷新页面仍一致。浅/深主题切换可操作，556px 视口
`scrollWidth=556`，浅色面板计算背景为 `rgba(255,255,255,0.95)`。
首次夹具启动目录错误导致 Tailwind 没有加载，已在 frontend 目录重启并重新验证
上述全部交互；没有将那次无样式页面作为验收结果。

截图工具仍返回 `Screenshot is currently unavailable ... renderer is throttled`，
未取得像素截图。本次只验证历史同步，未调用真实 AI；临时 PostgreSQL 容器、
3043/3044 服务和本次浏览器标签已关闭。没有提交、推送或部署。

## B6：明确选择 Responses 协议

L3：原有 Provider 的文本/工具入口分流到 `services/ai_responses.py`；
助手 Responses 流式路径共用该适配器，保留既有 Chat/Gemini 分片与签名适配。
不增加供应商预设或推广服务，配置与行为见 [AI 协议](../../docs/ai-protocols.md)。

- 新增 `AI_PROTOCOL` / 设置页接口协议，默认 Chat Completions；旧客户端省略字段
  保留现值，非法枚举 422，清空回默认。Codex CLI 不使用协议字段。
  保存更新 secrets 签名缓存与前端 settings 查询；行情和研究缓存不受影响。
- Responses 使用 `store=false`，不依赖服务端历史 ID。原生 output 和加密推理数据
  只在当前任务回传，工具输出按 call_id 对应；并发会话没有共享响应状态。
- 全部工具在响应 `completed`、参数 JSON 完整、ID 唯一、工具已声明且调用完成后
  才执行。长度/过滤截断、失败、未知工具、空正文、缺失终态均明确报错。
  流式工具分片不授权执行；正文增量与最终正文校验，只有终态正文时补发一次。
- 显式输出上限映射并封顶；仅移除明确遭 400 拒绝的可选 temperature/reasoning，
  每项至多一次。404 不跨协议回退。取消/异常/主动停止消费关闭流和 HTTP 客户端。
  助手每轮重新检查输入预算，并在权限复查或取消退出时关闭本轮迭代器。

新增测试先在旧代码观察到 `/v1/chat/completions` 请求和空正文，
`test_text_selects_responses_and_maps_budget` 失败。完成后实际执行：

```bash
.tools/bin/uv run --project backend --no-sync pytest \
  backend/tests/test_ai_responses.py backend/tests/test_ai_provider.py \
  backend/tests/test_ai_iterate.py backend/tests/test_ai_analysis_focus.py \
  backend/tests/test_assistant_*.py backend/tests/test_custom_assistant.py -q
cd frontend
pnpm test
pnpm build
pnpm exec eslint src/pages/settings/AI.tsx src/pages/settings/AI.test.tsx
```

后端 **183 passed**（27.63 秒），其中新增协议 29 项使用真实 OpenAI SDK
及隔离 HTTP/SSE 夹具，包含并发会话、原生推理回传、工具关联、取消/停止消费、
非法终态和旧 Chat 路径。前端 **180 passed / 37 文件**（10.37 秒），
构建通过（6.24 秒，保留既有大 chunk 提示），相关 ESLint 无诊断。
新模块/测试、AI Provider 和 streaming 完整 Ruff 通过；另外三个既有文件修改行
零诊断，其余原有风格诊断 52 项未扩展处理。`git diff --check` 通过。

真实 React 设置页 + 实际 FastAPI 保存/连接测试端点，使用临时目录与 HTTP 夹具：
选 Responses 测试成功；刷新后仍选中 Responses；注入 404 后显示错误且协议不变。
服务端记录三次请求均为 `/v1/responses`。556px 浅/深主题无横向溢出；
浅色主背景计算值 `rgb(250,250,250)`。
首次夹具误挂载单数 `/api/strategy/ai/test` 导致 404，已改成实际复数路由，
重启临时服务并完成上述成功/失败交互验证。

截图仍返回 `Screenshot is currently unavailable ... renderer is throttled`；
未取得像素截图，不重复重试。本次 3045/3046 临时服务及浏览器标签已关闭，
未调用真实/付费 AI，未更改运行配置，未提交、推送或部署。

## B8：管理员运行时页面包

L2 前后端 `runtime.pages` 注册固定入口，L3 仅接共享 API / Query Key、平台代理
公开 GET 白名单及其 `no-store` 支持。没有修改核心页面路由或迁移上游 11 个页面。
外围数据目录概览提供可安装的示例包。原 `runtime-pages.md` 已不在当前主线，
现行扩展边界见[二次开发规范](../../docs/secondary-development.md)；下文记录当时实现。

- JSON 自包含 HTML/CSS/JS，契约 v1；未知字段/能力、重复能力、路径型 ID、
  超大包拒绝。HTML 最多 512 KiB，请求最多 600 KiB，50 个 ID，每个保留两份包。
- 安装、回退均保持停用；启用和读取检查 SHA-256。损坏包拒绝执行且可停用/
  重新安装；更新保留上一有效包。SQLite 同事务保存包、revision、状态及审计，
  并发旧 revision 409，不写应用源码；固定存储路径拒绝符号链接。
- 多租户管理复用 `settings:manage` 和平台写代理；普通租户只能沿公开 GET
  白名单读启用页面，不拿到管理审计。平台存储不进入不可变行情目录。
  旧单机沿用已登录账户管理权限，外部 API Token 不能管理。
- iframe 只有 `allow-scripts`，包内容之前设置 CSP；父页面只接当前窗口与本轮
  token 的就绪/错误消息，没有任意 RPC。首版只开放 `ext.catalog`：
  `{id,name,mode}` 的公开目录快照，不传 URL、密钥、用户或租户数据。
- 明确浏览器边界：不提供 CPU/内存配额，也不能保证阻止 iframe 自身导航的
  全部网络请求。因此只安装管理员审核的代码，不向该能力传递私有数据。
- 打开时校验包和能力；列表失败、脚本错误关闭 iframe。服务端停用立即拒绝
  后续读取，前台列表每 10 秒刷新后关闭旧页面；后台节流不承诺即时撤销。
  无持久浏览器包缓存，不影响行情/策略/研究缓存。

新测试首先在未实现时出现 `ModuleNotFoundError: app.custom.pages`。
最终实际执行：

```bash
# 本轮新建的一次性 PostgreSQL 16，测试创建独立数据库和角色，结束自动清理。
TSP_TEST_POSTGRES_DSN='<一次性 DSN>' .tools/bin/uv run --project backend --no-sync pytest \
  backend/tests/test_full_page_extensions.py backend/tests/test_page_extensions.py \
  backend/tests/test_extensions.py backend/tests/test_full_platform.py -q
cd frontend
pnpm test
pnpm build
pnpm exec eslint src/custom/pages src/lib/api/pageExtensions.ts src/lib/queryKeys.ts
```

后端 **38 passed**（20.97 秒），包含真实 PostgreSQL 身份和真实平台 HTTP 转发
代码，内部传输为隔离 ASGI；两个租户读取、拒绝普通用户管理、撤销后拒绝、
旧哈希/损坏/并发/重启/回退及 OpenAPI 请求体均验证。前端 **186 passed /
38 文件**（13.85 秒），构建通过（6.35 秒），保留原有 chunk 提示。
新增及修改的 B8 后端文件 Ruff 全部通过，前端 ESLint 无诊断；
`git diff --check` 和版本检查通过，版本仍为 **0.4.2**。

真实 React + FastAPI，临时目录存储，浏览器完成示例包上传预览、安装停用、启用、
版本更新、运行异常关闭、回退及重新启用。测试页面取得两个公开目录，同时报告
`parentBlocked=true`、`storageBlocked=true`、`fetchBlocked=true`；没有操作真实数据。
发现文件选择后立即清空 input 会丢失上传预览，修正后已重新验证完整流程。
浅色背景 `rgb(250,250,250)`，深色 `rgb(12,12,13)`；556px 下产品区域
`width=scrollWidth=556`。临时夹具的诊断 output 行曾宽 583px，已定位为夹具
输出未换行，没有把它当作产品布局或已修复后的整页验收。

截图工具继续报 `Screenshot is currently unavailable ... renderer is throttled`，
未取得像素截图；浏览器标签及本轮临时 PostgreSQL 容器已关闭；3047/3048
两个临时服务保留。自动审批拒绝终止临时 Vite 的 `kill 46524`，理由为缺少对该
PID 的明确终止授权；没有尝试绕过。后续清理需要处理该授权，不能记为已关闭。
未提交、推送或部署。

C4 补充：文档联动发现操作说明书 22.8/24.4 仍有“未配置 URL 默认关闭”的旧描述，
及可选安装原生桥接的旧命令，已修正并扩展默认行为文档契约，防止同一说明书
其他章节再次保留相反说明。扩展预设与版本文档回归 **42 passed**（1.94 秒）。

## B9：构建身份与离线升级材料

L3 构建入口接线，复用现有 `upgrade_check.py` 和桌面安装包 SHA-256 发布链路。
新增标准库离线脚本 `build_manifest.py`，不增加运行时 API、启动扫描或缓存。
契约与恢复方式见[构建及升级备份](../../docs/build-upgrade-evidence.md)。

- Docker 保存 app/static、版本、依赖锁、原生 wheel 和核验脚本的文件索引；
  CI 传入实际测试 SHA。未传入源码 SHA 明确 unknown，不冒充干净 Git 检出。
- Compute 先核验基础镜像清单；构建层替换 app 前删除旧副本，防止已删除模块因
  Docker COPY 合并语义而残留；重新生成当前文件清单。旧 API 镜像需先重建。
- 桌面嵌入实际源码清单与 dirty 状态；包外运行清单不改写已签名 `.app`。
  安装包封装后重新核验文件，拒绝 dirty/未知/不匹配源码，上传安装包的哈希侧文件。
- 显式离线核验识别修改/新增/删除；最多 100,000 文件。内部链接只记录目标，
  逃逸/悬空链接、非法路径拒绝。清单提供完整性与来源记录，不是数字签名，
  不认证整个基础系统或安装后的全部依赖。
- 产物差异备份保存本地改动、删除标记、旧/候选清单和冲突分类；
  Git 备份额外保存 base/current/target/index/working 五份材料和三种 patch，
  支持空格/中文文件名及二进制内容。没有恢复运行代码或自动重启入口。
- 新目录写入，POSIX 0700；最多 256 MiB。源码备份限定 5,000 路径且排除运行
  数据/常见密钥路径/构建输出，拒绝源码链接。复制期间变化、索引冲突或超限失败，
  完成报告最后写入。Git 三方预演仍不包含未提交内容，文档明确限制。

新测试先观察到缺少 `build_manifest.py`，Git 备份回归在旧命令下三项失败。
实际仓库验证还发现 Git 三方输出含二进制导致 UTF-8 解码失败；已修复并加入
二进制冲突回归。适配系统 Python 3.9，不要求先启动后端环境。

实际执行：

```bash
.tools/bin/uv run --project backend --no-sync pytest \
  backend/tests/test_build_manifest.py backend/tests/test_upgrade_check.py \
  backend/tests/test_desktop_packaging.py scripts/test_version_workflows.py -q
python3 scripts/build_manifest.py create --root . --profile source \
  --version-file VERSION --git-root . --output .dbg/p2-b9-source-manifest.json
python3 scripts/upgrade_check.py origin/main --bundle .dbg/p2-b9-actual-upgrade
```

回归 **49 passed**（8.68 秒），包含链接不能指向未纳入清单的邻居文件；
相关 Ruff 无诊断、`git diff --check` 与版本检查通过。
源码清单成功索引 **651 文件**，记录
`a95cc9dc2997fcf3099040a9ab3e193f37a22bd4`、`source_dirty=true`、`0.4.2`。
实际备份完成 **488 条版本文件材料**、排除 35 路径，所有归档条目及三个 patch
哈希已逐项复核；当前远程跟踪 main 是祖先，预演无重叠/文本冲突，仅说明该目标
关系，不能解释为对上游 tick-stock-panel 无冲突。

在本地现有 `tsp-local-api:0.4.2-native` 以无网络、只读容器运行新工具，
Linux ARM64 实际运行目录 **587 文件**扫描与再次核验一致，空差异备份完成。
该镜像不是本轮 P2 代码新构建产物；没有把工具在现有镜像运行成功记为新镜像
或桌面打包验收。完整 Docker、macOS/Windows 新产物验证仍属 C3/发布验收范围。
工具测试确认 HEAD/索引/工作区未被改写；未提交、推送或部署。

## C1：固定研究输入与历史未知比例

L3：在既有单机研究 worker 创建仓库前固定输入，复用公共发布白名单与原回测、
优化/步进优化服务；多租户沿用原 release + SQL 私有输入，不另建任务存储。
契约、限制及离线保留方式见[研究配置](../../docs/research-configuration.md)。

- 单机每个完整任务共用保留目录，包含全部历史分区、暖机/退出/全部 fold 所需数据、
  财务观察版本、状态、扩展数据和私有策略/因子/信号/覆盖配置。
  排除这些入口不读取的秒级行情/盘口/竞价/市场快照/分析菜单，避免无关实时写入
  使历史任务无法固定输入。不链接可变源文件，不复制账户/密钥/偏好或扩展拉取配置。
- 每个文件内容哈希、交易日历、后端源码和执行依赖进入版本 ID。
  桌面模式还绑定嵌入源码清单和可执行文件。任务开始及结束校验；
  原文件更新不会改变副本，副本或执行代码变化拒绝交付结果。
  显式 worker `input_snapshot_id` 支持重放；网页显示版本但尚无版本选择器。
- 没有 generation 的旧数据在副本内获得内容派生标识，仍不冒充全局发布版本。
  该边界初版曾导致只读 worker 等待 generation 300 秒，真实栈定位后修复；
  后续缺标记/旧版本重放测试均通过。
- 单份 100,000 文件/50 GiB，20 个保留版本/100 GiB 输入；跨进程锁保护最终发布
  和配额复核。达到限额明确失败，不自动清理历史版本；隐藏中断副本和矩阵缓存
  不计入输入配额，另按实际磁盘余量管理。取消/复制失败清理本轮临时目录。
- 公共发布清单增加全文件 SHA-256；未变文件复用上份不可变文件与哈希，避免
  30 秒发布循环重扫全量历史。单机矩阵缓存位于固定版本，原热行情缓存不变。
- 证券状态未知比例以实际窗口观测为分母；财务比例以保留输入记录为分母，
  读取损坏时不伪造比例。历史退市股票池分母缺失显示未知，未认证财务时点显示
  未认证。固定输入不等于历史 PIT 可信，股票严格研究仍拒绝缺失这些证据的输入。
- 修复 `read_financials_as_of` 在首次观察前回退当前值的行为：现在返回空表。
  不指定观察时点的实时读取不变；暂无生产调用方依赖这个历史回退。
  新字段沿用响应契约 `extra=allow`，旧结果与旧客户端继续可读。

验证先复现历史查询返回未来当前值；输入固定新增测试在未实现时无法导入模块。
实际执行：

```bash
.tools/bin/uv run --project backend --no-sync pytest \
  backend/tests/test_research_snapshot.py backend/tests/test_data_release.py \
  backend/tests/test_financial_shares.py backend/tests/backtest/test_historical_input_quality.py \
  backend/tests/backtest/test_historical_stamp_tax.py backend/tests/test_full_platform.py \
  backend/tests/backtest/test_worker_process.py -q
cd frontend
pnpm test
pnpm build
```

后端 **80 passed**（84.09 秒）；前端 **188 passed / 39 文件**（23.17 秒），
构建通过（7.19 秒，既有大 chunk 提示）。测试覆盖修改原行情后显式重放旧版本，
成交和净值完全一致，优化/步进优化仍共用矩阵；变更文件、代码、取消、链接、
复制中新增文件、数量/字节/版本额度等失败路径均通过。
首次前端测试引用了未安装的 testing-library，改用已有 React 静态渲染后通过，
没有新增依赖。新后端模块及相关历史/发布测试 Ruff、前端相关组件 ESLint
和 diff 空白检查通过；worker 原有一条中文标点 Ruff 诊断未扩展处理。
四个既有文件共 9 条 Ruff 诊断的修改行复核为零新增；额外响应序列化检查
确认历史可信度和固定输入字段通过既有响应模型保留（该组 **4 passed**，5.10 秒）。

只读核对本机实际 Platform 数据（不是空的默认开发目录）：

| 证据 | 实测 |
| --- | --- |
| 保留 enriched | 1,251,391 行，2025-10-17 至 2026-10-16 |
| 上述窗口内已知历史状态 | 0 行；未知比例 100% |
| SCD2 状态 | 5,224 证券，首次观察 2026-10-17；无已留档未上市/退市区间 |
| 五份财务表输入 | 各 36,447 行，合计 182,235 行 |
| 原公告/修订可见时间 | 182,235 行均未认证；未知比例 100% |
| 历史退市证券缺失比例 | 分母不可验证，仍为未知 |

未补造退市样本、行情或原公告证据，不能签收“严格历史研究数据完整”。
本次实际数据核对仅只读统计，未对运行中的数据做全量副本或覆盖。新可信度组件的
像素截图待 C3/整体界面验收补齐；没有提交、推送或部署。

## C2：标准公司行为与模拟盘事件账务

L3：新增标准 `CorporateAction` 与独立 Provider 能力/路由，账务放在
`strategy/paper_actions.py`，复用原模拟盘事务、锁及账本重放。
详细数据契约、适用范围与回滚见[公司行为](../../docs/corporate-actions.md)。

- rustdx 保留 TDX category=1 的分红、送转及配股，显式从每十股转换成每股。
  登记、到账、上市日不猜测；HTTP 自定义源也可映射完整标准字段。
  独立同步在盘后结算前执行，失败保留原快照、不跨源回退。
  100,000 事件上限；重复 ID、跨源同证券同除权日冲突拒绝写入。
- 新账户采用事件模式，旧账户缺模式字段时保留 `legacy_factor`。
  旧 `corp_action` 重放不变；不存在把价格因子自动迁移成真实权益的行为。
  账户固定印花税与回测历史税率明确区分，红利按税前金额记账。
- 权益取登记日收盘持仓。除权日撮合前确认应收红利和送转股；到账日转现金，
  上市日解锁。配股需主动选择数量并缴款；缴款可以早于除权，上市前作为应收股份，
  除权前按缴款成本、除权后有行情按原始价估值，缺价回退成本。
  应收不提供可用现金，应收股份进入组合仓位占用。
- 缺日期证据、事件修订、零碎股规则未知、迟到事件影响已发生成交时，
  暂停相关入账/撮合和新风险判断，不写该日定版净值。已过登记日的红利不因后续卖出消失。
  FIFO 卖出跳过锁定股份；整数零股只允许随全仓一次卖出。
- 新台账以 `(action_id, stage)` 幂等，原 SQL/RLS 继续隔离账户。
  文件模式追加后中断由完成标记重建；完成标记最后写入，正常轮询不反复写持仓。
  公开事件按证券索引并限制四份缓存；私有账本不进入公共发布。
  现金收入独立于交易配对胜率，不制造一笔红利“赢单”。
- 页面展示登记/除权/到账/上市日期、未知状态、已认购数量及明确缴款按钮；
  成功后沿原 `paper` 查询前缀刷新总览、现金和账本。CSV 包含公司行为及现金金额。
  旧程序不认识新账本，回滚需恢复配套的代码和私有数据备份。

新增测试先观察到 `ImportError: cannot import name 'paper_actions'`。最终执行：

```bash
# TSP_TEST_POSTGRES_DSN 为本轮新建的隔离 PostgreSQL 16，不使用运行数据库
.tools/bin/uv run --project backend --no-sync pytest \
  backend/tests/test_paper* backend/tests/test_capability_matrix.py \
  backend/tests/test_rustdx_provider.py backend/tests/test_corporate_action_provider.py \
  backend/tests/test_full_paper.py -q
.tools/bin/uv run --project backend --no-sync pytest \
  backend/tests/test_paper_corporate_actions.py backend/tests/test_corporate_action_provider.py \
  backend/tests/test_pipeline* backend/tests/test_data_release.py \
  backend/tests/test_initial_sync.py backend/tests/test_research_snapshot.py -q
cd frontend
pnpm test
pnpm build
```

第一组 **214 passed**（13.08 秒），覆盖实际 PostgreSQL 的重复执行、认购并发、
事务中途失败整体回滚、RLS、旧因子账户，及现金/数量/成本/总权益、
登记后卖出与除权后买入、配股前后估值、上市解锁、金额不足和文件中断恢复。
第二组 **96 passed**（19.86 秒），有重叠不相加为独立测试数。
前端 **192 passed / 40 文件**，最终构建通过（6.73 秒，保留原有大 chunk 提示）。
新模块、Provider、API、账本存储/导入及相关测试 Ruff 通过；
另九个既有文件共 107 项历史诊断，修改行零诊断。相关前端 ESLint 无诊断；
版本检查仍为 `0.4.2`，`git diff --check` 通过。

真实网络只读核对三只证券：rustdx 返回 **74 条、3 只，0.158 秒**，
其中 **74 条登记日缺失、日期认证为 0**。该耗时只描述这组三证券请求，
不是全市场性能证明；没有把这些不完整观察自动填入真实账户或运行数据。
原公告和清算日期、个人红利税、零碎股代售及配股退款仍不可认证/未模拟。

真实 React + FastAPI 临时账户联调：主动认购 200 股、缴款 1,000 元；
刷新后现金 **89,000**、应收 **1,000**、已认购 **200 股**。
旧账户显示因子估算边界；未知日期账户显示待核对且无认购按钮。
556px 视口浅色 `scrollWidth=556`，主题切换可操作；没有取得本轮人工检查的像素截图。
第一次夹具把应收 props 固定为零，已接实际总览接口并重新核对以上金额。
本轮浏览器标签已关闭，未提交、推送或部署。

自动审批拒绝停止本轮临时 Node 进程 PID 61147（3049），理由为缺少进程终止授权；
未绕过，改用 3051 端口完成总览联调。3049/3050/3051 临时服务和本轮隔离 PostgreSQL
曾暂保留；整轮回归完成后已删除隔离 PostgreSQL。此前 B8 的 3047/3048 清理限制
仍有效，3047–3051 既有服务未终止。不得把这些服务当生产部署。

## C3：真实链路分段量测与平台产物

新增只读输入、有界执行的 `backend/scripts/accept_market_pipeline.py`。它从真实
Provider 请求指定证券的带日期历史日线，经过标准化、Parquet 往返、指标计算、
平台内部 Arrow 快照、平台 SSE marker、API 侧解码及日 K HTTP 接口；输入运行目录
从不写入。历史数据不能认证实时源的新鲜度，脚本在结果中固定写入
`source_freshness_certified=false` 和 `trading_day_soak_certified=false`。

本次选择沪主板 `600036.SH`、深主板 `000001.SZ`、科创板 `688981.SH`、创业板
`300750.SZ`，窗口 2026-03-20 至 2026-10-16。10 轮每轮均取得 **560 条**记录，
末日均为 2026-10-16；输入和最新 enriched 哈希跨轮稳定，平台快照读取 10 次、
Arrow 帧解码 40 次、marker 重连 0 次。稳定轮次的中位耗时：

| 分段 | 中位数 | 最大值 |
| --- | ---: | ---: |
| Provider 请求及解码 | 482.57 ms | 767.89 ms |
| 标准化 | 1.16 ms | 1.44 ms |
| 日线分区写入 | 292.16 ms | 1,162.79 ms |
| 指标计算 | 9.90 ms | 20.81 ms |
| enriched 写入 | 246.50 ms | 1,911.99 ms |
| 平台 marker 传播 | 11.94 ms | 16.91 ms |
| Arrow 快照 HTTP 解码 | 39.61 ms | 49.43 ms |
| 页面日 K HTTP | 51.30 ms | 60.89 ms |

首轮写入包含目录和视图冷启动，故写盘最大值明显高于后续轮次。该夹具未提供复权
因子，指标按原始价格计算，并在结果中记录 `raw_without_supplied_factors`；这不替代
生产前复权链路验收。

另执行全股票池 rustdx 快照：**5,224 只、100/100 轮完整、最小覆盖率 100%**，
8 个持久连接，无失败或重试；P50 412.71 ms、P95 2,596.06 ms、P99 7,069.44 ms。
该观测发生在周六，源端单票时间只保留证据，不认证当日新鲜度、09:25 或收盘终态。

真实 React `StockDailyKChart` 通过同一临时 API 读取 140 根 K 线。556px 视口下
浅/深色 `scrollWidth=556`，Canvas 为 1048×952 物理像素、524×476 CSS 像素；
抽样 3,960 点中 544 点非透明、82 种颜色，证明图表非空。浅色及深色像素截图成功，
没有文字遮挡或横向溢出。截图：[浅色](screenshots/c3-market-pipeline-light.png)、
[深色](screenshots/c3-market-pipeline-dark.png)。扩展拉取页面补得
[浅色全页截图](screenshots/b3-b4-pull-light.png)；助手、Responses、
运行时页面和公司行为长页面仍因浏览器截图超时只保留 DOM、样式和无溢出证据，
未伪称像素截图通过。

当前源码实际构建新的 API 和 Compute 镜像，均为 **Linux ARM64**。两份
`build-manifest.json` 离线核验 `changed=[] / missing=[] / added=[]`；API 镜像内
rustdx 原生桥接为 `jelven-rustdx-complete 1.12.2 @ 904b33e / bridge 0.1.1`。
Compute 验收以 UID 10001、只读公共数据、无主机凭据和无网络运行，元数据、
自定义策略拒绝、独立恢复、回测、因子批量、优化、步进优化及挖掘产物均通过。
隔离 PostgreSQL 16 的 full-product 回归 **153 passed**，覆盖身份、RLS、模拟盘、
研究、会话和流式票据。

首次用默认 `BACKEND_EXTRAS=` 启动新 API 镜像时真实发现
`ModuleNotFoundError: No module named 'psycopg'`：单机和 full-product 已共用
模拟盘/告警 SQL 适配类型，但依赖仍只列在 `cell` extra，CI 默认镜像也没有传该
extra。现将 `psycopg[binary,pool]` 设为默认运行依赖，保留空 `cell` extra 兼容旧
构建命令并更新锁文件；安装/桌面契约 **28 passed**。重新构建后 API 容器正常完成
22 个策略与调度器初始化，`GET /health/live` 返回
`{"status":"ok","version":"0.4.2"}`。最终 API/Compute 清单各 **605 文件**，
相同文件索引哈希 `162fe4a32807278c6b19010748568c4c7cfba129d60b4e20f825f330764bf9c0`；
源码清单 **659 文件**，核验无差异。构建未声明 `SOURCE_COMMIT`，清单中的提交
身份保持 unknown，不把未提交工作区冒充固定候选。

### 外部 SSE 候选边界

新增 `backend/scripts/accept_external_sse.py`，每次创建随机 Docker network、
一次性 PostgreSQL、确定性平台 marker 流、候选 Gateway 和真实 Nginx，不复用
现有端口、数据库或服务。Nginx 精确转发 `/api/events` 和
`/api/events/ticket`，关闭缓冲；验收覆盖双库/三类 listener 就绪、浏览器会话换票、
票据单次消费、公共 `quotes_updated`、租户私有事件及 `Last-Event-ID` 重连恢复。

第一次真实启动发现候选 Gateway 在只读根文件系统导入 `pipeline_jobs` 时错误创建
`/app/data/job_store`；继续追踪又发现插件可用性探测读取密钥时会创建
`user_data`。现将管道作业存储、密钥、偏好及自定义源目录的写入角色统一限制为
`standalone/platform`，`api/gateway` 导入保持只读。空目录导入及相关回归
**54 passed**，观察器/测试 Ruff 通过。

修复后的 Linux ARM64 候选镜像
`sha256:e6150885794c432c29fef32ffccd1320d342d99bf81c4a8d97552fc7f3f3af4b`
通过外部边界：四项 readiness 全绿，公共首帧 **1,007.95 ms**，私有事件序号
1/2、重连恢复序号 3，票据重放返回 401，605 文件清单无差异。完整证据见
[external-sse-candidate.json](external-sse-candidate.json)。该环境为本机 HTTP
loopback 和确定性 marker，不含公网 DNS/TLS/WAN；这是最初单连接证据。

续验将脚本扩展为有界并发扇出：100 个客户端分别换取一次性票据，经同一真实
Nginx/Gateway 建立 SSE，在全部收到公共事件后再写入同租户私有事件。ARM64 候选
100/100 建连、100/100 公共事件和 100/100 私有事件均成功；换票 P95
**271.860 ms**，首个公共事件 P95 **1,072.443 ms**，私有扇出 P95
**980.095 ms**。单连接重连和票据重放拒绝继续通过，完整证据见
[external-sse-candidate-100.json](external-sse-candidate-100.json)。
这是 2.353 秒的本机突发功能验收，不替代持续容量或公网链路。

### Linux AMD64

Buildx `orbstack` 明确支持 `linux/amd64`。Docker Hub 首次返回 `Bad Gateway`，
改用 `public.ecr.aws/docker` 后成功构建：

- API `sha256:dbaea3f5531d6cd1ddad4078e7a389fa682a41a29214bba126ab4277bb65a97d`；
- Compute `sha256:ca9bdbaf4c3a6d7abdeb3c8e32381bb9dab6b2562ac5085f1eda043c19f95a94`；
- 两份清单均为 605 文件，文件索引哈希
  `36c5fbf728393667bc0d96fe527763844b6b4d1c23db526fb5995c43166f8c5b`；
- x86_64 wheel
  `tsp_rustdx_native-0.1.1-cp311-cp311-manylinux_2_34_x86_64.whl`
  实际加载为 `jelven-rustdx-complete 1.12.2 @ 904b33e / bridge 0.1.1`。

Compute 按真实 Broker 参数以 UID 10001、无网络、只读根/公共挂载和可写 `/tmp`
运行 `n_day_low_reversal` 元数据任务成功；AMD64 Gateway 再次通过完整 Nginx/SSE
验收，公共首帧 **1,012.19 ms**，见
[linux-amd64-sse-emulated.json](linux-amd64-sse-emulated.json)。
本机为 Apple ARM，QEMU 报告缺少 AVX/AVX2；因此这里只签收构建、原生桥接和功能
烟测，不签收原生 AMD64 宿主的 Polars 重负载、容量或性能。

续验首先让原 AMD64 Gateway 执行同一 100 客户端扇出：100/100 公共/私有事件
成功，换票 P95 **476.841 ms**、首个公共事件 P95 **1,130.449 ms**、私有扇出
P95 **994.272 ms**；见
[linux-amd64-sse-emulated-100.json](linux-amd64-sse-emulated-100.json)。
这些分位数受 QEMU 影响，只证明功能边界。

随后真实执行 Compute 的 `signal_backtest`，发现两个构建/隔离缺口：

- 原 Compute 镜像直接继承未安装 `backtest` extra 的运行镜像，协议虽公开
  `signal_backtest`，运行时却缺少 `vectorbt`；
- 补齐依赖后，`BacktestService` 会把结果写入只读 `/market/backtest_results`。

现由 `Compute.Dockerfile` 从冻结锁导出带哈希的 `backtest` 闭包，经镜像源补装并
检查 `vectorbt 0.28.2`；使用 `uv pip install` 保留基础镜像已有的
`legacy-cpu` 等包。Compute 调用 `BacktestService` 时关闭本地结果落盘，上层仍按
原协议持久化租户结果，单机默认落盘行为不变。首次直接 `uv sync` 因锁内下载地址
长时间无进展，已改为与基础镜像一致的冻结导出/哈希校验流程。

修正版 AMD64 Compute 镜像
`sha256:d89015b8c1d06896e96a65a709ec0d38b9a6e6028e251bbaeb1f8dc8223a0f2b`
以 UID 10001、无网络、无凭据、只读根和公共行情、可写临时目录运行完整验收：
元数据、私有策略、危险导入拒绝、独立恢复、策略计算、策略回测、
`signal_backtest`、因子批量、优化、Walk-Forward 和挖掘 Parquet 产物均通过；
605 文件清单哈希
`2ba593c55e51677decfc3e72541bbd1bd63ce434b2d77b04b367bccd5a16b512`
核验无差异。完整证据见
[linux-amd64-compute-emulated.json](linux-amd64-compute-emulated.json)。
直接绕过 Broker 且不设置 Numba 临时缓存不是受支持入口；按 Broker 的
`NUMBA_CACHE_DIR=/tmp/numba`、只读和 tmpfs 参数，vectorbt 与 rustdx 均实际加载。

### 隔离联合恢复

新增 `backend/scripts/accept_production_recovery.py`，在一次性 PostgreSQL 内建立
源 Cell 和独立恢复 Cell，实际执行两轮 `pg_dump`、最终停写快照、双库
`pg_restore`、v6 校验及运行角色重新授权，并同步恢复平台配置、公共 release/current、
私有产物和 Edge 配置副本。最终数据库对账 **46 行**，数据库总哈希
`5765dc9a9d626ab1fef8240889d824100e73dfab3d8f32ec625ddd3a22ba0e73`；
四类目录逐文件哈希一致。

恢复后实际核对 2 个既有会话、RLS 跨租户不可见、3 条持久事件、模拟盘账户/订单/
成交/持仓、成功研究任务及 artifact、公共 `release-002` 和平台配置 generation 2。
夹具最终备份捕获滞后 **0.232 秒**，恢复至全部业务核验完成 **0.846 秒**，无缺行；
这些数字只描述停写的本机小夹具，不是生产 SLA。切流后写入会先阻断盲目回退，
待数据库行和私有产物增量对账后才允许切回源端。完整证据见
[isolated-production-recovery.json](isolated-production-recovery.json)。

续验增加基于备份生成时字节数和 SHA-256 的恢复前门禁，并实际注入三类失败：
截断 control dump 在预检阻断；修改公共备份文件在目录预检阻断；绕过预检把截断
data dump 交给 `pg_restore --exit-on-error` 时以 EOF 失败。三次失败后流量指针
仍为 `source`，未进入切流。

随后正常流程再次通过：最终备份捕获滞后 **0.231 秒**，恢复 **46 行**且无缺行，
业务核验 RTO **0.672 秒**；切流后数据库和私有产物增量先阻断盲目回退，完成对账后
恢复 source。完整证据见
[isolated-production-recovery-failure-injection.json](isolated-production-recovery-failure-injection.json)。
这些仍是一次性本地 PostgreSQL 小夹具，不是生产主机灾备签收。

### 完整交易日与 72 小时

`observe_second_acceptance.py` 新增 `full-trading-day` 档：北京时间
2026-10-19 **09:15–15:40** 持续采样，秒级质量分成 09:30–11:30 与
13:00–15:00 两段，避免把午休当采样缺口；另记录 09:25、开盘、午休、午后、
收盘和 15:35 里程碑。观察器支持独立平台配置路径，缺省值按应用契约解析。
通过平台管理 API 已把本地候选平台秒级归档设为 `enabled/full/retention=7 days`；
周末 writer 不运行符合时间门控。

任务已由独立容器 `tsp-observer-full-day-20261019` 持久排队，状态和启动记录见
[observations/full-trading-day-20261019](observations/full-trading-day-20261019)。
当前仅为 `queued / waiting_for_market`，`trading_date_confirmed=false`，不能签收。

新增 `observe_candidate_soak.py`，每 30 秒被动采样 3018 API、3028 Platform、
3031 Gateway 深度健康，以及 `tsp-local-api`、Broker、PostgreSQL 镜像/启动时间/
重启计数和磁盘余量。72 小时窗口为 2026-10-17 15:17 至 2026-10-20 15:17，
容器 `tsp-observer-passive-72h-20261017` 正在运行，三个端点和三个容器基线均正常；
见 [observations/passive-72h-20261017](observations/passive-72h-20261017)。
该任务不生成用户或通知流量，终态只能签收被动可用性观察，不能替代 100 用户
持续负载。

2026-10-17 17:16 +08:00 续查时，完整交易日任务最后心跳为 17:16:18，
仍为 `queued / waiting_for_market` 且无错误。72 小时任务累计 **174 个样本**，
最后心跳 17:16:34；三个端点失败样本为 0，运行容器重启样本为 0。最初 3 个样本
曾因 Docker socket 权限未取得容器信息，15:19 建立基线后持续正常。两个观察容器
本身 running、重启数 0；继承 API 镜像健康检查造成的 Docker `unhealthy` 显示
不代表观察进程失败，状态文件心跳持续更新。两项任务均未停止或重建。

### Windows 可得验证

当前主机不能生成或运行 Windows 产物，且没有 `gh`，本轮未推送或触发
`workflow_dispatch`。静态与可移植验证 **60 passed**，覆盖桌面运行时、安装契约、
构建/安装包清单、rustdx 默认安装、版本源及发布 workflow；冻结锁解析 126 个包，
包含本地 rustdx 原生模块、`polars-runtime-32`、`polars-runtime-compat`、
pywebview/pythonnet 和 Windows 专用 winotify。新增契约把 `windows-latest` x64、
Windows 10+、双 Polars 内核、rustdx 前置校验、WebView2 签名、便携/安装版冒烟、
EdgeChromium GUI、静默卸载保留数据及 sidecar manifest 绑定为门禁。
续验确认本机仍无 Wine、PowerShell、Inno Setup 或 `gh`，Buildx 只支持 Linux；
远程 `main` 已到 `v0.4.7`，但不包含当前未提交候选，不能用主线产物冒充本候选。
上述契约复跑仍为 **60 passed**，Windows 候选产物和真机矩阵不变为未签收。

仍未执行并明确不签收：

- 完整交易日和 72 小时任务终态，以及 100 用户 72 小时主动负载；
- 09:25、午休、盘后终态及源端时间新鲜度；
- 经外部 Nginx 的百用户持续 SSE 容量、真实公网 TLS/WAN；
- API/Worker/Broker 故障注入与重启接管，以及真实生产主机恢复；
- 原生 Linux AMD64 重负载、Windows x64 runner 产物和 Windows 10/11 真机矩阵；
- 生产部署、切流或回退。

本轮没有替换 3018 运行服务；仅通过平台管理 API 开启本地候选秒级归档，并新增
两个只读观察容器。没有提交、推送、发布或生产部署。

## 整轮回归

首次后端全量得到 **3,463 passed、7 failed、121 skipped**。逐项核对后：

- 股票目录增加 `board` 后，竞价测试仍冻结旧字段集合；
- 自动挖掘夹具未提供现有 `_get_engine` 所需的 `strategy_engine`；
- 兼容 Parquet 扫描会为旧分区补 `quote_time_basis=null`，测试未包含加法字段；
- Tier A 实际新增 4 个模拟盘端点，且公司行为端点最初缺少响应模型；
- rustdx 清单测试未纳入新 `corporate_actions` 能力；
- 自选股批量测试用 `MagicMock` 触发多租户分支，不能证明单机 Parquet 去重。

分别更新真实请求夹具、加法 schema 期望、能力/端点清单，为公司行为接口补稳定响应
模型，并把 Tier A 文档从 65 个同步为 **69 个端点**。定向契约组
**108 passed**。自选股测试现使用实际 Starlette `Request`，重复
`600036.SH` 时仍只报告另外两只净新增；没有改变大小写敏感的证券代码业务规则。

第一轮修正后的全量在前端、浏览器和后台测试并发时出现一项渐进式策略测试竞争：
最后一条缓存已可见时，后台耗时文件可能尚在随后数毫秒的收尾写入。隔离复跑
**8 passed**；测试改为等待其真正断言的耗时记录，不改变生产执行时序。

最终实际结果：

```text
backend: 3470 passed, 121 skipped, 89 warnings (203.71s)
frontend: 192 passed / 40 files
eslint: 0 errors, 75 existing warnings
frontend build: passed (7.35s)
full PostgreSQL: 153 passed
version: 0.4.2
```

OpenAPI 定向回归、新增脚本及本轮契约文件 Ruff 通过；既有
`test_watchlist_ocr.py` 仍有 11 项历史中文标点/正则 Ruff 诊断，本轮修改行无新增。
`git diff --check` 和版本一致性检查通过。跳过项主要是按环境/可选依赖门控的测试，
没有把跳过计为通过。

## 本轮续验回归

2026-10-17 16:04 +08:00 在未提交工作区重新执行本轮新增和受影响契约：

```text
observer/platform/desktop/manifest/rustdx: 80 passed
gateway/stream/version automation: 73 passed
backend full: 3475 passed, 121 skipped, 89 warnings (239.66s)
frontend: 192 passed / 40 files
eslint: 0 errors, 75 existing warnings
frontend build: passed (10.05s)
rustdx native: 6 passed
```

两组定向测试有重叠，不相加为独立测试数。新增观察、SSE、恢复、构建清单脚本及
相关测试 Ruff 通过；`git diff --check`、版本一致性检查通过，版本仍为 `0.4.2`。
前端命令显式使用仓库内 Volta 环境。Rust 首次未指定 Python 时误取 Xcode
Python 3.9 并因缺少 `libpython3.9` 链接失败；按测试文档设置
`PYO3_PYTHON=backend/.venv/bin/python` 后，以锁文件重新执行并通过 6 项测试。

续验时完整交易日任务仍为 `queued / waiting_for_market`，最后心跳
`2026-10-17T16:04:48+08:00`，没有错误；72 小时任务为 `running / observing`，
已写入 31 个样本，最后心跳 `2026-10-17T16:04:53+08:00`。31 个样本的三个端点
均无失败；最初 3 个样本因 Docker socket 权限不足未取得容器信息，后续样本恢复，
最新 API、Broker、PostgreSQL 均为 running 且重启计数为 0。

两个观察容器自身均为 running、重启计数 0，但 Docker 显示 unhealthy。原因是
观察容器复用了 API 镜像并继承 `127.0.0.1:3018/health/ready` 健康检查，而观察
进程不在容器内提供 API；任务状态文件心跳和实际候选端点探测正常。续验没有为修正
显示状态而重建或停止观察容器。隔离 SSE 和恢复演练临时容器均已清理；两个观察容器
及原有运行服务继续保留。

以上仅完成当前可得回归和启动后观察，不改变完整交易日、72 小时、Windows 真机、
原生 AMD64 重负载、公网 SSE、生产主机恢复仍待终态或外部环境签收的结论。

## 第二轮续验回归

2026-10-17 17:16 +08:00 在未提交工作区完成：

```text
external SSE/gateway/stream contracts: 23 passed
Compute packaging/runtime affected tests: 18 passed
Windows/manifest/rustdx/version contracts: 60 passed
recovery integrity: 4 passed, 50 environment-gated PostgreSQL tests skipped
backend full: 3485 passed, 121 skipped, 89 warnings (254.54s)
version: 0.4.2
```

新增/修改的 SSE、恢复、Compute Worker、构建契约和验收脚本 Ruff 通过；
`backend/app/services/backtest.py` 仍有 9 项既有命名、中文符号和历史 `noqa`
诊断，本轮修改行没有新增诊断。`git diff --check` 与版本一致性检查通过。
本轮未修改前端，沿用上一轮已记录的 192 项前端测试、Lint 和构建结果，没有重复
宣称重新执行。隔离 SSE、恢复和 Compute 临时容器均已清理；观察容器及原有服务保留。
