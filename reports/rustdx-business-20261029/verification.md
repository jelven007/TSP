# rustdx v2.0.4 业务适配与全股票实时行情性能核验

核验日期：2026-10-29，北京时间约 06:48–06:59。业务基线：TSP v0.4.37，已部署提交 `f4d8c11b766bf62c269d8fca05b7ef6d9a559e42`。本报告使用上游官方源码、两端已安装模块、真实网络请求和隔离写盘结果。

## 1. 核验范围与版本身份

GitHub 官方 API 返回最新标签和 main 均为 `v2.0.4` / `c4c0273456874898aed50447fd900521d9ffd3b8`；GitHub Releases 列表为空。上游 README 将源码版本与注册表历史发布分开，列出的 crates.io 历史发布版本为 2.0.0。TSP 使用固定提交的本地核心源码依赖，不能把注册表安装命令等同于当前集成版本。<a href="#source-1">[cite:1]</a><a href="#source-2">[cite:2]</a>

本地运行进程的工作目录为已安装 v0.4.37 release；ECS Platform 镜像 revision 为同一 TSP 提交。两端实测安装模块均为桥接 0.2.0、核心 2.0.4、池接口 3、最多 8 条业务连接和 1 条独立探测连接；核心与补丁 SHA-256 完全一致。没有使用工作区尚未部署的并行修改作为运行版本。<a href="#source-3">[cite:3]</a>

“所有股票”采用当前产品的沪深四板股票池：沪主板 1,700、深主板 1,493、创业板 1,411、科创板 618，合计 **5,222** 只。上海 2,318 只、深圳 2,904 只；每批最多 60 只且按市场分批，共 **88 批**。两端股票池身份一致，证券集合 SHA-256 为 `77ba73082a39eafcfa5f0b8114391f669b12b2db8931154dc29e3e557dfa3f5d`，目录 `as_of=2026-10-28`。不包含已经退役的 ETF 路径或产品范围外的北交所股票。<a href="#source-3">[cite:3]</a><a href="#source-4">[cite:4]</a>

已公布日历判断 2026-10-29 为交易日，日历身份为 `SSE-2019-2026-20261026`。核验发生在开盘前；两端管理员状态 API 均返回 `interval_s=1.0`、`enabled=true`、`running=true`、`paused=false`、`is_trading_hours=false`、`is_polling_window=false`，本次运行尚无当日股票轮次。今天 09:00 的目录刷新也尚未发生。<a href="#source-3">[cite:3]</a><a href="#source-5">[cite:5]</a>

## 2. 全股票真实取数结果

两端使用各自已部署 Python 环境和原生模块，在独立测试进程中运行同一固定节点 `117.34.114.13:7709`，配置 8 条业务连接、8 秒 socket 超时。每端执行 1 轮冷启动、3 轮预热和 **100 轮正式样本**，按轮次开始时间至少间隔一秒；慢轮完成后才开始下一轮。正式样本使用 nearest-rank 分位数。<a href="#source-4">[cite:4]</a>

测量入口是 Provider 显式股票快照调用 `get_auction_snapshot(symbols)`，复用实时快照的 `_quote_rows` 与原生全量报价链路。耗时包含网络采集、JSON 解码和标准字段转换；不包含股票名称缓存读取、指数补拉、日K写盘、enriched 计算、监控评估、公共传输或浏览器渲染。

| 指标 | 本地 macOS | 线上 ECS |
| --- | ---: | ---: |
| 正式完整轮次 | 100 / 100 | 100 / 100 |
| 每轮股票覆盖率与有效价格覆盖率 | 100% | 100% |
| P50 | 0.658 秒 | 0.525 秒 |
| P95 | **1.417 秒** | **0.695 秒** |
| P99 | 2.328 秒 | 0.980 秒 |
| 最大耗时 | **2.365 秒** | **0.992 秒** |
| 超过一秒的正式轮次 | **16 / 100** | **0 / 100** |
| 冷启动完整取数 | 7.593 秒 | 5.916 秒 |
| 网络重试 / 失败 | 0 / 0 | 0 / 0 |

表中原始数据分别来自 本地结果（附件保存在受控归档）、线上结果（附件保存在受控归档）和[汇总](measurement-summary.json)。<a href="#source-4">[cite:4]</a>

