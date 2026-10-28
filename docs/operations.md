# TSP 部署与运维手册

2026-10-28最新整合升级必须使用main质量门通过后的同一SHA和正式镜像，
先本地后生产。两端都要在停写后备份ETF/指数目录、身份库、业务库、
受控配置及release manifest，并完成临时恢复对账；新代码加载后才删除ETF目录和业务记录。
重建发布并启动后，在实时轮、盘后轮和再次重启后核对ETF不再生、指数指纹不变。
任何备份不可恢复、配置漂移、镜像身份不符、ETF再生或指数变化都立即停止并回滚。
执行清单见[统一发布评审](../reports/complete-release-20261028-3/review.md)。

2026-10-28数据采集统一需同时加载匹配前端与Platform/API代码。
升级后数据总览不再显示独立「数据修正」；从1分K或日K卡片设置进入离线补齐，
选择起止日期后执行全量或遗漏。遗漏先查本地，无缺口不依赖上游可用性；
有缺口才校验生效Provider。运行中任务保持启动时逻辑，待终态后再升级。
无需迁移Parquet、清空行情或重置账号、口令、SMTP及用户配置。

2026-10-28周K schema 修复仅改变后端读取入口，无Parquet迁移、清理或配置变化。
部署需让执行“生成数据”的Platform加载匹配源码；已失败任务保留，升级后重新发起周K生成。
禁止为消除`quote_ts`报错删除10月27日日K分区；账号、口令、SMTP及用户配置不变。

2026-10-28指数页交互修复仅需加载匹配前端资源，无数据迁移或配置变化。
升级后清理浏览器旧静态缓存并核对指数页；后端、账号、行情分区、管理员口令、
注册/找回口令及SMTP配置均保持。任务自动收起只影响展示，不删除任务历史。

2026-10-28历史K线性能候选改后端1分K日期分块及rustdx共用历史分页，
覆盖获取、全量/遗漏修正及相关资产，无配置或Parquet迁移。
更新须让行情写端Platform加载匹配源码；已启动任务保持原执行逻辑，
待其终态后再按固定发布产物升级，不为提速中断已有取数。
新分块最多32自然日/100证券；定位偏移仍受原最大页数约束，
探测页只在一次调用中复用，不跨任务缓存。内存峰值需按实际数据验证，
仍受源历史深度限制，未找到记录须检查真实剩余缺口。
同环境基准与未部署边界见[性能记录](../reports/minute-history-performance-20261028/verification.md)；
保留账号、行情、口令、SMTP和用户已保存的配置。

2026-10-28指数扩展升级应同时加载匹配前端、API与Platform，确认
`GET /api/index/catalog`含七只、`GET /api/index/kline`接受`1d/1w/1mo`，
`POST /api/index/repair`仅`data:manage`可执行且在Platform写入。
停写备份包含`kline_index_daily/kline_index_enriched/kline_index_weekly/kline_index_monthly`
和`instruments_index`；公共发布先更新Platform再更新消费者，新增目录无需SQL账户schema迁移。
缺周/月数据先核对本地指数日K，再执行对应周期修正；遗漏不覆盖已有记录，
全量按选定整周期重建。取消后等待任务终态；检查实际分区及剩余缺口。
本轮仅源码，未更新运行副本/线上或采集真实行情，见[验证记录](../reports/indices-kline-20261028/verification.md)。
保留管理员、口令、SMTP及用户后续保存的配置，版本由main CI分配。

2026-10-28源码清理移除未使用的`platformdirs`依赖及打包元数据，数据目录定位仍沿原配置。
无需迁移或重置用户数据，版本及验证边界见[项目精简记录](../reports/project-cleanup-20261028/verification.md)。

2026-10-27本批常规整合升级保留已有行情、账号及配置，不重复之前的K线清理。
按主线固定源码/产物→分支归档清理→本地停写备份恢复/升级→线上同样检查顺序执行；
Platform快照先于消费者，真实登录/业务/配置与完整functions分别验收。
阶段和水位见[本批记录](../reports/latest-release-20261027/verification.md)，下方旧部署条目保留当时事实。

同批补入历史日历；升级保留旧研究快照，新研究捕获新的日历身份。
本次常规升级不自动重新执行多年补采任务，历史覆盖按后续实际缺口核对。

2026-10-27本地自选补丁已更新到`local-watchlist-20261027`目录，真实入口3021/3028；
控制器ROOT已持久切换，原v0.4.31版本号和Compute基线保持。停写双库备份已在临时库
实际恢复对账；原16条自选、1组、管理员及SMTP保留，运行时间字段按正常调度前进。
自选页面挂载始终重读清单，避免2秒缓存写入节流使快速刷新显示旧策略关系。
恢复默认排序、跨组全量CSV和多策略已实测，详见
[本地验证](../reports/local-watchlist-update-20261027/verification.md)；本轮没有ECS更新。

2026-10-27当前ECS已升级v0.4.32并清空有效六周期K线，实测及匹配备份见
[本次记录](../reports/online-refresh-20261027/verification.md)。账号、双库schema v6和用户配置保留，
基础ready，缺指标的研究/监控/模拟交易未就绪；上线后设置API再次开启自动同步，
保留该新设置，后续自动任务可能重新生成K线。旧带日期“当前/候选”描述须按
[版本基线](version-baseline.md)判断。先等待活跃任务退出，固定产物后停写备份双库、
在临时库实际恢复验证，再按Platform目录/缓存→消费者顺序启动。
保留admin、口令、SMTP和用户随后修改值，重启后核对登录和全部functions。

2026-10-28本地源码的离线补齐规则：
5/10/15分K从本地1分K生成，周K从本地日K生成。
1分K和日K均选择起始/结束日期，并使用「获取全量数据」或「获取遗漏数据」；
日K获取后继续同步除权因子、重算指标，按生效数据源能力及运行任务状态禁用按钮。
全量覆盖所选区间同键记录，遗漏只补本地真实缺口。
空目标表或缺源能力不禁用按钮，基础不足时任务失败并提示先补基础K线。
生成结束核对实际分区及画像，日K历史获取另核对原始日K、Enriched和复权结果；
任务运行中等待完成或取消后再重试。运行进程须加载匹配前后端代码，生产状态仍见版本基线。

Windows关闭后进程残留需区分应用清理、日志关闭和原生系统退出；
v0.4.28诊断确认前两阶段已完成，CRT触发DLL卸载时仍阻塞。
冻结Windows改用`TerminateProcess`保留返回码，正式发布仍须重新通过GUI与退出烟测。
该修复不重置数据和配置，不修改ECS或其他平台的退出方式。

六周期画像升级应备份新增四目录 `kline_5minute/kline_10minute/kline_15minute/kline_weekly`
及受控preferences中的 `kline_sync_<周期>` 配置，公共数据发布同时包含四目录。
1分钟旧前复权数据沿既有取数基准继续兼容，TickFlow保留原adjust参数；
5/10/15分钟直接继承所读1分钟的基准，任务结果记录raw或legacy_1m，不批量改写历史。
分钟周期同步依赖北京时间交易日日历、总自动同步开关及对应周期开关，历史补齐仅手工触发。
5/10/15分钟由本地1分钟计算后入库，周K在周六由本地日K聚合；派生周期不请求上游。
缺少本地基础K线时无输出，并提示先补1分钟或日K。
巡检对照任务结果与实际覆盖，不把零行或部分日期当作全量初始化完成。
本地更新不修改版本或管理员/口令/SMTP配置；线上升级仍按固定发布身份执行。
日K画像卡片参照周K统一同步及记录统计文案，仅前端模板更新，沿原设置执行同步。

数据总览已删除独立「数据修正」入口。1分K、日K卡片设置中的离线补齐
使用行情页同款日历，结束默认今天，起始≤结束≤今天，两种获取方式均包含结束当天。
旧`repair_daily`接口不传period/mode/end_date仍为全量日K到北京今天，
旧`value/unit`调用也仅作兼容。
历史日K全量修正核对股票/指数和因子窗口、区间外分区保留及分钟采集未附带执行；
复权与技术指标可能因历史依赖更新区间外派生值。非法结束日期应在建任务前拒绝。
遗漏先查询本地实际缺口，再校验Provider和能力；只补缺少的证券/时间记录，
保留已有值和并发同步结果；无缺口不请求也不解析数据源。
未知交易日历时缩小范围或配置可靠日历源，不能用工作日近似补造。
5/10/15分钟或周K需重建时，先在1分K或日K卡片补基础数据，再到对应卡片「生成数据」。
旧派生周期遗漏API不拉取日历，未知年份须已有可靠日历缓存，否则缩小范围。
1分钟检查对应生效数据源的周期支持。非日K任务返回零行会失败并给出原因，
此零行失败规则适用于全量获取；遗漏任务无缺口零行成功，未补齐则显示已补/剩余行数。
部分日期/证券有输出时仍应核对实际覆盖；不以执行100%替代完整性验收。
无需清库、迁移旧任务记录或更改用户配置；运行进程加载本次前后端代码后入口生效。
日K遗漏只重算补入证券的本地复权/指标；若需重新获取除权/指数等附属数据，使用全量模式。

新周期查询可沿现有minute/daily接口检查freq及返回日期。页面设置旧值异常时自动使用
有效默认项，正常显隐与顺序保留；无需清空全部浏览器配置。日K空库可直接选择明确起止日期，
点击全量或遗漏获取并继续除权/指标流程；确认任务与对应分区后再判断完成，
数据源缺能力不跨源回退。

个股详情六周期下拉需前后端同时加载本次代码，沿`/api/kline/daily?freq=...`
检查所选周期与分钟时间；原不传freq的日K调用不变。5/10/15分K查询默认根据
本地1分K计算，无基础结果时兼容已有周期分区；为空时先核对并补齐1分K，
再到对应卡片生成数据，离线补齐不要求上游具有分钟能力。
默认/放大/缩小只调整可见根数，不写行情或用户配置；数据不足先检查日期范围与源能力。
无需迁移数据、清理缓存或重置管理员/口令/SMTP；源码更新与生产切流分别验收。

用户明确要求清空线上六周期K线时，先关闭自动同步并取消相关采集任务，等待执行体退出；
停写备份与恢复核对后，仅清理股票、指数K线及对应enriched目录。
保留证券目录、除权、财务、账号和业务历史，重建视图与指标缓存并发布新的共享数据版本。
核对源目录、当前共享清单及数据画像均无六周期数据；历史研究引用和受控备份单独保留。
清空后保持自动同步关闭，避免启动初始化和定时写回；缺行情的就绪告警属于预期状态，
不能把它标为全量初始化完成。需要重新采集时由用户在数据页面明确开启或手动获取。

2026-10-26本地派生分钟调整验证见[本地聚合记录](../reports/local-derived-minutes-20261026/verification.md)。
本次未部署、未执行用户行情回填；现有分区保留，完整历史覆盖仍需独立核对。

进度修复需要后端进程加载新源码；前端更新即可使用周期名称与结果字段。
正在执行的历史任务仍沿其启动时逻辑运行，不能为更新显示而中断该任务；
待任务退出后再更新运行进程，核对总/阶段进度及对应分区行数。
本次不迁移任务记录、不更改已存行情或受控配置，成功100%不表示所有预置数据已完成。
页面设置点击异常本地尚未复现，保留错误/环境证据继续定位，不通过清空浏览器配置处理。