完整轮次要求全部预期证券返回、价格非空且有限、无重复或多余证券；允许停牌证券的零价。每端预热及正式调用都实际发出批量网络请求，最终网络计数 9,188，正式每轮增量 88；报价路径没有用静态缓存代替网络采集。两端业务连接实测均为 8、探测峰值为 1，后台请求峰值为 0。线上环境虽然保留 `RUSTDX_QUOTE_CONNECTIONS=35` / `RUSTDX_WORKERS=35`，Python 和原生层均钳制为 8，不能靠把这两个配置继续调大获得更多连接。<a href="#source-3">[cite:3]</a><a href="#source-4">[cite:4]</a>

这组结果支持“本次盘前、热连接、无后台池竞争时，线上 5,222 只股票完整取数的 100 个样本全部小于一秒”；本地已经出现一秒超时轮次。它不能认证当日盘中全链路、长期尾延迟或故障恢复的一秒目标。

## 3. 写盘、时间语义与实际轮询边界

### 3.1 实时显示与秒级历史归档

另取真实的完整股票响应，调用已部署 `QuoteService._build_daily/_build_quote_extra` 和 `SecondQuoteArchive`；归档写到隔离目录，使用真实磁盘及默认的 **10 轮或 10 秒**提交策略。

| 指标 | 本地隔离磁盘测试 | ECS 隔离磁盘测试 |
| --- | ---: | ---: |
| Provider 返回股票 | 5,222 | 5,222 |
| 日K标准化有效记录 | 5,216 | 5,216 |
| 构建日K及补充字段表 | 13.805 毫秒 | 23.401 毫秒 |
| 归档入队 | 1.027 毫秒 | 20.625 毫秒 |
| 入队至首份完整文件可见 | **10.073 秒** | **10.084 秒** |
| 单份实际写盘耗时 | 7.108 毫秒 | 4.817 毫秒 |
| 最终归档记录 / 丢失轮次 | 5,222 / 0 | 5,222 / 0 |

这是一份真实快照的存储时延测试，不是全链路 P95 测试；文件可见时间以最多约 50 毫秒的观察精度测得。默认异步缓冲决定了首份历史归档不会在一秒内可见，磁盘本身的毫秒级写入耗时不能替代缓冲等待。生产实时展示缓存与这条归档旁路各有生命周期。<a href="#source-6">[cite:6]</a><a href="#source-7">[cite:7]</a>

5,216 条日K并非报价漏了 6 只股票：完整报价已经返回 5,222 条，日K构建按停牌/零成交规则排除 `002717.SZ`、`002827.SZ`、`301468.SZ`、`601326.SH`、`605258.SH`、`688496.SH`。归档仍保留全部 5,222 条真实观测。<a href="#source-6">[cite:6]</a><a href="#source-7">[cite:7]</a>

第一次 ECS 写盘试验使用 `/tmp`，该挂载为总容量 512MB 的内存盘，不满足归档默认 1GB 空闲阈值，导致 1 轮被拒绝写入。该结果保存于 `ecs-storage.json`，不能外推为生产磁盘空间不足。核对实际 `/app/data` 文件系统约有 93.67GB 空闲后，在其独立缓存子目录复测成功；上表采用 `ecs-volume-storage.json` 的磁盘结果。没有修改生产空间门禁。<a href="#source-6">[cite:6]</a>

### 3.2 一秒取数不证明一秒行情新鲜度

两端独立取数样本中的 **全部 5,222 条 `source_timestamp` 均为空**，Provider 对外 `timestamp_basis` 均为 `received`。其中 2 条缺 `source_time`；其他证券的 `source_time` 是不带日期的单证券最后事件时间。把它与今天的日期拼接只能得到推断时间，不能证明源快照确实来自今天、已经过收盘边界，或所有证券采样于同一个时点。<a href="#source-6">[cite:6]</a><a href="#source-8">[cite:8]</a>

88 批报价分别完成且没有统一的交易所快照游标；整轮返回时刻不是每只股票的源事件时刻。低成交证券也未必每秒发生交易，验收应分别检查“完整采样轮耗时”“采样轮之间的间隔”“源状态的有效性”，不能要求每只股票每秒都成交。

TSP 常驻轮询以 `cycle_started + interval` 为下一轮起点，超过一秒时立即进入下一轮，不补造或积压遗漏的采样轮。`行情刷新…耗时`日志在日K写盘和 enriched 计算之前记录，不能将该日志数字当作完整业务链路完成时间。全市场分钟K采集是另外一条任务，当前 rustdx 的有效最低轮询间隔为 60 秒。<a href="#source-7">[cite:7]</a><a href="#source-8">[cite:8]</a>

### 3.3 昨日盘中证据的版本限制

对两端 2026-10-28 的全部保留归档分片读取版本与观察时间：本地 1,825 份、股票记录 61,645,710 条；ECS 1,768 份、股票记录 78,627,654 条。两端全部归档版本均为 `1.12.2@9dfc024c9662fa5091099c1f17610569ee2eca8b`，没有 v2.0.4 的昨天盘中归档。因此旧归档可以证明此前曾持续采集，不能充当已部署最新版的盘中性能验收。<a href="#source-3">[cite:3]</a>

## 4. 当前业务不满足点与责任归属

以下优先级按当前业务影响排列。“上游尚未验收”“TSP 尚未接入”“部署编排问题”分别归属，避免把接口存在等同于本产品业务已经可用。

| 优先级 | 业务需求与不满足点 | 责任与已确认边界 | 处理方向 |
| --- | --- | --- | --- |
| P1 | 全股票稳定一秒采样 | 当前链路：本地正式样本有 16% 超过一秒；线上盘前样本达标，但冷启动、后台共享池、盘中及全链路尚未验收 | 先取得同版本盘中全链路分位数、覆盖率和轮间隔；针对网络尾延迟及池竞争处理 |
| P1 | 精确源日期、新鲜度、收盘定版 | 源与桥接契约：报价源时间不带日期，接收时间不能证明源快照日期；全部样本缺明确源时间戳 | 保留未知资格；补充可靠、带日期的源证据，继续使用官方日线定版 |
| P1 | 完整历史分钟窗口 | 源保留范围：另一专项真实读取 `301110.SZ` 时，可用节点最早仅到 2026-06-03/04，三四月区间为 0 行；增加空页请求或分页上限不能得到缺失数据 | 为要求的早期区间接入真实历史分钟文件或其他合格源，逐证券核对覆盖 |
| P1 | 严格历史财务原始版本与公告时点 | 上游数据和 TSP 使用契约：当前历史表可用，但原始归档记录 `publication_time_verified=false`；最新修订版本不能倒认证为历史当天版本；通用0010字段仍有候选/未知资格 | 继续保留观察修订；对严格研究另补逐字段和历史公告版本证据 |
| P1 | 公司行为现金到账、股份上市与可卖权益 | 源覆盖：当前 Provider 有除权日和分红/送转/配股数值，没有已认证的登记、到账、缴款、上市等日期 | 继续暂停缺证据的权益结算；补公告支持的事件日期 |
| P2 | 真正连续逐笔与启用前历史秒级/竞价回放 | TSP 尚未接入核心逐笔扩展；当前秒级归档是轮询采样，不覆盖启用前历史。上游也未验收全品种逐笔完整性/业务单位 | 另定义逐笔消费、日期与完整性门禁；不能将采样快照当作完整逐笔 |
| P2 | 通过订阅主动推送取代88批轮询 | 上游0547已有主动刷新持续更新入口，但官方仍未观察并认证真实被动推送；TSP未接入该订阅模型 | 单独在盘中验证被动帧、全订阅键同步及恢复，不把主动刷新当作推送证明 |
| P2 | 统一历史分时日期与任意早期保存范围 | 上游0feb默认日期资格仍为 `Unsupported`，非空历史样本不认证连续完整历史；当前TSP使用bars路径 | 有完整独立带日期参照才可使用扩展能力，不能以换接口自动补齐历史 |
| P2 | 目录当前状态可还原全部历史ST/退市/分类事实 | 当前维表与F10不足以认证所有历史时点；历史状态从成功观察起积累。行业/概念/龙虎榜属于独立扩展路径 | 使用公告或独立历史状态源；保留未知，不将当前目录倒填过去 |
| P2 | 分页连接变化后的不中断恢复 | TSP部署编排：共享分页来源检查正确拒绝混用连接；工作区已有完整重采一次的局部修复，但常驻v0.4.37尚未加载该候选 | 按现有发布流程独立验收部署；不要把应用恢复问题归为上游缺少分钟接口 |