2026-10-25 本批最新现状、环境更新与验证见
[最新记录](../reports/current-state-release-20261025/verification.md)。
新部署重点确认自动同步默认值、非 Windows 多租户基础服务、SMTP受控配置、
已有偏好与身份保留，以及[最新加五个历史版本](release-operations.md#31-版本保留)的恢复集合。
源码归并不自动升级生产；生产固定版本继续见[版本基线](version-baseline.md)。

本手册统一部署、配置、账户初始化、Windows运行、巡检、备份、恢复和交接。先核对[版本基线](version-baseline.md)，再按部署模式执行；正式放行见[发布与服务运营](release-operations.md)。目标组件尚未接入，不由文档生成可用配置或已签收SLA。

- [运行边界](#1-运行边界)
- [安装与配置](#2-上线前准备)
- [启停和健康](#3-启动停止与状态)
- [备份与恢复](#8-备份)
- [升级与故障](#10-升级与回滚)

<a id="strategy-history-backup"></a>
策略结果归档随业务备份恢复：多租户位于PostgreSQL业务库的
`business.documents`（`strategy_result_history` namespace），沿用双库备份流程；
独立模式位于`DATA_DIR/user_data/strategy_results.sqlite3`。
在线备份SQLite应使用其backup API，不能只复制正在写入的主文件而遗漏WAL；
完整停机备份可纳入整个数据目录。恢复后验证A→B→A、旧版本明细及租户隔离。
归档无自动过期或自动删除，巡检纳入业务库/用户目录空间增长。
用户可在策略历史结果中筛选并确认删除单条归档；该操作无法撤销，
恢复须使用已有业务备份，重算只生成新的结果。删除不清策略配置、监控或旧当前快照。
权限不足返回403，不存在返回404，存储故障返回503；检查数据库/磁盘权限及健康后重试。
升级同时替换API和Worker/Compute的归档代码，以保留同ID重建的缓存标识契约；
旧归档无需迁移。本地开发和验证不修改管理员、口令、SMTP或应用版本。
同策略同日同配置的重复结果复用已有明细和计算时间，仅必要的最新任务顺序及输入来源
更新摘要；迟到旧任务和同任务重试不写入。升级后用单跑/批跑重复运行验证仅一条归档，
计算时间保持原值且重启可读；结果变化更新当日快照，参数/版本不同仍分别保留。
不要用清空`strategy_cache.json`恢复历史；它只是兼容当前快照。
写入失败先排查空间、权限和数据库健康，修复后重跑指定日期；
读取失败不作为“无记录”触发全量补算。新源码不自动替换生产或迁移旧缓存。
独立服务首次并发保存须使用包含WAL初始化串行修正的源码；已存在的归档及
WAL文件保持原位。升级验证共用与多个仓库实例均能完整保存结果，不靠清库恢复。

<a id="1-运行边界"></a>
## 1. 运行边界

- 原生 Windows 已暂停多租户，使用单进程管理员个人空间；旧平台、worker、
  watchdog 的停用和备份切换步骤见[Windows 单机说明](#windows)。
- 当前完整单机多租户运行 Platform、API、独立 Gateway 和四类 Worker，API 默认四进程；
  PostgreSQL、公共版本、Broker、备份和恢复见[完整部署](../deploy/full/README.md)。
- 2026-10-25实测新ECS `118.196.108.188` / `https://shanao.asia`
  已运行v0.4.21/7823b84b/schema v6，七应用角色及PostgreSQL健康。
  本批后续升级状态以[版本基线](version-baseline.md)为准；旧v0.4.11/092bd79c
  和inode修复是历史实例记录，不能继续作为此实例当前部署值。
- 自选业务连接池五秒获取超时仍在调查，不调大池或延长截止时间代替根因修复。
  保留 PG 等待状态、主机 I/O/内核栈、请求耗时的联合时间线及已有失败记录；
  原诊断与完整交易日观察仍待终态，见[调查](../reports/online-timeout-20261021/watchlist-pool-investigation.md)；
  新实例健康不能证明旧故障根因已修复。
- 新源码的五档/收盘价与 Windows 单机预算尚须独立发布验收；
  原生 Windows 升级备份管理员身份与完整数据目录，旧多租户服务必须先停写，
  见[本次整合](../reports/local-consolidation-20261022/verification.md#local-contract-2026-10-22-本地变更整合与生命周期基线)。
- 旧实例2016起日线[回填结果](../reports/history-backfill-20261020/result.md)
  不适用于新ECS。新实例种子日线为2025-10-27至2026-10-23，已补119个分钟源分区；
  指数enriched、真实邮件投递和模型等未完成项继续单列，不称完整初始化。
- 下方根目录 Compose 操作适用于独立运行模式，其生产形态是单容器、单 Uvicorn worker。
- 独立模式同一`DATA_DIR`只允许一个主实例写入；完整多租户由Platform单写公共行情，API/Gateway/Worker按既有角色及SQL事务运行。
- 默认服务端口为 `3018`。
- Docker Compose 将主机 `./data` 挂载到容器 `/app/data`。
- Compose 使用 `restart: unless-stopped`；镜像内置容器 `HEALTHCHECK`，调用
  `GET /health/ready`。外部告警系统仍由部署环境提供。
- `GET /health` 和 `GET /health/live` 是进程级浅探活；
  `GET /health/ready` 是本地依赖深度检查。
- 当前没有已签收的固定SLA保证；各阶段的可用性、RPO/RTO、持续窗口与恢复证据统一见[联合验收目标](testing.md#acceptance-4-负载持续窗口与恢复目标)。巡检与恢复记录须注明实际达到的水位及耗时。

该ECS通过`tsp-full.service`及`/opt/tsp-full/operations/start_full.py`完整启停，
先等Platform目录任务与数据快照就绪，再创建API/Gateway/Worker。
`tsp-full-health.timer`核对完整业务功能，公共回收timer保持引用保护。
更新固定Runtime/Compute和Broker源码前，停止应用写入者并备份双库、角色、
平台配置、启动器/Compose、Edge和数据目录；双库须实际恢复验证。
同schema代码回退保留当前双库，只有完成升级后增量对账才允许数据恢复。
管理员、注册/找回口令、SMTP和用户后改偏好均保留，真实授权码不写入报告。
运行手册每次部署/故障/恢复后更新，至少每月复核；拓扑或命令漂移即标记待复核。

<a id="2-上线前准备"></a>
## 2. 上线前准备

<a id="21-主机"></a>
### 2.1 主机

- 校验 CPU 架构与镜像或桌面产物匹配。
- 为代码、镜像缓存、日志、备份和全量历史数据预留磁盘空间。
- 校准系统时间；业务计算使用北京时间，但错误系统时钟仍会影响日志和 TLS。
- 公网只开放必要端口，优先通过 HTTPS 反向代理访问。
- 不在主机命令历史、Git 或工单中记录密钥和明文密码。

<a id="22-配置和持久化"></a>
### 2.2 配置和持久化

```bash
cp .env.example .env
chmod 600 .env
mkdir -p data backups
```

上线前至少确认：

- `PORT` 和监听地址符合网络规划。
- `DATA_DIR` 指向持久化位置。
- 公网部署已创建邮箱账户。
- 数据源、AI 和通知密钥仅保存在受控环境中。
- Docker 的 `data` 挂载、`.env` 和 `tiers.yaml` 路径正确。
- 不会有第二个容器或进程写同一个数据目录。

<a id="installation"></a>
### 2.3 按模式安装

完整多租户使用[deploy/full](../deploy/full/README.md)的角色部署；以下根Compose与Dev命令用于独立模式。固定验收提交或标签，生产前端构建为静态资产并由应用托管。

<a id="install-docker-compose"></a>
#### Docker Compose

<a id="install-前置要求"></a>
##### 前置要求

- Git
- Docker Engine
- Docker Compose v2
- 建议至少 4 GB 可用内存和足够的数据盘空间

<a id="install-首次部署"></a>
##### 首次部署

```bash
git clone https://github.com/jelven007/TSP.git
cd TSP
cp .env.example .env
docker compose up --build -d
```

访问 <http://localhost:3018>。

基础镜像默认从 Docker Hub 拉取。若 `registry-1.docker.io` 超时或返回
`Bad Gateway`，可通过构建参数切换拉取仓库：

```bash
BASE_IMAGE_REGISTRY=public.ecr.aws/docker docker compose build app
```

也可在 `.env` 中保存 `BASE_IMAGE_REGISTRY=public.ecr.aws/docker`。
直接构建使用 `docker build --build-arg BASE_IMAGE_REGISTRY=public.ecr.aws/docker -t tsp:local .`。
该参数只替换 Node、Python、Rust 基础镜像的仓库前缀，版本和依赖锁不变；
使用备选仓库时应核对镜像摘要。`USE_CN_MIRROR` 仅控制构建中的 npm、PyPI、
APT 和 Cargo 源，不控制 Docker Hub 拉取。

查看状态和日志：

```bash
docker compose ps
docker compose logs -f app
curl -fsS http://localhost:3018/health/live
curl -fsS http://localhost:3018/health/ready
```

Compose 会把以下内容挂载到容器：

- `./data` → `/app/data`
- `./tiers.yaml` → `/app/tiers.yaml`
- `./.env` → `/app/.env`
- 主机 Codex 配置目录 → `/root/.codex`，只读

生产环境应限制 `.env` 和 `data/` 的文件权限，并把数据目录放在有备份和容量监控
的磁盘上。

<a id="install-dev-模式"></a>
#### Dev 模式

<a id="install-前置要求-1"></a>
##### 前置要求

- Python 3.11+
- Node.js 20+
- [`uv`](https://docs.astral.sh/uv/)
- pnpm 9
- [Rust 编译工具链](https://www.rust-lang.org/tools/install)（源码安装默认原生桥接）

macOS / Linux：

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

- 前端：<http://localhost:3011>
- 后端：<http://localhost:3018>

自定义端口：

```bash
BACKEND_PORT=8000 FRONTEND_PORT=5173 ./dev.sh
```

手动分别启动：

```bash
cd backend
uv sync --frozen
uv run --frozen --no-sync python -m app.plugins.rustdx.client
uv run --no-sync uvicorn app.main:app --reload --port 3018
```

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

rustdx 原生桥接是后端默认依赖，不需要额外指定 `--extra rustdx`。开发脚本在
已有 `.venv` 时也同步依赖，并在启动前校验桥接。Docker 镜像和桌面包在构建时
内置扩展并校验，目标机器不需要 Rust。旧源码环境的桥接不匹配时执行
`uv sync --frozen --reinstall-package tsp-rustdx-native` 重建。

<a id="install-桌面安装包"></a>
#### 桌面安装包

桌面安装包从当前仓库的
[GitHub Releases](https://github.com/jelven007/TSP/releases) 获取。
仅使用实际存在且校验通过的平台资产。

桌面版与服务端版的明确差异：

- Windows 桌面版与原生源码版暂停多租户，使用默认账号 `admin/Admin123` 登录，
  提供管理员个人空间、改密与退出；不提供注册和邮箱找回。
- macOS、Linux 默认使用多租户账户登录，开放注册、密码找回、账户管理和退出；
  部署须准备 PostgreSQL 与内部平台服务。显式关闭多租户的旧冻结桌面模式仍可免登录。
- 非 Windows 服务端和 Dev 模式继续保留原认证。

Windows 原生服务只运行一个 standalone 进程。资源配置、旧多租户服务停用和
目录切换见[Windows 单机管理员说明](#windows)。

覆盖安装前仍应备份安装目录下的 `data/`。安装包不会主动删除运行时数据，但备份是
升级和回滚的前置条件。

<a id="install-老-cpu-兼容"></a>
#### 老 CPU 兼容

CPU 不支持 AVX2/FMA、进程出现 `exit 132` 时，在 `.env` 设置：

```ini
BACKEND_EXTRAS=legacy-cpu
```

同时需要回测依赖：

```ini
BACKEND_EXTRAS=legacy-cpu backtest
```

然后重新执行 `docker compose up --build -d` 或启动脚本。不要用
`POLARS_SKIP_CPU_CHECK` 隐藏硬件不兼容。

<a id="install-codex-cli"></a>
#### Codex CLI

Compose 默认只读挂载 `${HOME}/.codex`。Windows 的 `HOME` 可能未设置，可在
`.env` 指定：

```ini
CODEX_HOME_HOST=C:\Users\your-name\.codex
```

覆盖容器内 Codex CLI 版本：

```bash
CODEX_CLI_VERSION=0.144.3 docker compose up --build -d
```

只在可信主机启用 Codex 登录态挂载。

<a id="install-反向代理"></a>
#### 反向代理

公网部署至少需要：

- HTTPS 终止。
- 正确转发 `X-Forwarded-Proto`。
- 支持 SSE 长连接并关闭代理缓冲。
- 为回测和同步接口设置合理超时。
- 不直接暴露数据目录和配置文件。

反向代理上线后重新验证登录、SSE、回测进度、文件下载和更新检查。

<a id="configuration"></a>
### 2.4 配置、默认值与生效方式

配置分启动环境、持久偏好和密钥；页面仅修改已开放字段。进程角色、线程、连接池和资源硬上限须由部署者调整环境并重启；下述策略并发数可在页面保存后动态生效。完整多租户的参数与密钥文件以[完整部署配置](../deploy/full/README.md)为准。

完整单机资源配置补充：

| 配置 | 作用 |
| --- | --- |
| `TSP_*_MEMORY` / `TSP_*_CPUS` | 各 Compose 角色的内存和 CPU 上限 |
| `TSP_*_POOL_SIZE` | API、Gateway 及四类 Worker 的每库连接池上限 |
| `TSP_*_CONCURRENCY` | 四类 Worker 的并行执行上限 |
| `TSP_COMPUTE_CAPACITY` | 部署允许的最大 Compute 并发数，页面配置不能超过此值 |
| `TSP_COMPUTE_MEMORY_MB` / `TSP_COMPUTE_CPUS` | 单个 Compute 的原始资源上限，实际分配还受策略总预算约束 |
| `TSP_COMPUTE_QUEUE_SECONDS` | 执行槽满时的最大短等待时间，范围 0–30 秒 |
| `TSP_COMPUTE_CONFIG_FILE` | Broker 只读的全局 `preferences.json` 路径，须与 Platform 写入路径对应 |

`deploy/full/production-16g.env` 是单 Compute 的 16 GiB 预算，应用角色 8,704 MiB、
双库连接上界 36。该文件不含密钥；DSN、内部 Token 和外发凭据仍放在权限 0600 的
私密环境文件中。

<a id="strategy-concurrency"></a>
#### 并发策略

在“设置 → 系统设置 → 并发策略”手工输入1～999整数，失焦或按Enter保存；
首次未配置时显示当前推荐数字，“使用推荐值”恢复随执行主机估算的推荐模式。
完整多租户仅 `settings:manage` 管理员可配置，管理员缺省使用推荐值，租户默认1，
租户系统设置只显示1且不可编辑。所有租户共用一个主机 Broker 的预算。
该数量表示同时执行的计算任务；一次批量任务内部逐策略执行，普通租户一个活动槽，
管理员可在全局额度内并发；策略页批量任务排队保持有效。

执行主机的总内存与总 CPU 各取 50% 给策略任务。16 GiB 只是示例：其策略
内存预算为 8 GiB；实际页面取 Broker 执行主机资源，不能取浏览器或 API 容器资源。
页面百分比由返回的`budget_ratio`显示；升级须同步Compute和独立模式后端，
旧服务未升级时仍返回原预算，不能仅更新页面就视为50%生效。
推荐值取内存预算除以单任务内存上限、CPU 预算除以单任务 CPU 上限、部署并发上限
三者中的较小整数；资源足够至少运行一个任务时最低为 1。默认单任务参考限额为
4096 MiB / 2 vCPU。推荐依据限额估算，并非实测峰值或已验收容量。

手工值可高于推荐值及当前资源上限，保存范围为1～999；实际生效数仍取保存值与
资源/部署上限中的较小值。既有Broker部署容量上限16不因输入范围扩大而变更。
每个新任务的内存/CPU 额度取原限额与“总预算 ÷ 并发数”的较小值，
最低准入资源为 256 MiB / 0.25 vCPU；CPU 配额向下取六位小数。
提高并发可能降低单任务资源、增加耗时或导致内存不足。
配置写入全局偏好并原子保存，重启保留；降低并发不终止旧任务，旧额度继续计入
总预算，新任务须等待并发及内存/CPU 均有余量后才能进入。

页面同时显示保存值、推荐值、实际生效值和运行中任务数。资源不足时禁止新增；
资源缩小时保留已存值并限制实际生效数。Broker 不可用或未接入配置文件时拒绝
保存，路径不一致时提示“已保存但尚未应用”，不得当作生效成功。
原生单进程模式采用同样并发准入，内存预算仅为估算，不提供 Docker 内存硬隔离。
单主机 Broker 的共享预算不能外推为跨节点全局调度。

升级须同步前端、API/Platform与Compute的配置校验，否则旧服务会拒绝或忽略大于16的保存值。
进入策略页自动运行已删除，旧偏好不再生效；策略运行及监控启停由用户手动触发。
系统设置外链标题为“个股外链”；开源区域仅仓库链接、最新版本号和发布时间，
加载/空发布/网络错误保留反馈，后台观察仍沿原共享查询。此源码修改未更新运行副本或线上。

目标范围候选升级须同时更新API、Worker与Compute镜像，避免只升级界面。
自选目标配置仍在策略覆盖记录，成员运行时解析；不需要schema迁移。
备份包含原策略覆盖、分组和成员；旧版本不理解目标字段，回退前确认目标策略已停用
或恢复兼容配置，不能把新目标限制被忽略视为正常结果。分组删除409先核对本租户策略
引用并通过界面解除；不要直接删除SQL行或JSON。文件模式新组CSV写失败恢复原文件，
进程异常中断时仍应核对分组/成员一致性。当前功能候选尚未更新生产。
清空全部或分组返回409时同样先解除策略引用；任一组被引用时不能通过切到其他组
清空全部自选，但未引用组可单独清空。升级后抽查同股跨组与“全部”去重，
停用监控、修改目标分组成员后再启用，确认目标重读；加载失败应保留停用状态。
管理自选分组可单选并导出该组的完整股票列表，复用当前空间已有清单接口，
只需更新前端产物，无新增后端API或数据库迁移。读取失败/空组不下载文件；
导出故障先核对当前空间、自选清单接口和浏览器下载权限。CSV默认仅股票代码列，不包含名称、备注、
加入日期或组配置，不代替用户数据备份；本地验证不表示生产已更新。

CSV股票导入从自选页批量导入入口执行，在候选中选择目标分组后确认。
管理分组弹窗已移除CSV导入，仍可新建分组及导出。默认列表按文件首现顺序展示；
若已主动按字段排序，先取消该排序再核对导入顺序。升级须同时更新API后端和前端，
保持批量正序保存契约；历史列表不会自动翻转，不需要数据库迁移或清理自选数据。

2026-10-27自选关系候选升级须同时加载匹配前后端：文件模式新增`strategy_ids`列，
读取旧文件不改盘，首次写回保留`.bak`且不覆盖已有备份。SQL仍使用既有schema v6，
关系列保存在租户`business.documents`中，双库完整备份的documents计数和恢复对账继续覆盖；
完整文件转SQL业务导入也保存关系。CSV仅股票代码，不能恢复分组、备注或策略标记。
验收抽查一股多组/多策略、重新导入及置顶保留关系、删除后重加无旧关联，
恢复默认排序保留筛选。页头导出按钮仅保留图标，按点击时选中的全部、未分组或具体组
导出该分类完整代码；与自选清单的`group_ids`核对，其他筛选及行情加载不改变范围。
文件名对应分类，空分类/读取失败不下载；该导出改动只需更新前端，无API或数据库迁移。
不可用策略可解除，保存失败保留原关系；目录未就绪时可重试。
本轮只验证源码及隔离预览，实际运行版本见[版本基线](version-baseline.md)。

<a id="config-数据源"></a>
#### 数据源

```ini
TICKFLOW_API_KEY=              # 留空 = None 模式(历史日K免费);填 Key = 按订阅档位解锁
```

项目首次启动时七项数据能力默认路由到 `rustdx`；仅已删除或非法的源值回退默认，
已保存的合法路由保持不变。设置页的数据源卡片及可选路由标签按 rustdx、fuyao、
其他插件/自定义源排序，TickFlow 卡片、Key 配置入口和可选标签已移除；
已有 TickFlow 路由继续显示实际来源，不自动迁移。YAML 声明自有接口见
[custom-data-source.md](reference/integrations.md#http),插件开发见
[plugin-development.md](reference/integrations.md#plugin)。

「设置 → 数据源」的能力路由、数据源能力标签和详情表统一显示实时、K线、除权、财务四类。
新增数据源的编辑列表为实时、K线、除权、公司、财务；K线内分别填写日K与分钟K。
财务像除权一样预填字段映射，包含公告日、报告期、指标、三大报表与股本；按上游实际
字段填写，空映射草稿不保存。财务测试使用 `table=metrics`，缺失字段不补造。
K线一次选择同时更新日K与分钟K，只列同时适配两者的源；已有不同选源会分别显示，
打开页面不重置。“套用”仅调整这四类，K线未完整适配则使用默认 rustdx。
五档与公司行为保留内部数据集契约和已存配置，页面不再提供单独选源卡片。
仓库内置的 `rustdx` 插件无需 API Key，四类均已适配；分钟历史与盘中落盘共用分钟选源。
Docker 镜像和桌面包已包含原生扩展；
源码环境具备 Rust 编译工具后，执行 `uv sync --frozen` 即默认安装；
启动和构建时校验桥接版本与接口。公开通达信服务器可能限流或不可达，安装、
环境变量、单位校准、竞价采集及覆盖边界见
[rustdx 数据源](reference/integrations.md#rustdx)。
rustdx v2.0.4请求共用最多8条业务连接及1条独立探测连接，历史财务共用业务槽并独立解析。
旧环境中的35会被钳制到8；后台默认最多7。配置变化先关闭旧池，跨进程共享额度目录
变化要求重启。固定源码与离线构建说明见[原生运行时](reference/integrations.md#rustdx-原生运行时)。
选择任一非 TickFlow 源后，请求失败不会静默切回 TickFlow。

- **留空(None 模式)**:通过 free-api 使用历史日 K(当日数据盘后 1-2 小时可用),**无需付费**即可体验核心选股/回测功能
- **填入 API Key**:按你的订阅档位解锁更多能力

<a id="config-tickflow-备选源按档位提供的实时能力"></a>
##### TickFlow 备选源按档位提供的实时能力

| 档位     | 实时能力                                 |
| :------- | :--------------------------------------- |
| Free     | 自选页前 5 个标的实时监控(最低 6 秒刷新) |
| Starter+ | 全市场实时行情                           |
| Pro      | 分钟 K + 盘口                            |
| Expert   | WebSocket + 财务数据 + 全市场分钟批量端点 |

> 完整能力矩阵见 [tickflow.org/pricing](https://tickflow.org/pricing/),高等档位含较低档全部权益。
> TickFlow 保留后端兼容支持；设置页已移除其配置卡片，已有部署可通过 `TICKFLOW_API_KEY` 维护。
>
> **档位仅适用于 TickFlow 数据源**。功能门槛的统一标准是"能力"(`kline.minute.batch`、`depth5.batch`、`financial` 等能力键):其他第三方/自定义数据源以声明的数据集能力为准,系统会按当前数据源配置自动合并判定,UI 提示一律以能力名表达,不再依赖 TickFlow 档位名。

<a id="config-全量分钟-full_minute"></a>
##### 盘中分钟落盘

盘中把全市场当日1分K持续增量落盘到本地 `data/kline_minute/` 当日分区，
分钟策略与分时视图读取同一数据。选源统一在 **设置 → 数据源 → 分钟K**；
开关与间隔在 **设置 → 实时监控 → 盘中分钟落盘**。

- **已有 TickFlow 路由**：Expert 权限提供修复轮 `intraday.batch` 与稳态
  `intraday.universe`，低档位仍可有历史分钟但不能运行全市场落盘。
- **插件/自定义源**：声明 `minute`；可实现 `get_intraday_batch` /
  `get_intraday_latest` 优化批量与增量。未实现批量方法时使用 `get_minute` 当日窗口，
  没有增量方法时仅修复轮且间隔至少 60 秒。YAML 复用 `minute` 接口。
  契约见[插件](reference/integrations.md#plugin)与[HTTP数据源](reference/integrations.md#http)。

接入步骤:

1. 在 **设置 → 数据源 → 分钟K** 选择可用来源，实时监控页按服务状态显示落盘是否可用；已有 TickFlow 部署的 Key 通过环境变量维护
2. 落盘服务默认开启；仅在交易日 09:15–11:30 / 13:00–15:15 运行，午休和 15:15 后自动暂停；已保存的关闭值优先
3. 冷启动(如 10 点才开服务)自动触发**全天修复轮**,一次批量补齐当日缺口;稳态走**增量轮**(偏好可配 1–120 秒)。rustdx 逐证券取 bars，有效周期至少 60 秒；设置页显示生效周期，原偏好保留
4. 与盘后分钟同步写同一分区，完整读/合并/替换加锁，`unique(symbol, datetime)` 幂等合并

说明:标的池为 A 股股票(CN_Equity_A);覆盖滞后超阈值或连续空轮会自动再跑修复轮自愈。
旧 `full_minute_data_provider` 值保留在原配置中但不参与路由；旧 API 写入该字段被忽略，
旧 getter 跟随 `minute_data_provider`。旧 YAML `full_minute` 可读取，但必须配置
真实 `minute` 才能接入；不自动把仅支持当日的旧源声明为历史分钟源。
升级无需清理分钟分区；回滚代码仍能读取原配置和数据。

<a id="config-公司行为与秒级归档"></a>
##### 公司行为与秒级归档

`corporate_actions_provider` 独立选源，默认 rustdx；TickFlow 当前没有该适配。
它提供分红/送转/配股事件，不替代 `adj_factor`。日期证据不足时新事件账户会暂停
相关撮合与净值定版，不能仅凭矩阵 `usable` 放行；见[公司行为](reference/trading-ai.md#corporate)。

秒级归档是业务偏好，默认开启，保存现有 rustdx 实时采集结果：
`second_quote_archive_enabled`、`second_quote_archive_retention_days`（默认 0，
不自动删除）。通过受认证设置 API 修改；目前没有独立配置页面，
示例和资源/质量边界见[秒级研究](reference/market-data.md#second-7-操作入口)。

---

<a id="config-ai可选"></a>
#### AI(可选)

用于自然语言生成策略。**所有配置留空即跳过**,不影响核心功能。支持任意 OpenAI 兼容接口。

```ini
AI_PROVIDER=openai_compat              # openai_compat | openai | codex_cli
AI_PROTOCOL=chat_completions           # chat_completions | responses
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=                            # 留空 = 关闭 AI
AI_MODEL=deepseek-chat
AI_MAX_OUTPUT_TOKENS=16384            # 请求输出封顶
AI_CONTEXT_WINDOW=128000             # 输入上下文估算上限
AI_ROUND_CHECKPOINT=100               # 助手连续工具轮次检查点；0 不检查
```

| 配置项 | 说明 |
| :--- | :--- |
| `AI_PROVIDER` | `openai_compat`（兼容服务）、`openai`（支持推理强度配置）、`codex_cli`（本机 CLI） |
| `AI_PROTOCOL` | 默认 `chat_completions`；服务商支持时显式选择 `responses`。Codex CLI 不使用此项 |
| `AI_BASE_URL` | 接口地址,如 DeepSeek `https://api.deepseek.com/v1` |
| `AI_API_KEY` | 留空则关闭 AI 功能 |
| `AI_MODEL` | 模型名,如 `deepseek-chat` |
| `AI_MAX_OUTPUT_TOKENS` | 输出 token 上限，16384；具体任务请求值仍可能更小 |
| `AI_CONTEXT_WINDOW` | 输入上下文估算上限，128000；超限返回明确错误 |
| `AI_ROUND_CHECKPOINT` | 助手持续工具调用的继续/停止检查点，100；不是每日费用限额 |

旧 `.env` 中的 `AI_DAILY_TOKEN_BUDGET` 不被读取；当前累计准入参数及未知用量处理见
[AI用量预算](#ai-usage-budget)。供应商费用/余额限制仍须在所用模型服务配置。

页面预设包括自定义、OpenAI、DeepSeek、GLM 和 Codex CLI。
通义千问与 Kimi 不再单列预设；已有其他 OpenAI 兼容配置会显示为“自定义”，
其地址、模型和密钥仍按原配置使用。

「接口协议」用于报告、策略生成及助手。页面保存的协议优先于环境变量；
旧客户端省略协议字段时保留已保存值，清空 AI 配置后恢复 Chat Completions。
「测试」按钮先保存表单并更新共享设置缓存，再测试连接；保存、测试和清空期间
禁用重复操作。保存失败会显示具体原因，当前有效配置保持原值。
API 地址必须是完整的 HTTP(S) 地址，不能重复拼接 URL 或包含查询参数、片段；
完整 `/chat/completions`、`/responses` 地址会去掉末尾接口段，已有 `/v3`、`/v4`
等版本段保持不变，无版本段时补 `/v1`。选择协议本身不触发请求。
连接测试为推理模型预留最多 512 个输出 token，仍受当前输出上限约束；
响应因长度截断时会明确提示提高输出上限，不将截断响应视为连接成功。
Responses 的工具、完整性及取消约定见 [AI 协议](reference/trading-ai.md#ai)。

接入示例见 [strategy.md](reference/research.md#strategy) 的「AI 生成策略」章节。

---

<a id="config-内置扩展概念与行业"></a>
#### 内置扩展概念与行业

```ini
EXT_CONCEPT_DATA_URL=
EXT_INDUSTRY_DATA_URL=
```

扩展概念和扩展行业默认可直接拉取同花顺分类数据，默认接口分别为：

- `https://shy313.com/api/plugins/market_flow/exports/ths-concepts`
- `https://shy313.com/api/plugins/market_flow/exports/ths-industries`

环境变量未设置或为空时使用上述默认值，非空 URL 可独立覆盖对应上游。

- 新安装默认自动拉取，由后台调度器执行启动首轮，
  后续在交易日 `09:15-11:30`、`13:00-15:15` 每分钟拉取一次。
- 可在「数据 → 扩展概念/行业 → 拉取设置」关闭自动拉取；
  在概念/行业页点击获取仍可执行一次同步。
- 旧版原样生成、没有数据的空预设会补全默认地址并保持手动模式；已保存的自定义地址、
  主动关闭状态、定制配方及已有数据保持不变。
- 手动拉取使用表内保存的 URL、请求头、鉴权和字段映射，与定时拉取一致。

完整多租户模式下，管理员的配置列表从平台服务读取，显示保存的拉取地址和调度；
普通账户仍读取公共数据副本，不返回拉取配方或上游请求头。

预期概念响应行为 `{symbol, name, concepts: [...]}`，行业响应行为
`{symbol, name, industries: [...]}`；接口可直接返回数组，也可使用常见
`data`、`list`、`rows` 等信封字段。

---

<a id="config-服务"></a>
#### 服务

```ini
HOST=0.0.0.0          # 开发服务监听地址 / Docker 主机绑定地址
PORT=3018             # 开发后端端口 / Docker 主机映射端口
LOG_LEVEL=INFO        # DEBUG | INFO | WARNING | ERROR
```

- `HOST`:`0.0.0.0` 监听所有网卡(容器/公网部署需要);仅本机用可设 `127.0.0.1`
- `PORT`:默认 `3018`;开发模式兼容显式的 `BACKEND_PORT` 覆盖,改端口后 SSH 转发命令也要同步改
- `LOG_LEVEL`:排查问题时改 `DEBUG`

---

<a id="config-数据"></a>
#### 数据

```ini
DATA_DIR=./data       # Parquet / DuckDB 数据存储目录
```

整个 `data/` 目录都不纳入 git —— 行情 K线、财务、自选、回测、监控记录,乃至概念/行业扩展数据,全部是程序运行时生成/拉取的用户数据。

独立模式迁移前先停写，再复制整个数据目录、`.env` 与自定义插件；完整多租户须
同时备份/恢复双库、平台配置、公共版本及私有产物，不能只复制 API 的 `DATA_DIR`。
见[部署更新](#10-升级与回滚)与[备份恢复](#8-备份)。

<a id="config-数据页初始化默认值"></a>
##### 数据页初始化默认值

新项目按以下参数初始化「数据」菜单；后端偏好读取、页面初始显示及设置中的
“默认”预设保持一致。已保存的值优先，包括主动关闭状态；缺失字段才使用默认值。
完整多租户模式下，数据页参数由平台服务统一维护。

多租户 API 与后台进程启动前，应确认平台共享快照可读取，不能仅凭存活探针或旧发布文件判断
就绪。首次同步期间，共享快照的板块展示只读取现有 enriched 缓存；冷缓存返回空结果，
不在请求中触发全量重算或等待数据同步的计算名额。

数据总览首排在宽屏采用数据同步、存储 3:1 左对齐布局，窄屏纵向排列。
数据同步卡片右上角仅显示自动同步开关按钮；实时行情卡片在数据画像15分K后，
保留独立开关与轮询间隔。数据同步按生命周期分类，并逐项显示自动采集周期；
基础与完整性、实时行情与快照、K线采集与加工、财务与能力保障及扩展数据默认收起，
点击各类标题展开或收起；刷新状态保留各组展开选择。
日K周期同步紧接15分K周期同步，与盘后管道共用日K开关和时间，默认交易日16:00，
已有自定义时间保持。存储依次展示个股维表、1/5/10/15分K、日K、周K、除权因子、
Enriched、财务数据和扩展数据，空数据项保留，数量和体积读取现有存储统计。
本次布局为源码候选，运行/部署状态见[版本基线](version-baseline.md)。

| 配置 | 初始化默认值 |
| --- | --- |
| 界面主题 | 白天模式（亮色）；已有浏览器保存的主题选择优先 |
| 日K、复权、分钟、五档、实时、财务数据源 | 全部 `rustdx` |
| 公司行为数据源 | `rustdx`，独立于复权；日期缺证据时暂停相关账务 |
| 自动数据同步 | 开启；首次行情任务、盘后管道、竞价、五档定版及启动修复按现有门控执行 |
| 实时行情 / 分时图实时刷新 | 开启，行情轮询偏好 3 秒，分时图间隔 5 秒 |
| 实时资产范围 | A 股开启；核心指数沿用固定补拉契约 |
| 证券目录同步 | 固定沪深交易所官网；系统启动及每天北京时间 09:00 自动执行，含休市日，不受行情总开关控制 |
| 盘后管道 | 自动，交易日北京时间 16:00；同步包含 A 股和指数，已保存的自定义时间优先 |
| 指标计算 / 指数日K批量大小 | 1000 / 100 |
| 管道包含分钟K | 开启，最近 5 天，分段大小 20 个交易日；跟随手动/定时管道执行 |
| 盘中分钟刷新 | 开启，间隔偏好 60 秒；rustdx 生效周期至少 60 秒 |
| 管道包含环境计算 | 开启；跟随手动/定时管道执行 |
| 环境全量回填 | 每批 120 个交易日，预热 40 个日历日 |
| 扩展概念、扩展行业 | 快照模式，内置默认 URL，GET，无需鉴权；自动拉取开启，1 分钟间隔、30 秒超时 |
| 财务周期同步 | 默认开启；有财务能力时启动后 60 秒首轮，随后约每周同步 metrics；首次初始化另同步全部五张历史表 |
| 秒级快照归档 | 开启，跟随 rustdx 实时行情；保留期 0 表示不自动删除 |
| 数据画像卡片 | 默认顺序为个股维表、分钟K、日K、除权因子、Enriched、环境、指数、财务数据；按数据能力显示，已有用户排序优先 |

首次初始化自动创建后台全量管道，失败保持待同步状态，可修复来源后点击「立即同步」重试。
「同步调度 → 自动同步」可暂停或恢复定时数据任务与启动补跑；财务周期执行也检查该总开关。
全量管道时间可在「数据 → 同步调度」修改，支持 15:35–23:59；保存后立即更新调度，
重启后保留。早于 15:35 的时间明确拒绝，避免在盘后固定价交易和日线定稿前同步，
不再静默改写输入。关闭自动同步时修改时间保持任务暂停，休市日或交易日证据未知时跳过。
已关闭时修改时间不会重新开启。个股维表始终自动同步，时间固定 09:00，无需数据源密钥。
盘后管道、首次行情初始化与日K修正/补全只读取已有维表，不重复更新名单。
首次后台行情初始化等待独立启动目录任务结束；没有完整快照时失败并保留待同步状态，
需先完成启动或09:00目录任务后重试行情同步。
管理员可从「数据总览 → 个股维表 → 管理」搜索、组合交易所和板块筛选、分页及新增、编辑、确认删除。
全部交易所时板块选项为沪主板、深主板、创业板、科创板；上海仅显示沪主板和科创板，
深圳仅显示深主板和创业板。切换交易所保留兼容的板块，清除不相容板块；改变筛选回到第一页。
底部可输入1至总页数的整数并点击「跳转」或按Enter，空值、越界、小数和空名单不发起跳转。
股本单位为股；编辑不能改代码，需删除后新增。修改即时刷新查询，删除保留历史行情；
下次官方同步会恢复仍在册的证券并重核名称和上市日期，官方缺失的股本沿用原值与来源。
网络不可达、文件不完整、字段异常或四板缺失时保留旧快照，并在任务日志记录失败；
应核对 `instruments_as_of` 与任务结果，不能仅凭旧表非空判断当日已更新。
升级时原 `instruments_schedule` 仍可保存在旧偏好中，但读取固定为09:00；
旧客户端提交其他时间会返回422。此变更不迁移历史行情或数据库schema。
目录任务停机时等待正在执行的有界下载/写入退出，部署前应正常停止服务并保留任务日志。
实时行情、盘中分钟刷新及扩展数据自动拉取分别由各自开关控制，已有显式选择保留。
默认开启不绕过数据源能力、交易日/时段、权限、任务互斥和发布门控。
默认菜单顺序为：行情、环境、自选、策略、因子、回测、复盘、异动、持仓提醒、
监控中心、连板梯队、个股分析、概念分析、行业分析、财务分析、信号指标、
模拟交易、数据总览；内置菜单全部显示，自选分组不展开到侧栏。

扩展数据的交易时段、自动拉取及自定义 URL 规则见上文
[内置扩展概念与行业](#config-内置扩展概念与行业)。

<a id="config-实时监控初始化默认值"></a>
##### 实时监控初始化默认值

「设置 → 实时监控」初始化采用当前页面参数；缺失字段使用下表默认值，
已有间隔、关闭状态和通知渠道优先，重启不重置。读取默认值不会改写偏好文件。

| 配置 | 初始化默认值 |
| --- | --- |
| 实时行情 | 开启，轮询间隔 3 秒；生效间隔遵守数据源下限 |
| 页面实时刷新 | 行情、自选页、连板梯队、概念/行业分析的 SSE 刷新全部开启 |
| 自选/策略分时图刷新 | 开启，间隔 5 秒 |
| 全市场分钟落盘 | 开启，目标间隔 60 秒；实际周期遵守分钟数据源能力和请求成本 |
| 真假板修正 | 关闭；五档轮询 10 秒，收盘定版时间 15:02 |
| 新建规则默认通知 | 系统 SMTP 与注册收件人可用时选择邮件；已保存空渠道或旧关闭值优先 |
| 飞书、企业微信、第三方 Webhook / 智能机器人 | 未配置地址或凭据，默认渠道不选择；智能机器人关闭 |

邮件继续复用受控的注册 SMTP 配置，收件人默认为当前账户的注册邮箱；
授权码保存在运行配置中，不作为源码默认值。详见[邮箱配置](#email-accounts)。

<a id="config-异动监控初始化默认值"></a>
<a id="config-异动数据初始化默认值"></a>
##### 异动数据初始化默认值

竞价异动、盘中异动、偏移异动默认均启用，进入对应页面后自动加载。
竞价历史从首次成功采集起积累；尚无交易日快照时显示空状态。
偏移异动在交易时段每 60 秒计算一次，非交易时段每 5 分钟刷新。
偏移开关按当前账户保存在浏览器中，个股详情使用同一开关；主动关闭后停止计算，
仍可查看上次结果，重新打开页面也保留关闭状态。
后台告警由「监控中心」的规则独立控制。

---

<a id="config-后端依赖-extras可选"></a>
#### 后端依赖 Extras(可选)

```ini
BACKEND_EXTRAS=             # 留空默认;legacy-cpu 兼容老 CPU
```

老 CPU 无 AVX2/FMA 支持时设为 `legacy-cpu`,会给 Polars 切到 `rtcompat` 运行时;
需要可选 vectorbt 信号回测栈时设为 `legacy-cpu backtest`；矩阵回测不以 vectorbt 为前置。
Docker 构建和开发脚本都会读取此值并同步依赖。见[老 CPU 兼容](#install-老-cpu-兼容)。
PostgreSQL 客户端由单机和 full-product 共用的模拟交易/告警模块直接引用，已属于默认
运行依赖；旧 `--extra cell` 命令仍兼容，但不再决定主应用能否导入。

---

<a id="config-配置优先级"></a>
#### 配置优先级

| 类型 | 实际优先级与生效方式 |
| --- | --- |
| 启动 `Settings` | 进程环境变量 → 所选 dotenv 文件 → 代码默认；`TICKFLOW_ENV_FILE` 可改变文件路径 |
| 数据源 Key / AI 配置 | 通过 `secrets_store` 读取的有效页面保存值 → `Settings` 后备值；不能用环境覆盖已经保存的有效值 |
| 业务偏好 | `preferences.json` 已保存值 → getter 默认值；完整多租户的平台/租户归属由服务端接口决定 |
| 插件 Key | `secrets.json` 中该插件有效值 → `api_key_env` 指定的环境变量 |
| 注册/通知 SMTP | 显式 `AUTH_SMTP_HOST` → 已保存的系统 SMTP → 完整多租户 `TSP_CELL_SMTP_*`；均未配置时不可发送，不提供内置授权码 |

配置文件含密钥，不能进入 Git。完整多租户通过 `PLATFORM_CONFIG_DIR` 读取公共系统
配置，Compose 指定 `TICKFLOW_ENV_FILE=/dev/null`，依赖显式私密环境文件注入。
页面保存不会改变 Compose 内存上限、Worker 数或已经初始化的线程池。
实际入口见[配置类](../backend/app/config.py)、[密钥读取](../backend/app/secrets_store.py)、
[偏好 getter](../backend/app/services/preferences.py)。

<a id="email-accounts"></a>
### 2.5 邮箱账户与通知SMTP

所有环境的全新初始化默认预置管理员 `admin/Admin123`。
非秘密初始化值在[初始化默认配置](../backend/app/bootstrap_defaults.py)集中定义。
SMTP 服务器、发件地址和授权码由受控 `.env`、系统配置或 Cell 环境提供；
源码与镜像不携带实际邮箱凭据。未配置可用传输时，注册/找回无法完成邮件验证，
通知默认邮件渠道不启用。重启不会重置已修改密码或替换已有 SMTP。
普通模式已有账户或旧版访问密码时保留原身份，显式邮箱/密码预置仍按下方兼容规则处理。

macOS/Linux 默认启用多租户（`MULTITENANCY_ENABLED=true`、`SERVICE_ROLE=api`），
冻结桌面同样使用账户认证；Windows 启用单机管理员模式。
注册和密码找回在首次启动自动启用，默认注册口令 `天王盖地虎`；首次环境变量可
自定义口令，已保存的口令不会被重置。完整多租户 API 在已有 SQL schema 上使用
运行角色幂等创建缺失管理员和口令；建库迁移及内部服务配置仍按[完整部署](../deploy/full/README.md)执行。
管理员默认具备租户业务能力；没有默认租户的有效管理员在启动时自动获得个人空间，
登录后可直接使用自选、策略、监控和模拟交易。已有租户选择与私有数据保留；
可在账户页切换目标租户。初始化中断在后续启动或登录时复用同一记录重试，不重置密码，
不启用停用账户；容量不足或空间预留后的初始化失败时仍可登录管理，空间就绪后再使用私有业务。
显式 `MULTITENANCY_ENABLED=false` 或 `SERVICE_ROLE=standalone` 保留旧独立模式，
不会把多邮箱共享空间宣称为具备租户隔离。仅复制邮箱模板不能代替多租户基础服务。
Windows 原生见下一节，不能用邮箱环境变量覆盖本地管理员。

<a id="email-方式一-浏览器注册"></a>
#### 方式一: 浏览器注册

普通 Web 模式首次打开可用预置管理员登录，也可进入注册页:

1. 输入邮箱和注册口令, 点击“发送验证码”。
2. 输入邮件中的 6 位验证码和至少 8 位的密码。
3. 验证码校验成功后创建账户并自动登录。

后端只有在注册口令正确时才会发送邮件。验证码 10 分钟有效, 60 秒内不能重复发送, 连续输错 5 次后失效, 且成功注册后不能再次使用。口令校验和发送请求合计按来源 IP 限制为每小时 10 次。

注册口令有首次初始化默认值；邮件传输必须配置。以下参数用于自定义口令并配置邮箱：

```bash
AUTH_REGISTRATION_SECRET='请设置注册口令'
AUTH_SMTP_HOST='smtp.example.com'
AUTH_SMTP_PORT=465
AUTH_SMTP_SECURITY='ssl'       # ssl | starttls | none
AUTH_SMTP_USERNAME='no-reply@example.com'
AUTH_SMTP_PASSWORD='SMTP 密码或授权码'
AUTH_SMTP_FROM_ADDRESS='no-reply@example.com'
```

服务将注册口令转换为带随机盐的 PBKDF2 哈希，多租户写入 SQL
`control.registration_config`，旧独立模式写入 `data/user_data/auth.json`，不保存明文。
初始化成功后可删除 `AUTH_REGISTRATION_SECRET` 并重启，原口令保留。

`AUTH_SMTP_HOST` 非空时整组环境变量优先。留空时先复用已保存的系统 SMTP，
完整多租户再读取 `TSP_CELL_SMTP_*`，均未配置时返回未配置状态。
显式配置服务器却未提供授权码时不继承其他来源的密码。
通知与注册复用同一传输配置，通知收件人仍独立设置。

<a id="email-方式二-环境变量预置"></a>
#### 方式二: 环境变量预置

自动化部署可在 `.env` 或运行环境中同时设置:

```bash
AUTH_EMAIL='admin@example.com'
AUTH_PASSWORD='至少八位的密码'
```

服务首次启动时会:

1. 规范化邮箱并校验密码长度。
2. 使用 PBKDF2 哈希密码后写入 `data/user_data/auth.json`。
3. 将账户文件权限设为 `600`。
4. 已有账户时跳过初始化, 不覆盖页面中修改过的密码。

预置账户属于部署初始化, 不经过公开注册接口, 因此不要求注册口令、邮箱验证码或 SMTP。后续用户从浏览器注册时仍必须完成两项校验。

需要以用户名登录的管理员时，使用 `AUTH_USERNAME` 替代邮箱预置：

```ini
AUTH_USERNAME=admin
AUTH_PASSWORD='Admin123'
```

此配置只向现有 `auth.json` 添加管理员账户，不切换运行模式或暂停注册。
已有邮箱账户、会话和注册口令保留；已有同名账户不会重置密码。
`AUTH_USERNAME` 非空时优先使用用户名预置，Windows 单机模式仍按原有规则初始化。
旧密码账户尚未绑定邮箱时，须先完成原账户迁移，不能通过管理员预置绕过。

`.env` 应保持 `600` 权限且不得提交到 Git。密码含 `$` 等字符时建议使用单引号。

<a id="email-旧版访问密码迁移"></a>
#### 旧版访问密码迁移

升级前已设置访问密码的部署无需重置。新登录页会显示“升级现有账户”:

1. 输入要绑定的邮箱。
2. 输入原访问密码。
3. 绑定完成后使用邮箱和原密码登录。

迁移只改变认证文件结构, 不修改行情、策略、自选或系统设置。

只设置 `AUTH_PASSWORD` 而未设置 `AUTH_EMAIL` 时, 系统也会进入这一兼容模式。

<a id="email-修改和重置"></a>
#### 修改和重置

- 修改密码: `设置 → 账户 → 修改密码`。成功后该账户的全部会话会退出。
- 退出登录: 使用侧栏底部的退出按钮或账户设置页。
- 忘记密码: 在登录页点击“忘记密码”，输入邮箱、注册口令、专用邮箱验证码和新
  密码。找回验证码 10 分钟有效、最多尝试 5 次，与注册验证码相互隔离。
- 重置成功后，该账户的全部会话和 API Token 都会撤销，需要重新登录并重建外部
  调用凭据。
- 未注册邮箱与已注册邮箱使用统一发送响应，运维日志和页面不得暴露账户是否存在。

不得通过删除 `auth.json` 代替正常找回；该操作会影响全部独立模式账户。只有认证
存储损坏且正常找回不可用时，才可在停服、完整备份和明确影响范围后执行离线恢复。

<a id="email-安全建议"></a>
#### 安全建议

- 公网部署必须使用 HTTPS。
- 密码建议使用 12 位以上随机字符串。
- `auth.json`、`auth_verifications.json` 和 `.env` 不应进入 Git 或公开备份。
- 注册口令按密码管理; 初始化完成后从 `.env` 删除明文。
- SMTP 密码优先使用邮件服务商提供的独立授权码, 不要使用邮箱主密码。
- API Token 是外部程序调用通道, 与浏览器邮箱账户相互独立。

“设置 → 实时监控 → 邮件”始终复用上述注册 SMTP，仅允许修改收件人。主机、端口、加密方式、登录用户名、发件人和密码/授权码均由系统维护，页面与保存接口不允许修改；授权码仅在后端使用，保存收件人不会复制或改写凭据。收件人默认显示当前账户的注册邮箱，测试邮件使用该默认值；共享后台通知默认发给已注册账户，保存自定义收件人后以保存值为准。发信服务和注册账户均可用时，新建监控规则默认选择邮件；已保存的默认渠道（包括关闭）保持不变。未配置 `AUTH_SMTP_HOST` 时保留历史系统 SMTP 作为兜底，桌面版需由系统配置发信服务并填写收件人。

<a id="windows"></a>
### 2.6 Windows单机管理员与16GB预算

<a id="windows-账户与个人空间"></a>
#### 账户与个人空间

- 首次启动创建 `DATA_DIR/user_data/local_admin.json`，只存储加盐密码哈希。
  该文件已存在时不会覆盖账号、重置密码或重新生成个人空间身份。
- 登录后可在「设置 → 账户」查看管理员角色、个人空间，修改密码或退出登录。
  改密会撤销该账号全部会话；重启后仍使用新密码。
- 关闭注册、邮箱找回、旧账户迁移与租户切换；后端同样拒绝相关写接口。
  `AUTH_EMAIL`、`AUTH_PASSWORD`、`AUTH_REGISTRATION_SECRET` 不覆盖本地管理员。
- 单机已有自选、策略、回测、监控和设置继续使用原 `DATA_DIR` 下的本地存储。
  个人空间是管理员使用完整业务功能的本地空间，不创建 PostgreSQL 租户。
- 旧 `user_data/auth.json` 保留不动。账号文件损坏时启动失败，须恢复备份，
  不会自动回退初始密码。备份及回滚应包含整个 `data/`。

默认密码用于首次进入；开始保存个人数据前，在账户页面修改密码。
源码服务脚本和桌面版均默认只监听本机。

<a id="windows-启动与升级"></a>
#### 启动与升级

使用原生 Python 3.11+、Node/pnpm 和 rustdx 构建所需 Rust 工具。
按仓库安装流程完成 `uv sync --extra dev --extra backtest --frozen` 与
`pnpm install --frozen-lockfile`、`pnpm build`。

源码服务示例（在仓库根目录运行，路径按实际安装调整）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\tsp-service.ps1 `
  -RepoRoot E:\Projects\TSP -DataDir E:\TSPData -ResourceProfile native16gb
```

打开 `http://127.0.0.1:3030`。省略 `DataDir` 时使用 `<RepoRoot>/data`。
脚本从仓库 `.env` 读取业务配置，强制 `SERVICE_ROLE=standalone`、
`LOCAL_ADMIN_MODE=true`、`MULTITENANCY_ENABLED=false`，运行一个 uvicorn worker
并托管完整 `frontend/dist`。不需要 PostgreSQL、多租户 Broker 或四类常驻 worker。
独立启动 API、行情同步、监控、通知和定时任务使用既有单机调用链；
研究任务沿用单机回测、优化、walk-forward、挖掘进程。

Windows 桌面安装包双击启动，使用安装目录旁的 `data/`，窗口打开后先登录。
桌面入口会在加载数值库前应用同一组 4096 MiB、线程和任务并发默认值；
启动环境中显式设置的同名变量优先，不会被覆盖。

桌面窗口依赖Microsoft Edge WebView2 Runtime。当前安装器会先检查并安装该运行时，
避免退回不支持React/Vite的MSHTML而白屏；实现见[安装器](../packaging/tsp.iss)，
回归入口见[桌面打包测试](../backend/tests/test_desktop_packaging.py)。
旧0.3.x邮箱注册/SMTP首次初始化流程不适用于当前Windows管理员模式，
旧安装包记录也不构成0.4.17真机验收。

从旧 Windows 多租户部署切换时：

1. 备份数据库、公共行情、单机数据目录、服务参数及 edge 配置。
2. 先执行旧 worker 的 `tsp-full.ps1 stop`，再停止并禁用旧
   `TSPWorkerWatchdog`（含计划任务）；停止 `TSPApi`、`TSPPlatform` 和旧 Vite 服务。
3. 解除新单机服务对平台、PostgreSQL 的 `DependOnService` 依赖，只托管一个
   `tsp-service.ps1 -Role standalone` 进程。`-Role api` 是兼容别名；
   `-Role platform`、`tsp-full.ps1 start/restart` 均拒绝运行。
   模式检查先于安装路径解析，即使旧安装盘符不存在也会明确拒绝旧平台模式。
   旧 watchdog 入口只提示停用后退出，`tsp-full.ps1 stop/status` 仍可清理旧进程。
4. 为单机选择可写数据目录。禁止将 `public_market/current` 或 release 目录直接
   作为写目录；脚本拒绝 `PublicMarket` 下的路径。保留旧数据库和公共快照，
   不自动导入、合并或删除租户私有数据。需要的旧租户资产须单独导出后导入。
5. 启动新服务，检查登录、个人空间、数据源能力与 `/health/ready`，
   再验证真实业务任务。使用 edge 时确认其 upstream 指向新单机服务。

配置层的 `LOCAL_ADMIN_MODE` 在原生 Windows 强制开启。若旧 `.env` 保留
`SERVICE_ROLE=platform/api/gateway`，直接运行 Python 会明确报错；先完成停写切换，
再改成 `standalone`。仅有旧 `MULTITENANCY_ENABLED=true` 时会被单机模式强制关闭。
WSL2 / Docker 内的 Linux 不受 Windows 平台默认值影响。

回滚时先停止单机进程，再恢复旧服务配置、对应数据库、目录与路由。不得让两个
运行模式同时写同一数据目录。新管理员身份文件可随备份保留，旧版本不会读取它。

<a id="windows-16-gb-资源控制"></a>
#### 16 GB 资源控制

原生服务默认 `native16gb`，单进程预算提示为 **4096 MiB**：

| 项目 | 设置 |
| --- | --- |
| Polars / Numba | 最多 2 线程 |
| BLAS / OpenMP / MKL / NumExpr | 1 线程 |
| 策略批量执行 | 1 worker |
| Polars collect | 前台总计 2，后台最多 1 |
| 小内存 DuckDB | 最多 2 线程，单连接缓冲上限为有效预算的 1/8，最多 512 MiB |
| 回测面板缓存 | 字节 LRU / TTL，单缓存最多 256 MiB，超大面板不驻留 |
| 历史指标 | 按有效预算分批，不截断日期、标的或字段 |

有效预算取显式值、物理内存与适用 cgroup 限制的最小值。预算不等于 Windows
Job Object 硬限制，也不是整棵进程树 RSS 保证；多个连接、任务进程和临时矩阵仍可
增加内存。16 GB 机器应避免同时提交多个重计算任务，并保留系统分页文件和 SSD 空间。
`-ResourceProfile existing` 保留已有资源环境配置。

既有 Provider、金融口径、Parquet 契约、磁盘矩阵缓存、generation 和 SSE 流程不变。
此次为 L1 启动配置及 L3 核心账号/缓存调整，未新增平行业务引擎。
单机模式不使用多租户 Compute，原生租户沙箱的实现缺口留待恢复多租户时处理。

此前合成缓存基准（macOS arm64，Python 3.11.17，Polars 1.44.1）：
固定 1536 MiB 预算、2 线程、三个 `1,000,000 × 16` 面板，各运行三次。
缓存持有数据中位数从 256,000,000 降至 128,000,000 字节；
峰值 RSS 从 633,815,040 降至 615,497,728 字节；
耗时从 0.2353 增至 0.2508 秒，六次校验和一致。
这只反映该缓存场景，不代表 Windows 整机内存下降 50% 或吞吐提升。

<a id="windows-验证与边界"></a>
#### 验证与边界

后端定向入口：

```powershell
cd backend
uv run --frozen --no-sync pytest tests/test_local_admin.py tests/test_auth_accounts.py tests/test_auth_env_bootstrap.py tests/test_auth_runtime_mode.py tests/test_desktop_packaging.py -q
$env:PYTHONPATH = (Get-Location).Path
& ..\deploy\windows\test-resources.ps1 -Python (Resolve-Path .venv\Scripts\python.exe).Path
```

前端运行 `pnpm test src/pages/LocalAdmin.test.tsx src/pages/Auth.test.tsx
src/components/TenantSelector.test.tsx src/components/AccountScopeBoundary.test.tsx
src/lib/accountSession.test.ts src/lib/auth-api.test.ts` 和 `pnpm build`。
Windows Server 2022 CI 覆盖原生资源、脚本及账号回归；桌面 release smoke 检查
默认登录、匿名访问拒绝、个人空间、退出后会话失效和完整 API 路由。
macOS / Linux 打包 smoke 继续验证免登录。

2026-10-22 整合 PR 首轮 Windows Server 2022 / PowerShell 5.1 CI 已执行，
资源子进程成功，但测试脚本将 `ConvertFrom-Json` 的数组输出再次包入 `@(...)`，
导致参数数量断言失败。调整为直接接收 JSON 数组，并在 Python 子进程内增加逐项
参数断言，覆盖空字符串、引号、中文及末尾反斜杠；以重跑终态验证修正。
第二轮已通过全部七种角色资源与参数检查；随后暴露旧 `platform` 拒绝路径在
模式检查之前解析 E 盘的问题。将路径初始化放在模式检查之后，并使用随机不存在
的 PowerShell drive 增强原生拒绝回归；完整 Windows 质量门仍待重跑终态。

本轮没有接入 Win10 22H2 真机，未执行安装包构建。
不能据本地跨平台测试或 Server 2022 CI 宣称 Windows 全功能实机验收完成。真机还需覆盖真实数据
同步、选股、回测、优化、walk-forward、挖掘、监控、通知、调度、取消和重启恢复，
并记录系统提交量、进程树峰值内存、换页及耗时。

<a id="windows-legacy"></a>
#### 旧Windows多租户部署的退出边界

原v0.4.9平台/API/四worker/NSSM方案已停止作为当前安装路径。
先用旧 `tsp-full.ps1 stop` 清理worker，再停止并禁用watchdog、API、平台及旧Vite服务；
旧看门狗的子进程可能使直接停服务卡在 `STOP_PENDING`。保留双库、公共版本、
私有产物、服务参数与Edge配置的匹配备份，不能直接把只读 `current` 当作单机写目录。

当前原生模式只托管standalone与静态前端。若继续使用HTTPS代理，upstream指向新单机端口，
SSE关闭缓冲；hash资产可长期缓存，index保留 `no-store`；证书数据与私钥受控备份。
仅信任受控代理转发头，不把配置文件暴露在静态目录。

Windows文件替换、信号处理、venv启动器PID、进程树回收和目录权限仍须做平台验证。
原多租户Compute依赖Unix socket及容器沙箱的缺口不能靠改配置消除；
恢复多租户必须补齐受控IPC、隔离与资源回收后另行验收，不用无沙箱通道替代。
Win10真机与安装包未在本次文档合并中验证。

<a id="3-启动停止与状态"></a>
## 3. 启动、停止与状态

<a id="31-docker-compose"></a>
### 3.1 Docker Compose

启动或重建：

```bash
docker compose up --build -d
docker compose ps
curl -fsS http://127.0.0.1:3018/health
```

查看日志：

```bash
docker compose logs --tail=200
docker compose logs -f
```

优雅停止：

```bash
docker compose stop
```

删除容器但保留绑定挂载数据：

```bash
docker compose down
```

不要在未确认备份时添加删除卷参数，也不要删除宿主机 `data/`。

<a id="32-单个-docker-容器"></a>
### 3.2 单个 Docker 容器

```bash
docker ps --filter name=TSP
docker logs --tail=200 TSP
docker restart TSP
curl -fsS http://127.0.0.1:3018/health
```

实际容器名应以部署命令为准。使用固定版本或提交 SHA 镜像，不要把 `latest` 当作
可复现生产版本。

<a id="33-dev-模式"></a>
### 3.3 Dev 模式

```bash
./dev.sh
```

前端默认在 `3011`，后端默认在 `3018`。Dev 模式会启用开发工具和热更新，只用于
开发与联调。

<a id="4-健康检查"></a>
## 4. 健康检查

基础探活：

```bash
curl -fsS http://127.0.0.1:3018/health
curl -fsS http://127.0.0.1:3018/health/live
```

正常响应包含：

```json
{
  "status": "ok",
  "version": "<实际部署应用版本>",
  "mode": "none"
}
```

深度就绪检查：

```bash
curl -fsS http://127.0.0.1:3018/health/ready
```

该接口检查仓库初始化、`DATA_DIR` 可写性、磁盘余量、调度器、Provider 路由审计、
数据最新分区、最近盘后任务和当前 data release。关键检查失败返回 HTTP 503；
非阻断漂移返回 HTTP 200 且 `status=degraded`。它不向外部 Provider 发网络请求，
避免健康探针消耗额度。

平台进程由独立后台线程每 5 秒执行上述检查，`/health/ready`、`/api/health`
和内部行情快照共用检查结果。HTTP 探针不占用业务请求线程池；首次检查尚未完成、
检查结果距开始检查已超过 15 秒或服务正在关闭时，返回未就绪，HTTP 探针返回 503。
`/health/live` 仅确认事件循环可以响应，不执行深度检查。

完整多租户 API readiness 还检查两个数据库、公共文件、Platform、指标缓存、
Compute Broker 和四类 Worker。Worker freshness 与 Supervisor 共享按类型阈值；
Broker 检查实际运行无网络沙箱，不只检查镜像是否存在。持续观察器在运行目录
`alerts.jsonl` 写入去重的 `firing`/`resolved` 事件；部署环境应采集该文件并转发。

完整多租户的基础就绪取决于数据库、公共文件和Platform。后台研究、指标预热、
通知或调度异常保留在`checks`，基础行情依赖正常时返回HTTP 200与`status=degraded`。
`functions`分别报告`market`、`research`、`monitoring`、`paper_trading`、
`notifications`、`scheduled`的`ready`和`unavailable_dependencies`。
全功能发布验收仍须逐项核对所需功能，不能把基础就绪当成研究、通知或账务全部可用。
核心依赖错误继续返回503；不会放宽行情新鲜度、授权或账务执行检查。

各检查保留诊断信息；关键读取失败返回完整`checks`及HTTP 503，
独立后台功能异常按上述依赖范围降级。
数据新鲜度按北京时间判断, 18:00 前保留盘后处理宽限, 不要求当天完整分区。
`reference_trading_date` 来自已缓存交易日历及本地指数日期证据，
`observed_trading_lag` 是已确认落后的交易日数; `calendar_lag_days` 仅作展示。
同时检查股票日线、enriched、分钟的缺失、未来日期与落后情况。

`calendar_coverage=unknown` 表示已有日历无法覆盖判断日期, 不等于数据过期，
也不能据此断言休市。健康接口不会主动刷新日历。长假期间应结合数据同步任务
已获取的交易日历核验, 不通过修改分区日期或重写 release 来消除告警。

`checks.data_release.unavailable_generations` 非空表示 enriched 尚未完成发布。
股票通过盘后指标重算自动切换全量恢复。源日线缺失或重建失败会继续保持
不可读, 应先恢复原始日线或完整备份。不要删除 generation 标记或手工改为
`ready`: 逐分区写入中断后, 磁盘文件未必属于同一完整批次。
完整重建成功后, 由正常发布流程推进全局 release; 仅 generation 漂移且数据可读
时是未纳入基线的增量告警, 与发布中断区别处理。

采集后的版本发布与仓库文件写入共用写锁，锁内只读取分区元数据和紧凑研究输入、
提交 manifest；网络采集和 Provider 审计在锁外执行。原有 generation 和研究输入
一致性校验仍保留，未完成的 enriched 重建不能发布。

竞价快照落盘后若发布失败，采集任务返回 `publication_pending`，已有 Parquet
仍保留。显式开启自动同步后，调度器每分钟及启动时补发布当天已验证且缺少 `data_release_id` 的快照，
成功后将版本 ID 原子写回 `metadata.json`。补发布不请求行情，不受竞价采集窗口
限制；不会补造缺失日期或重新采集已落盘的竞价。旧版当天元数据缺少版本 ID 时
也通过此路径恢复，历史日期保持原样。

`mode` 会随数据源配置变化；示例值不是生产期望值。上线检查还应检查：

1. 页面可加载并能登录。
2. 设置页能力矩阵符合预期。
3. 数据页最新交易日和分区覆盖正常。
4. 最近一次盘后管道有成功终态。
5. 实时行情、监控、AI 和通知等已启用能力分别可用。
6. 数据目录可写且磁盘余量充足。

使用 rustdx 时检查 `checks.providers.rustdx_only=true` 且
`checks.providers.unavailable` 为空。混源只告警，不会被健康检查自动改写。

当前策略组协调器没有独立健康或状态 API。运行期只能结合策略结果更新时间、
`strategy group cycle failed`、`策略组周期消费时已过期`、`策略组周期部分失败`
日志及模拟交易未执行原因判断。缺少状态页面不能用“没有告警”替代成功证据；
完整状态 API 和多租户接线属于后续阶段。

<a id="5-日志与诊断证据"></a>
## 5. 日志与诊断证据

<a id="51-日志位置"></a>
### 5.1 日志位置

- Docker：`docker compose logs` 或 `docker logs`。
- 非冻结后端：`DATA_DIR/backend.log`，单文件最大约 10 MiB，保留 3 个轮转文件。
- 桌面应用：由桌面入口写入 `desktop.log`；实际目录以桌面运行时的数据目录为准。

日志可能包含代码、策略 ID、路径和供应商错误。提交 Issue 前应脱敏，不上传
`.env`、Token、Webhook、认证文件或完整用户数据。

每个 HTTP 响应都包含 `X-Request-ID`。合法的调用方 ID 会原样返回，缺失或非法值
会由服务端生成；同一请求内的应用日志包含 `request_id=<值>`。排障时优先记录该
值，不要用完整 URL、查询参数、请求体或 Token 代替关联 ID。

<a id="52-进程内-http-指标"></a>
### 5.2 进程内 HTTP 指标

登录面板会话后可读取：

```text
GET /api/observability
```

该接口返回进程启动时间、运行时长、在途请求、总请求数、5xx 数量、状态码类别、
耗时和按“方法 + 路由模板”聚合的明细。指标不包含原始 URL、查询参数、请求体和
认证头。`errors` 只统计 5xx；401、403 等拒绝可在 `status_classes.4xx` 中查看。
总计及逐路由的`duration_ms`包含`sum/max/average/p50/p95/p99`；
分位数由有界直方图估算，无样本时为`null`，不作为压测原始样本的替代。

该端点只接受面板会话，不加入公开探活白名单，也不对 API Token 开放。指标保存在
内存中，进程重启后清零；它用于单实例即时诊断，不承担长期留存、跨实例聚合或
告警。使用命令行读取时应使用权限受控的 cookie jar，不要把会话值写入命令历史。

文件型同步任务先持久化`queued`再确认接受；写入失败返回
`503 job_state_persistence_unavailable`，不得据此认为任务已启动。
终态提交失败时保留真实结果及`persistence_pending`，查询或下一次创建会重试提交，
完成提交前不接受新任务。健康详情`checks.tasks.persistence`报告待提交数量和错误类型。
已同步到磁盘的`*.json.*.pending`可在重启后恢复，未完成的`*.json.tmp`不会作为
终态采用；恢复后仍活跃的记录标为`interrupted`。排障时保留这些文件，不手工删除
记录来绕过写入失败。

<a id="53-最小故障记录"></a>
### 5.3 最小故障记录

每次故障至少记录：

- 发生和恢复时间，统一标明时区。
- 版本、提交或镜像 digest。
- 部署方式、操作系统和 CPU 架构。
- 页面、API、标的、策略或任务 ID。
- 预期行为、实际行为和复现步骤。
- 相关日志时间段及已脱敏错误。
- 数据最新交易日、受影响能力和用户范围。
- 已执行的临时处置。

<a id="6-例行巡检"></a>
## 6. 例行巡检

<a id="61-每个交易日"></a>
### 6.1 每个交易日

- 开盘前确认服务、登录和数据源能力可用。
- 09:25 后检查竞价快照是否成功发布；未配置相应能力时记录为不适用。
- 收盘后检查盘后管道任务终态和数据最新交易日。
- 检查失败或长时间运行的同步、回测、挖掘和复盘任务。
- 抽查监控触发和模拟交易结算；没有触发不等于故障。
- 新事件账户检查公司行为的登记/到账/上市日期、证据和应收；
  待核对或修订冲突时相关撮合/净值暂停属于保护行为，不应改日期强行放行。
- 启用多个策略监控时，抽查结果更新时间和策略组过期/部分失败日志；模拟交易出现
  `portfolio_duplicate`、`portfolio_conflict_exit`或`portfolio_existing_symbol`
  表示组合裁决生效，不应通过重放同一事件强制下单。

<a id="62-每周"></a>
### 6.2 每周

- 查看磁盘用量、日志增长和备份结果。
- 抽查备份文件能列出且大小合理。
- 检查 Provider、AI 和通知接口的失败趋势。
- 检查镜像或依赖更新公告，但不直接在生产使用未验收的 `latest`。
- 核对关键 Token 和管理员访问是否仍符合最小权限。

<a id="63-每次升级前后"></a>
### 6.3 每次升级前后

- 升级前执行停机备份并记录当前版本。
- 阅读版本变更、配置变化、数据兼容和已知限制。
- 升级后执行健康、数据、研究和开放接口烟测。
- 保留上一版本镜像或提交及对应备份，直到观察期结束。

项目只提供受认证的进程内 HTTP 快照，不内置集中巡检平台。需要持续告警时，应由
部署环境监控 HTTP、进程、容器、磁盘、备份结果和关键任务状态。

<a id="7-调度任务"></a>
## 7. 调度任务

当前主要默认时点均按北京时间：

| 任务 | 默认/窗口 | 运维检查 |
| --- | --- | --- |
| 个股维表同步 | 系统启动及每天北京时间 09:00，含休市日 | 官方下载和四板校验是否成功、快照日期与名称是否更新；失败保留旧表 |
| 竞价终态快照 | 09:25 后的有效采集窗口；当前要求实时源为 rustdx | 覆盖率、源时间和发布状态 |
| 实时行情 | 默认开启；交易日 09:15-11:30、13:00-15:15，默认每 3 秒一次，范围为 A 股 | 当前数据源下限、最后获取时间和快照覆盖率 |
| 盘中分钟增量 | 默认开启，交易日双时段；偏好 60 秒，rustdx 下限 60 秒 | 生效周期、分钟源落盘可用性、覆盖和最近轮次错误 |
| 盘后管道 | 默认自动，交易日 16:00，含 A 股、指数及已启用分钟阶段 | 自定义时间和下次执行时间是否一致；日 K、除权、公司行为、enriched、模拟账务及视图刷新 |
| 数据修正/完整性自愈 | 默认允许启动检查；仅窗口内问题自动修复，关闭自动同步时跳过 | 取数和计算期间实时行情应继续更新；只在写盘或删分区的短临界区出现暂停/恢复日志 |
| 盘后复盘 | 默认 15:40，默认关闭 | 是否启用、依赖数据是否已完成 |
| 扩展数据 | 内置概念/行业默认 URL、默认自动；启动首轮及交易日双时段每分钟；已保存启停值优先 | 实际源、源端日期、分页/目录完成度、完整快照哈希和备份 |
| 财务周期同步 | 默认开启；有财务能力时启动后 60 秒首轮，随后约每周同步 metrics，总开关关闭则跳过 | 最后同步时间、互斥状态及发布版本 |
| 秒级归档 | 默认开启，跟随已启用 rustdx 行情；保留期 0 不自动删除 | 队列/丢轮、写入耗时、磁盘增量与 session 记录 |
| 因子挖掘 | 可配置工作日，默认关闭 | 前置数据、运行 claim 和结果状态 |

应用停机、休市、数据源故障或任务冲突都可能导致任务未执行。恢复服务后：

1. 查看数据页和任务历史，不凭日志中的启动成功判断数据已新鲜。
2. 确认目标日期是交易日。
3. 在 UI 中对缺失的盘后数据执行“立即跑盘后管道”或相应补数入口。
4. 等待任务终态后复查数据覆盖，再恢复依赖它的监控、复盘或研究任务。
5. 竞价历史不能事后从普通实时行情可靠重建，缺失日期应保留为缺口。

内置概念/行业任务的启动首轮不受交易日和时间窗口限制；后续轮次要求交易日
探针明确确认，并使用北京时间窗口。交易日状态未知时按跳过处理，避免节假日
持续请求上游。

持久任务的统一状态为：

- `queued`：已登记，等待执行资源。
- `running`：执行体已经开始。
- `cancelling`：已收到取消请求，但执行体尚未确认退出。
- `succeeded` / `failed` / `cancelled` / `interrupted`：终态。
- `succeeded_with_budget_exhausted` / `skipped_prerequisite`：研究任务专用终态。

看到 `cancelling` 时不要立即启动另一个写任务；等待其进入终态，或在执行体无法退出
时重启单个应用进程。旧记录的 `pending` 会在读取时兼容为 `queued`。

<a id="8-备份"></a>
## 8. 备份

<a id="81-备份范围"></a>
### 8.1 备份范围

至少备份：

- 整个 `DATA_DIR`。
- `data_releases/` 全局发布历史；回测结果中的 release ID 依赖该历史做追溯。
- `instrument_status/history.parquet`；它包含从首次启用起积累的历史名称/ST/上市状态。
- `corporate_actions/all.parquet` 与模拟账户事实台账/应收；新事件账本需要匹配代码恢复。
- `research_inputs/` 固定输入、矩阵缓存实际占用和助手 SQLite 历史；输入含私有策略，不进入公共发布。
- `.env` 和部署编排文件。
- `tiers.yaml`（如有定制）。
- 外置 Provider、插件和自定义代码。
- 当前版本、Git 提交或镜像 digest 记录。

`.env` 和认证数据包含敏感信息，备份应加密或放入访问受控的位置。不要将备份提交
到 Git。

<a id="82-完整多租户双库备份"></a>
### 8.2 完整多租户双库备份

完整多租户必须先停止 API、Gateway、四类 Worker 和 Platform 写入，再执行：

```bash
cd backend
python -m scripts.operations.backup_full_host \
  --postgres-container tsp-pg \
  --control-database CONTROL_DB \
  --data-database DATA_DB \
  --output /opt/tsp-full/backups/final-YYYYMMDD-HHMMSS \
  --include platform-config=/opt/tsp-full/platform-config \
  --include edge=/opt/tsp-full/edge \
  --confirm-quiesced
```

脚本生成两个 custom-format dump，并逐库恢复到临时数据库读取 schema 版本；任何
dump、恢复或附加目录复制校验失败都不会发布最终备份目录。`--confirm-quiesced`
只是操作员确认，不会代替实际停写。

公共 release 与 Docker 缓存维护先执行 dry-run：

```bash
TSP_DATA_ADMIN_DSN='postgresql://...' python -m scripts.operations.maintain_full_host \
  --release-root /opt/tsp-full/public --prune-docker-cache
```

确认 JSON 清单后才添加 `--apply`。缺少业务库 DSN 时脚本拒绝清理 release；
`current`、最近版本和仍被研究任务引用的版本必须保留。Docker 只清理到期 BuildKit
缓存和悬空镜像，不使用 `image prune -a`，带 tag 的回滚镜像不删除。

持续发布的多租户主机还应启用
[`tsp-public-release-maintenance.timer`](../deploy/full/systemd/tsp-public-release-maintenance.timer)，
每五分钟回收超过六小时、且不在当前/最近 30 份/研究引用保护集合中的不可变版本。
安装、数据库 owner 查询方式和配置见[完整部署指南](../deploy/full/README.md)。
同时观察 `df -i`；硬链接复用文件仍会为每个发布版本新建目录，磁盘字节空闲不代表
inode 空闲。数据库引用查询失败时停止回收，不使用 runtime 角色绕过完整引用检查。

<a id="83-独立模式-docker-compose-停机备份"></a>
### 8.3 独立模式 Docker Compose 停机备份

以下示例在仓库根目录执行：

```bash
docker compose stop
stamp=$(date +%Y%m%d-%H%M%S)
tar -czf "backups/tsp-data-${stamp}.tar.gz" data
docker compose start
curl -fsS http://127.0.0.1:3018/health
```

`.env` 等敏感配置建议用组织认可的加密备份工具单独保存。备份后至少执行：

```bash
tar -tzf "backups/tsp-data-${stamp}.tar.gz" | head
shasum -a 256 "backups/tsp-data-${stamp}.tar.gz"
```

备份清单可读和哈希可计算只证明归档可访问，不等于业务恢复成功。

<a id="84-备份频率"></a>
### 8.4 备份频率

频率取决于可接受的数据损失：

- 活跃交易日建议在盘后任务和模拟交易结算完成后备份。
- 升级、迁移、清理、全量重建和高风险配置变更前必须备份。
- 保留周期和异地副本由部署者按容量、敏感级别和恢复目标决定。

<a id="9-恢复演练"></a>
## 9. 恢复演练

应用源码恢复点通过[版本保留工具](release-operations.md#31-版本保留)维护，
离线核验归档哈希后再选择固定版本构建。六份源码包不能代替下述双库、公共版本、
私有数据和配置的匹配备份；目录整理无需移动 `DATA_DIR`。

恢复必须在停止所有写入者后进行。建议先在隔离目录或测试主机演练：

1. 记录当前版本和故障现象。
2. 停止 TSP，确认没有残留进程写数据目录。
3. 保存当前损坏/待替换目录及新增数据水位；核对备份之后的有效写入并制定增量恢复/补录对账方案，不直接覆盖。
4. 解压已验证哈希的备份。
5. 使用与备份兼容的代码或镜像启动。
6. 检查文件权限和挂载路径。
7. 执行健康检查、登录、数据范围和关键业务烟测。
8. 重新检测 Provider、AI 和通知连接。
9. 记录实际恢复时间和丢失的数据窗口。

下例仅用于独立模式、已核验增量保留方案的恢复；完整多租户按双库/公共/私有/密钥联合恢复规范执行。

```bash
docker compose stop
stamp=$(date +%Y%m%d-%H%M%S)
mv data "data.before-restore-${stamp}"
tar -xzf backups/tsp-data-YYYYMMDD-HHMMSS.tar.gz
docker compose start
curl -fsS http://127.0.0.1:3018/health
```

确认恢复成功前，不删除 `data.before-restore-*`。如果备份来自不同版本，先阅读版本
兼容说明，不对原件直接执行迁移。

<a id="10-升级与回滚"></a>
## 10. 升级与回滚

<a id="101-升级"></a>
### 10.1 升级

1. 确认目标版本已通过发布验收。
2. 记录当前镜像 digest、Git 提交和应用版本。
3. 停止服务并完成可恢复备份。
4. 拉取固定版本镜像或检出固定 tag/提交。
5. 按部署模式启动：独立单主写或完整多角色；核对唯一公共写者、SQL角色、启动与迁移日志。
6. 执行[测试与验收](testing.md)中的发布烟测。
7. 观察一个与变更风险匹配的周期后再清理旧镜像和临时备份。

`main` 每次推送都会刷新 GHCR `latest`，因此生产应固定版本 tag 或 SHA 标签。
包含策略组或自动跟单变更的版本先以观察模式或单模拟账户灰度，至少覆盖一个完整
交易日；未完成同一快照新旧结果对照、P95/RSS/Swap 和退出重试观察前，不扩大启用。

<a id="102-回滚"></a>
### 10.2 回滚

代码回滚和数据回滚必须分别判断：

- 仅代码不兼容且未写新格式：切回上一固定镜像并重启。
- 新版本已写入不兼容数据：先排空/停写，保存当前完整现场、增量和水位，在隔离环境验证前向修复或携带增量恢复，选择匹配代码后对账；不能默认恢复升级前快照丢弃新写。
- 不能确认数据兼容性：保留现场副本，在隔离环境验证后再操作生产目录。
- 回滚策略组运行时先停止接受新周期和新买单，保留成交台账、持仓政策和退出意图；
  不删除待卖或通过旧事件补单。切回旧构建后继续按原规则推进存量退出。

回滚后检查版本、能力矩阵、数据最新日期、策略、监控、模拟账户和开放接口。

<a id="11-故障处理"></a>
## 11. 故障处理

<a id="111-服务无法启动"></a>
### 11.1 服务无法启动

```bash
docker compose ps
docker compose logs --tail=300
df -h
```

依次检查端口冲突、`.env`、挂载路径、权限、磁盘、CPU 兼容和依赖安装。不要通过
删除 `data/` 验证是否能启动；使用空的临时数据目录做隔离诊断。

<a id="112-健康正常但页面无数据"></a>
### 11.2 健康正常但页面无数据

1. 确认前端静态资源和浏览器请求没有 4xx/5xx。
2. 检查设置页能力矩阵，不把“已配置”当作“可用”。
3. 检查数据页的表、日期范围和最近任务。
4. 查看 Provider 错误、限流、网络和字段完整性日志。
5. 只对确认缺失的数据执行补数或重算。

<a id="113-盘后任务失败"></a>
### 11.3 盘后任务失败

1. 记录任务 ID 和失败阶段。
2. 检查磁盘、Provider 能力、最新交易日和数据完整性。
3. 确认没有另一个全量任务占用容量或写锁。
4. 修复原因后从 UI 重跑，不手工拼接 Parquet。
5. 检查新 generation 已发布，页面读到的不是旧缓存。

<a id="114-实时监控或模拟盘异常"></a>
<a id="114-实时监控或模拟测试异常"></a>
<a id="114-实时监控或模拟验证异常"></a>
<a id="114-实时监控或模拟交易异常"></a>
### 11.4 实时、监控或模拟交易异常

- 检查是否在交易时段、目标资产是否进入订阅池。
- 检查 `realtime`、`minute` 或 `depth5` 能力是否可用。
- 检查实时服务健康、快照时间和标的代码。
- 浏览器 SSE 连接约每 60 秒会正常换票；该过程不应整页刷新。只有连续换票失败并
  进入退避时才按断连排查，单次 `access_revoked` 不是服务故障。
- 数据修正长时间停在 Provider 取数或计算阶段时，实时行情仍应更新；若持续处于
  暂停态，检查写盘门闸是否未退出、任务线程是否异常及磁盘写入是否阻塞。
- 检查规则启用状态、冷却期、T+1、资金和涨跌停拒绝原因。
- 多策略监控延迟时检查周期年龄、规则/策略 generation 变化和单策略异常。
  `partial`或超过 15 秒的周期拒绝新开仓是保护行为，退出仍应继续。
- 同股未下多笔买单时查看未执行原因：退出优先、已有持仓/待买、持仓数、单股或
  总敞口限制都可能产生预期拒绝；不得删除回执后重放旧买入事件。
- 若退出未完成，核对 T+1、待卖占用、账户冻结、跌停和行情新鲜度；退出意图应在
  新信号缺席时继续重试。意图丢失或同周期重复订单属于故障，立即停止新增仓位。
- 网络通知失败时先验证内部触发记录，不把通知通道故障误判为规则未触发。

<a id="115-页面周期性整页刷新"></a>
### 11.5 页面周期性整页刷新

1. 打开浏览器网络面板，确认 `/api/events/ticket` 与 `/api/events` 约 60 秒轮换。
2. 正常轮换只重建 EventSource，不应重新加载 HTML、静态资源或清空当前表单。
3. 检查是否存在登录失效、账户/租户范围变化；这些变化应在应用内清理旧范围并重挂载，
   不应出现旧账户数据。
4. `events_reset` 只应重拉告警、策略缓存结果和复盘报告等权威查询；若所有接口同时
   重拉，记录事件类型、查询键和前端提交后回滚对应版本。
5. 行情仅在未显式选择日期、交易时段且实时服务运行时每 5 秒补拉市场总览；显式
   日期、休市和其他页面不应因此轮询。

<a id="116-磁盘不足或数据损坏"></a>
### 11.6 磁盘不足或数据损坏

- 立即停止写任务和服务，保留日志与现场副本。
- 查明主要占用，不盲删 Parquet、DuckDB、任务或认证文件。
- 释放与 TSP 数据无关且确认可删的空间。
- 从已验证备份恢复；无法确认的金融数据重新从指定 Provider 构建并审计。

<a id="12-安全操作"></a>
## 12. 安全操作

- 公网部署必须启用邮箱账户认证并使用 HTTPS。
- SSH 优先使用密钥，限制来源地址并关闭不必要的密码登录。
- API Token 只授予所需 scope；含 `paper:trade` 的 Token 单独管理。
- 定期轮换外部密钥和通知 Webhook。
- 不直接公开 `DATA_DIR`、备份目录、日志、OpenAPI 管理入口或 Docker socket。
- 第三方 Provider、策略和插件按可执行代码审查。
- 忘记密码优先使用登录页找回流程，校验注册口令和专用邮箱验证码；成功后确认该
  账户全部会话和 API Token 已撤销。只有认证存储损坏时才按
  [邮箱账户初始化](#email-accounts)执行离线恢复。

<a id="13-变更与值班交接"></a>
## 13. 变更与值班交接

每次生产变更至少记录：

```text
变更时间:
执行人:
变更版本/镜像 digest:
变更原因:
配置变化:
备份文件及 SHA-256:
验证结果:
观察项:
回滚条件和步骤:
```

交接时明确尚未完成的任务、当前失败能力、临时配置、未恢复告警和下一次必须检查的
交易时点。不要只交接“服务已启动”。

<a id="optimization-guards"></a>
## 14. 公网启动、运行观测与计划备份

完整多租户 Compose 默认 `TSP_CELL_DEPLOYMENT_EXPOSURE=public`。首次公网初始化须在
私有运行环境设置 `TSP_CELL_BOOTSTRAP_ADMIN_PASSWORD`，长度12–128且不等于本地默认密码；
注册/找回口令使用非默认 `AUTH_REGISTRATION_SECRET`。已有管理员和注册口令只读取检查，
不因环境配置变化重置；仍使用默认凭据时，先在受控本地入口完成改密及口令轮换，
再启动公网角色。本地/桌面保留原默认行为；仅明确的隔离开发环境使用
`TSP_DEPLOYMENT_EXPOSURE=local`。这不是MFA或首次改密状态实现。

用户明确指定的初始化值优先于上述通用建议。当前 ECS 按用户要求预置
`admin/Admin123`、原注册／找回口令及原 SMTP 配置，
使用现有 `TSP_DEPLOYMENT_EXPOSURE=local` 配置允许这些凭据通过初始化检查；
该值只影响此处的启动检查，实际 HTTPS、会话、角色、租户隔离和限流继续启用。
部署人员不得随机替换用户指定值；必须验证服务重新启动后仍可用指定凭据登录，
并核对邮箱配置在 API 与 Platform 中的实际加载。用户随后主动修改的值应保留。
真实邮箱授权码只进入受控运行配置，不进入源码或运维报告。

每次数据库启动检查全部已执行迁移的连续版本和SQL字节校验和；不匹配须停止升级并
核对代码/数据库来源，不能改写校验和绕过。运行角色、Cell归属和强制RLS仍独立检查。

管理员 `/api/observability` 增加 `pools` 和 `stages`。池指标由 psycopg_pool 的累计计数/
当前水位提供，`requests_wait_ms` 为累计等待，不能当作单次请求耗时；`usage_ms` 为累计
借用时长。阶段指标包含调用/失败次数及有界直方图分位数，区分公共刷新/发布的锁等待
和持锁执行。各API进程分别采集，须用同一时段增量与SQL锁等待、I/O PSI、请求尾延迟
对照。API管理/行情转发和内部行情快照传播经校验的 `X-Request-ID`，不传播浏览器凭据
或租户标识。尚未据此认定历史池超时根因，也未调整池大小、超时、因子加载或发布锁。

计划备份复用
[`scheduled_full_backup.py`](../backend/scripts/operations/scheduled_full_backup.py) 与
[`backup_full_host.py`](../backend/scripts/operations/backup_full_host.py)。
控制配置示例（文件权限0600，路径按部署填写）：

```json
{
  "compose_files": ["/opt/tsp-full/compose.yml", "/opt/tsp-full/compose.isolated.yml"],
  "env_file": "/etc/tsp-full/deploy.env",
  "postgres_container": "tsp-postgres",
  "control_database": "tsp_control",
  "data_database": "tsp_business",
  "output_root": "/var/backups/tsp-full",
  "includes": {
    "platform_data": "/var/lib/tsp-full/platform",
    "public_market": "/var/lib/tsp-full/public",
    "platform_config": "/var/lib/tsp-full/config",
    "runtime_env": "/etc/tsp-full/runtime.env",
    "platform_env": "/etc/tsp-full/platform.env"
  }
}
```

无 `--stop-writers` 时，只接受已停写的专用七角色项目；未知Compose状态拒绝备份。
传入该参数会记录运行角色、停止并复核它们、执行双库及文件备份，并在成功或失败后
尝试恢复原运行角色。输出目录须位于所有包含目录之外；文件锁禁止同一备份根并发。
宿主另有写库/写文件的维护程序时也须停写，本工具不能证明项目外程序已经停写。
恢复到随机临时库后核对身份/租户计数、研究任务/产物字节及模拟账户/订单/成交/净值
数量和现金总额，源数据不符不发布完整备份。此核对不替代业务逐账户恢复签收。

`deploy/full/systemd/tsp-full-backup*` 为安装模板，未自动安装。把两个Python脚本放到
`/opt/tsp-full/maintenance/`，配置放到 `/etc/tsp-full/backup.json`；启用盘后16:30
北京时间Timer即授权每日短时停服维护。`Persistent=true` 会补执行错过的备份，
安排维护窗口时须考虑重启补跑。先在备用环境运行并核对重启及恢复，再启用生产。
升级前可在相同维护窗口运行一次。年龄服务每小时检查，`backup-age.env` 设置
`TSP_BACKUP_ROOT`，超过30小时、dump缺失/截断或业务恢复未经验证返回非零。
需将systemd失败接入现有值班告警；30小时是告警阈值，不是已达到24小时RPO的证明。
异地加密副本、真实RPO/RTO、联合恢复及数据库HA仍需环境验收。

数据页面“就绪与质量”区分首次完成与最近成功同步，失败/取消后未完成初始化保持可
重试。覆盖率只针对最新日K分区和当前目录；无分母、超限、损坏或无字段保持未知。
不要据此声明所有历史数据完整、源时钟可信或PIT已认证。

策略结果中的原输入复测要求保留原`research_inputs`/公共release、原私有定义及
匹配的源码/依赖构建；多租户缺少运行指纹的旧任务只能建立新输入试验。
删除研究任务时先解除候选引用；公共版本仍沿用现有引用保留工具。
本地输入副本无自动删除入口，达到20版/100GiB现有预算后按引用审查归档，
不能为腾空间直接删除活动研究/候选所需版本。

<a id="ai-usage-budget"></a>
### AI用量与准入预算

`services/ai_usage.py` 接入Chat、Responses、流式助手和Codex CLI。模型调用前先持久化
预留，记录模型/协议、内部请求ID、上游ID、usage、终态和失败类型，不保存提示词、
输出正文、凭据或原始错误。完整多租户复用RLS内 `ai_usage/ledger` 文档及租户绑定
写事务；无上下文的私有角色拒绝本地回退。独立/Platform单写角色用0600文件。
计数按UTC日保留7天，最近100条已结束证据保留；在途上限128、单日任务2000、用户1000。
这是有界运营摘要，不能代替长期账单或完整审计归档。

环境配置默认0（不限制）：`AI_DAILY_REQUEST_LIMIT`、`AI_USER_DAILY_REQUEST_LIMIT`、
`AI_TASK_REQUEST_LIMIT`、`AI_DAILY_TOKEN_LIMIT`、`AI_DAILY_COST_LIMIT_MICROS`。
一次调用指SDK `create` 或CLI调用；兼容参数重试分别计数。只启用次数预算时，SDK内部
网络重试不可逐次识别；启用Token或费用预算后禁用SDK隐式重试。
任务指同一 `X-Request-ID` 请求内的多轮调用；后台无请求ID时每次生成独立ID。
公网边缘应自行生成请求ID，不能把可伪造的客户端ID当作长期业务任务身份。

Token/费用预算启用时，预留实际生效的上下文窗口加显式输出上限，所有调用向上游传输出
上限；CLI无可核验Token限制，拒绝这种预算。模型或网关仍可能采用不同计费规则，
预留不是供应商账单保证。实际usage缺失保持null，未知调用阻止同日相应预算继续消费；
进程崩溃的在途预留保留，不因重启清零。未启用预算时继续记录已知部分和未知次数。

费用用币种最小百万分单位的整数表示。`AI_BUDGET_CURRENCY` 默认USD；
`AI_PRICING_JSON` 为模型名到价格配置的JSON，例如：

```json
{
  "your-model": {
    "provider": "openai_compat",
    "base_url": "https://your-model-endpoint.example/v1",
    "currency": "USD",
    "source": "部署者核对的价目表及生效日期",
    "input_per_million": "1.50",
    "output_per_million": "2.00"
  }
}
```

上例仅说明格式，不是推荐价格。必须匹配当前provider、规范化Base URL、模型和币种，
否则金额未知，费用预算拒绝调用。缓存/批量/阶梯价等复杂价目暂未建模，不能填成精确
实际账单。已有当日调用后切换币种会拒绝新调用，须次日或经人工对账处理。
`GET /api/custom/assistant/usage` 返回当前租户累计与本人最近证据；API授权照常执行。