矩阵依据：取数与写盘实测、官方应用契约/验收矩阵、已部署 Provider、现有历史深度专项。<a href="#source-4">[cite:4]</a><a href="#source-6">[cite:6]</a><a href="#source-8">[cite:8]</a><a href="#source-9">[cite:9]</a><a href="#source-10">[cite:10]</a><a href="#source-11">[cite:11]</a>

“财务没有回填到2020-11-02”不属于当前缺口：此前已完成两端五表各 124,953 条、每端合计 624,765 条并逐字段与 SHA-256 对齐。本报告区分实际历史记录覆盖与严格历史公告版本认证。类似地，上游 CLI/runner 的 Linux部署、数据库恢复或整套指标引擎未验收，不能直接列为 TSP 已有独立实现的缺失能力。<a href="#source-12">[cite:12]</a><a href="#source-9">[cite:9]</a>

## 5. 当前判定与盘中验收边界

| 验收口径 | 当前判定 |
| --- | --- |
| 当前固定股票池全部返回且每只股票有效 | 两端本次100轮均通过 |
| 本地热连接完整取数P95≤1秒 | 不通过 |
| ECS盘前热连接完整取数P95≤1秒 | 本次通过，100轮全部≤1秒 |
| 冷启动及故障恢复也≤1秒 | 不通过；冷启动已超过一秒，额外本地观察曾出现1次重试/节点切换 |
| 秒级历史归档每轮一秒内持久可见 | 默认10轮/10秒缓冲不满足；本次单份实测约10.08秒 |
| 今天盘中完整股票、真实新鲜度、API/SSE/浏览器全部一秒完成 | **尚无可验收证据**；当前未开盘，取数测速未覆盖全链路 |

当日盘中应先锁定 09:00 更新后的完整股票池，分别在开盘活跃窗口及正常连续竞价窗口取至少 100 个完整样本，同时保留覆盖、源日期资格、请求重试、业务池竞争、采集至缓存/发布完成时间、轮次间隔和归档丢轮指标。当前采集日志的取数耗时必须与写盘/计算/发布耗时分开；API快读或不变价格不能替代网络及时间证据。由于当前源缺明确的快照日期，单靠性能达标也不能关闭严格源新鲜度验收。

本轮工作为核验与报告：新增真实测量结果、公开上游证据和隔离测试文件；没有实施源码修复、调整生产配置、提交或发布。原有后台任务及并行修改保留。ECS写盘试验只在独立缓存目录生成测试分片，未向生产 `quote_second` 或日K分区写入测试结果。

## 6. 复核入口与证据来源

核心复核文件：

- [机器汇总](measurement-summary.json)、本地逐轮样本（附件保存在受控归档）、线上逐轮样本（附件保存在受控归档）。
- [本地运行身份](local-runtime.json)、[ECS运行身份](ecs-runtime.json)、[本地服务状态](local-service-status.json)、[ECS服务状态](ecs-service-status.json)。
- [本地写盘实测](local-storage.json)、[ECS真实磁盘写盘实测](ecs-volume-storage.json)、[ECS内存盘限制记录](ecs-storage.json)。
- [上游API身份](upstream.json)、上游证据采集脚本（附件保存在受控归档）、运行及写盘脚本（附件保存在受控归档）。

原取数命令使用已部署release的 `backend/.venv/bin/python` 和 `scripts/performance/benchmark_rustdx_quotes.py`，参数为 `--connections 8 --rounds 100 --warmup 3 --interval 1 --round-timeout 90 --max-p95-ms 1000`，股票池来自实际Platform数据目录，结果目录与生产业务分区隔离。ECS镜像未包含运维基准脚本，测试复制同一已部署版本的脚本至临时目录，再调用镜像内原生模块。

本报告对可重复的本机事实使用跨端模块身份、请求计数和业务覆盖互相校验。上游能力资格以官方公开合同为依据，来源未独立的声明不伪称三方独立认证；历史分钟深度引用已有真实读取专项，本轮没有重跑该历史区间。

交付前再次校验：全部本地证据链接存在；每端正式样本恰为100轮且逐轮完整；预热/正式轮请求计数均增加88；两端状态API为200且处于未开盘阶段；两端磁盘归档各写入5222条且无丢轮；两个辅助脚本语法可解析；凭据格式扫描未发现真实GitHub令牌；`git diff --check`通过。文件摘要见[sha256.json](sha256.json)。这些检查不替代尚未执行的盘中全链路验收。

<a id="source-1"></a>[cite:1] GitHub 官方仓库、tags、main 与 Releases API，本次原始返回：[upstream.json](upstream.json)。[最新标签](https://github.com/jelven007/rustdx/tree/v2.0.4)。

<a id="source-2"></a>[cite:2] rustdx v2.0.4 官方 [README](https://github.com/jelven007/rustdx/blob/v2.0.4/README.md)。

<a id="source-3"></a>[cite:3] 本次已部署原生模块、目录与历史版本审计：[local-runtime.json](local-runtime.json)、[ecs-runtime.json](ecs-runtime.json)；ECS镜像revision与本地运行进程工作目录另通过只读命令核对。

<a id="source-4"></a>[cite:4] 本次真实5222证券固定节点基准：local-quotes/results.json（附件保存在受控归档）、ecs-quotes/results.json（附件保存在受控归档）、[measurement-summary.json](measurement-summary.json)。基准入口：[benchmark_rustdx_quotes.py](../../backend/scripts/performance/benchmark_rustdx_quotes.py)。

<a id="source-5"></a>[cite:5] 本次管理员认证后实时状态：[local-service-status.json](local-service-status.json)、[ecs-service-status.json](ecs-service-status.json)。

<a id="source-6"></a>[cite:6] 本次真实响应及隔离写盘：[local-storage.json](local-storage.json)、[ecs-volume-storage.json](ecs-volume-storage.json)、[ecs-storage.json](ecs-storage.json)；首次内存盘试验与生产磁盘试验分开记录。

<a id="source-7"></a>[cite:7] TSP链路：[quote_service.py](../../backend/app/services/quote_service.py) 的 `_poll_loop`、`_process_full_market_records`、`_build_daily`、`_flush_live_enriched`；[second_quote_archive.py](../../backend/app/services/second_quote_archive.py) 默认缓冲及 `_writer_loop`；[quote_bridge.py](../../backend/app/multitenancy/quote_bridge.py) 公共快照消费。

<a id="source-8"></a>[cite:8] TSP [Provider](../../backend/app/plugins/rustdx/provider.py) 的 `_quote_rows`、`_map_history`、`get_corporate_actions`；[原生报价调度](../../backend/native/rustdx_native/src/lib.rs) 的 `fetch_quotes`；[数据源契约](../../docs/reference/integrations.md#rustdx)；[财务归档资格](../../backend/app/plugins/rustdx/financial.py)。

<a id="source-9"></a>[cite:9] rustdx官方 [应用集成合同](https://github.com/jelven007/rustdx/blob/v2.0.4/docs/development/APPLICATION_INTEGRATION.md)；[生产验收矩阵](https://github.com/jelven007/rustdx/blob/v2.0.4/docs/testing/PRODUCTION_ACCEPTANCE.md)。

<a id="source-10"></a>[cite:10] rustdx官方 [26命令接口合同](https://github.com/jelven007/rustdx/blob/v2.0.4/docs/reference/COMMAND_API.md)。

2026-10-30文档维护清理四份外部仓库全文副本，以上固定v2.0.4链接继续作为来源入口。
`upstream.json`的`saved_as`及`sha256.json`保留原采集元数据，描述历史采集时点，
不表示这些副本仍在当前目录；原实测结果、来源身份与计数保持。

<a id="source-11"></a>[cite:11] 本仓库另一专项真实读取与恢复边界：[301110.SZ分钟核验](../minute-301110-20261029/verification.md)、[源深度机器结果](../minute-301110-20261029/source-depth.json)。当前已部署版本与未部署候选须分开。

<a id="source-12"></a>[cite:12] 已完成的两端财务回填：[财务核验](../financial-backfill-20261029/verification.md)、[机器结果](../financial-backfill-20261029/verification.json)。
