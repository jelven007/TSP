# 代码二次开发与 AI 扩展指南

2026-11-07定版校验、研究资格、建议和排序是L3内部整合，入口见[统一候选](upgrade-20261107.md)。
不存在新Provider/HTTP API或SQL迁移；策略示例的公开只读Python辅助函数
`market_data.previous_session`只复用已发布日历，未知时抛错。
不得绕过15:00证据、以更早日线补T-1或新增缓存掩盖文件更新。
扩展继续保留现场等待确认、完整池分母、租户隔离和旧冻结输入。
初始化原配置分钟窗口须先采集后校验及派生；窗口外历史官方批量定版不代替消费者的
精确T-1竞价校验，内部`minute_start`只表示首次采集边界，不是通用跳过校验开关。

2026-11-07发布流水线为L3工程工具与工作流修改，无新公开插件接口或SQL迁移。
扩展环境命令使用 `scripts/release/pipeline.py` 的受控argv/probe/postcheck，
将脚本、配置与产物清单纳入冻结inputs；不复制策略、备份或业务逻辑。
固定阶段、回执失效、write中断协调及暂停契约见[发布流水线](release-pipeline.md)。
修改镜像构建时保持Runtime/Compute两架构同SHA/源码树与联合正式提升门；
现有请求审计无完整行哈希，不能假定哈希差分已接入全部应用写入。

本轮属于既有L3内部契约整合，无新公开插件API、Provider数据集或SQL迁移；
真实入口和升级复核项见[2026-11-06升级](../reports/releases/unified-upgrade-20261106/verification.md)。
当前保存默认60秒；下列90秒说明保留其历史来源，冻结定义不自动迁移。

S02实时列属L3内部展示接线：复用ColumnConfig、账户storage和技术信号字段。
`market-snapshot`的可选实时模式须按证券及当日缓存过滤，旧调用兼容；
扩展不得将raw_close/实时评分覆盖历史归档字段。契约见
[实现与验证](../reports/engineering/s02-live-results-20261106.md)。

2026-11-06普通分钟信号历史查询为L3内部读取修复：现有`summaries`可按明确的
信号策略范围读取同日扫描，API通过选定record_id读取明细；没有公开新API或schema。
扩展须保留默认计算身份、成功空扫描、资格与实际扫描优先级和租户隔离，
不能通过改写旧记录“恢复”历史，见[回执](../reports/releases/unified-upgrade-20261106/verification.md#historical-hotfixes)。

最新升级为 L3 有界内部修改：`Settings.retired_builtin_strategies` 控制内置发现，
默认空，按来源和 ID 跳过，私有源不受影响；不提供数据清理或新的公开插件接口。
隔夜费用可选 `transfer_fee_fixed` 使用元，预留含费预算并按汇总委托累计增量计费，
旧缺字段不补入固定金额。真实入口及双端边界见[最新升级](../reports/releases/current-upgrade-20261103/verification.md)。

本轮统一升级为各领域L3既有实现三方整合，无新增通用插件接口；
公共缓存、调度、模拟账户SQL权限、秒精度及前端查询须按同SHA完整复验，
见[统一升级](../reports/releases/unified-upgrade-20261103/verification.md)。

时分/秒弹层仅滚动内部选项列表，不能因选中项自动定位滚动其模态祖先；
升级保持已选值、窄屏可操作及Escape焦点/取消契约。

Full目录回暖为L3生命周期接线，复用Repository现有回调、单飞后台预热和
QuoteService公共通知，不新增插件API/Provider/schema。升级保持只读snapshot热路径、
成功空通知和关闭回调移除；见[缓存修复](../.claude/artifacts/fixes/full-catalog-cache-20261103.md)。

自定义早盘清仓与尾盘建仓的时间输入均复用公共`TimePicker`，属于L3局部前端接线。
三项时间均启用`showSeconds`并保存`HH:mm:ss`。尾盘范围13:00:00～14:58:00，
精确启动秒只用于确认时钟，分钟 K 标签向下取整；不得复制时间弹层或放宽秒级撮合能力。

尾盘建仓启动后90秒为L3内部时间契约：六项参数ID保留，内部可选
`evaluation_delay_minutes`（新保存1.5，冻结缺省0或旧值1）。不得在历史加载时补1.5，
不得增加第七个用户控件。`evaluation_time`为启动后90秒确认时间，
`bar_time`为向下取整的启动分钟数据标签，不得混用；实时完成门闸用前者，
价格/广度/双MA5窗口用后者。14:30:20附着14:30行并携带确认时间14:31:50；
回放分别返回两个时点。冻结旧0/60秒定义仍要求秒数为00。
分钟成交事件向上对齐14:32，保留秒级信号时间；next_minute_open必须有14:32真实开盘。
MA5数量是值的数量，沿用现有N值算法；1分七值14:30至14:24，
5分四值14:30>14:25>14:20>14:15；相邻MA差值严格大于`1e-10`才算上升，
小于或等于该阈值按持平淘汰。没有新增公共API、
Provider、schema或专用S01/S02执行能力。

行情调度为L3内部扩展：采集共享`market_time.TRADING_DAY_COLLECTION_WINDOWS`，
日K默认15:30、周K默认周六09:00；当日修正固定交易日15:35，复用
`repair_daily.run_repair_daily`依次执行1d/1m/5m/10m/15m闭区间修正。
5/10/15分K只读取本地1分K，不新增Provider、插件接口或SQL迁移。
修正请求在占槽时持久等待，成功同日去重，失败/取消不自动重跑，跨日等待标记中断。
全量修正保持merge-upsert，不承诺删除旧异常时间键或补造停牌成交。
新默认只填缺失偏好，已有用户自定义时间保留；运行配置更新与源码部署须分别核验。
ScheduleOverview扩展任务名直接使用配置label，新增扩展源沿用相同展示规则。

一级菜单默认表为L3内部配置：信号、持仓、模拟分别保留`/signals`、`/lots`、
`/paper`路由。前端Layout、MenuSettings与后端`DEFAULT_NAV_ORDER`保持同一默认顺序，
新增扩展菜单仍按既有注册和排序规则处理；不得因名称简化迁移业务ID或覆盖已保存的
排序/显隐。新用户/空间采用当前默认，普通租户权限及Platform偏好覆盖链保持。

模拟账号生命周期为L3核心修改，沿原paper领域和查询键，不新增插件或通用扩展接口。
新增 `PUT/DELETE /api/paper/account` 与可选 `initial_cash_editable`，旧创建/费用/冻结兼容；
ID保持，费用前向生效，本金仅无委托/成交/持仓/净值时可改。删除限定账号专属业务和原件，
共享策略/监控保留；不可识别归属的旧提醒保留。升级复核文件写锁/失败恢复、
SQL schema 8及权限不变、SSE共享事件注册、取消旧请求和清理缓存、最后账号空态。
SQL旧版本明确拒绝删除并要求迁移，API不自动迁移；当前未部署。

通用分钟策略的已定时卖出只与同一信号时点的入场冲突，不得扩展为全天禁止再入场。
早盘现金账本卖清后结束旧批次的清仓状态，后续买点按原评分及仓位规则参与；
重新买入按新买入日期执行T+1，同一成交事件冲突仍退出优先。
这是L3内部规则修复，无新配置/API；研究身份沿源码哈希更新，历史冻结结果保持。

公共`TimePicker`可选`showSeconds`，默认时分调用不变；早盘两时间启用秒模式，
沿既有弹层容器、确认和Escape契约，保存字段不变。不得把界面秒精度误用为
连续资金回测支持秒撮合，见[最新整合](../reports/release-consolidation-dual-20261102.md)。

停牌延期/复牌退出为L3内部有界扩展，复用公共停复牌文件与持仓相对模板。
`StrategyDataContext.suspension_history`覆盖实际持仓而非当前候选集，旧缺字段兼容。
扩展必须保持原到期日开盘前已知停牌、公告时点及精确交易日因果顺序；
复牌信号不等于成交，收益账本使用公告后首个已完成分钟、真实容量及每日累计费用，
停牌估值携带最后真实价格日期，余股继续占用现金和仓位。没有新增Provider/API/schema，
S01/S02专用规则与额外风控限制保持；见[追加整合](../reports/release-consolidation-resume-20261102.md)。

停复牌内部文件兼容可选北京时间`announced_at`，不增加Provider或SQL接口；
Screener／分钟回放／盘前批次传递`observed_at`，同日仅消费已公开事件。
旧缺时间列保持保守语义，尾盘信号分母不能用日终公告改变；升级边界见
[线上全区间执行](../reports/custom-s02-full-range-online-20261102.md)。

本轮整合见[发布清单](../reports/release-consolidation-20261102-followup.md)。
独立历史停复牌文件属于L3内部数据契约，复用既有公共目录发布、Repository及
`pool_universe`证据传递，不新增Provider数据集、插件API或SQL迁移。
缺文件兼容未知，公告日与生效日分别限制，原失败快照及未勾选排除的股票范围保持。

策略日期是L3内部日期契约：既有行情状态增加可选北京时间日历字段，沿共享查询接入，不新增插槽。
交易日00:00后默认/上限/新监控使用今天，历史手选保持；最新日线日期只表示覆盖，不能覆盖时钟。

回测入口的持仓出场能力限制不能隐藏已配置引用：内部`SignalPicker`按signals保留已选项，
显示只读能力提示；通用分钟回测启用已支持模板，其他消费方保持原限制，不改变信号列ID和保存接口。
早盘收益账本为L3内部有界扩展，无新增公开插件API：复用`MinuteSignalReplayer`和
`morning_liquidation.evaluate`，输出原结果结构，实际持仓仅来自本次研究买入。
二开须保留事件开盘/收盘因果顺序、完整池、精确T+1和失败余股，不将该路径泛化为任意出场支持。

账户偏好保留是L3前端内部修复，复用`accountStorage`原API，不新增公开插槽或后端接口。
升级保持界面偏好白名单、user/tenant稳定键及旧auth_version迁移；未知键默认临时，
私有缓存、草稿和任务ID仍清理。不得通过保留全部localStorage取消账户隔离或恢复旧会话数据。

独立“卖／选／买”是自定义通用分钟策略的L3内部结果契约，不是新增公开插件API。
META `result_sections="signals"`由引擎启用，`select_rows`复用现有基础过滤及日线叠加，
原分钟路径保留触发记录；`section_mode`区分信号与原预置组合结果，
`section_errors`区分缺数据与成功零匹配。历史sell由共享存储事务补齐，
不在引擎读取租户库，也不以当天源/覆盖配置重演历史买点。

当日股票池盘前调度复用现有Screener/Compute及池归档，内部
`services/daily_pool_schedule.py`统一北京时间08:00/09:00批次。
盘前`premarket`上下文只读取精确前交易日价格/股本与当前目录；策略选股入口
仍沿保存的参考日配置。这是L3内部改动，无新增公开插件API；成功批次不随
中途配置修改重跑，09:00批次读取最新配置。每策略同日只保留最新成功池：
成功保存及旧池删除沿同一事务，失败不删旧池，迟到旧任务不写回；其他日期和任务记录保留。

通用分钟日线叠加现已复用`overlay.filter_minute_candidates`及原配置字段；
偏移0锚定精确前交易日，日线预热由`minute_daily_history_bars`动态规划。
涨跌幅评分按策略输出的真实分钟收盘价计算，前日价格采用raw_close。
本次为L3内部扩展，无新增公开插件API；专用执行不放宽。
通用分钟单一早盘清仓回测动态加载同一叠加依赖，其余回测路径按各自能力拒绝。

通用分钟扫描复用`TenantStrategyEngine.task`与现有持仓适配器，
直接任务和冻结执行快照均须传入所属策略的最小剩余批次；
冻结信号/覆盖不得被扫描时的新配置替换，多策略Compute上下文分开。
这是内部L3链路补齐，无新插件API；本地自定义s02转换见
[转换记录](../reports/custom-s02-minute-20261102.md)。

回测页旧选择校验属于L3前端局部修复，复用现有目录、详情查询键及账户作用域storage。
扩展时保持“最新成功目录→确认成员→读取详情/允许运行”的顺序，
成功空目录与请求失败不得混同；无新插件API，不自动恢复已删除策略或扩大执行能力。

Full策略源的持久记录键是稳定身份。旧正文缺少`id`或包含不同`id`时，
任务／监控快照从记录键填入，不改写原正文、revision或归档身份。
二开不能要求历史正文额外迁移，也不能用正文id覆盖所属记录键。

已有信号选择组件是策略设置局部扩展，复用现有Modal/查询缓存/保存回调，
不新增公开插件API。草稿、应用与持久化边界以及csg_/csgi_列名需保持兼容，
不能通过前端选择器绕过日线/分钟或专用执行限制。

早盘清仓属于现有信号模板和分钟引擎的局部扩展，不提供新的公开插件注册API。
新定义保留`time`键并增加`fallback_time/rise_pct/fall_pct`，旧振幅输入冻结兼容。
二开须保留现价相对权威原始昨收、分批持仓/T+1、兜底独立去重及真实分钟续卖，
不能用高低价差、买入盈亏、更早昨收或无限成交替代。最早信号与最终余股失败时间分离，
信号回放展示兜底不表示成交；历史研究快照不改写。契约及本地状态见
[双阈值双时间验收](../reports/morning-liquidation-dual-time-20261102.md)。
扩展时显式传入`positions`和`observed_at`，保留各剩余买入批次日期、
实时/完成分钟前缀/时钟来源和租户归属；不得把模板编译成普通无持仓布尔列。
日线、专用执行及无持仓监控的拒绝边界须与前端保持一致。
收益回测仅支持通用分钟A股连续资金、单一早盘出场，整分钟截止；
混合出场/额外风控/独立全量仍拒绝，缺数据或余股不能生成完整净值。

S02全天卖出为L3共享内核局部修改，不新增扩展接口。
升级须保留14:32前卖→14:32买→14:33后卖的顺序、T+1、午休排除、
当日累计卖额/费用日期和到期余股拒绝净值；前价/年龄用精确前交易日，
风险警示用选股当日元数据。显式旧参数和原S01兜底校验仍兼容。
内置S02参数分组属于前端展示契约，统一通过`layoutStrategyParams("s02", params)`
映射为尾盘选股、买入与仓位、次日卖出、费用与容量；不得为调整页面顺序改写后端
`PARAMS`，否则会无意义改变策略逻辑版本和历史结果身份。
2026-11-02候选未部署，验证与容量边界见
[研究记录与统一Excel口径](../reports/s02-full-day-20261102.md)。

内部L3新增`basic_filter.exclude_suspended`（历史缺省false），独立于数值开关，
只排除所选当日有证据的停牌。`strategy/daily_pool.py`在公式/评分/显示限制前构建
全局定义的当日股票池：股票范围与流通市值约束的完整集合，其余数值条件只用于策略选股。
流通市值无边界或数值禁用时为范围池；禁用不取消旧配置中的板块/ST范围。
新快照的`definition_version=scope_float_cap_v2`参与内容身份和每日任务签名，
旧快照缺字段仍可读，不能重新标注或覆盖历史。新增可选结果`daily_stock_pool/daily_stock_pool_error`，
保留旧结果缺字段兼容。持久化通过结果事务与专用池服务，Full需schema v7；
不据此宣称新增通用插件接口或跨策略交易状态API。
历史停复牌为L3内部数据输入，独立`instrument_status/suspensions.parquet`不改上市/ST历史。
字段为symbol、effective_date、announced_on、is_suspended、source_url；
Repository缓存，Screener与分钟回放沿既有`pool_universe`传递状态。
仅状态证据不扩充成员，价格仍来自真实K；最近已生效且信号日前已公告的事件才可消费，
同日缺精确公开时间保守延后。事件文件纳入研究指纹，新发布不改写旧任务快照，
没有新增公开插件API、服务消息或数据库迁移。

核对日期：2026-11-01。先完整阅读[贡献指南](../CONTRIBUTING.md)，
扩展点以第1章真实定义为准；当前源码/发布/运行身份见[版本基线](version-baseline.md)，
逐次升级与验证结果见[证据索引](../reports/README.md)。

## 当前核心兼容边界

策略页的卡片选择、加入池与新建只切换选择或池；开启监控时以共享行情状态
确认当前北京时间交易日，休市/日历未知时兼容最近数据日，规则成功保存才调用单策略执行。
不重建挂载／切日期／已有S02监控的页面自动补算；保留手动重载、参数保存重算、
日期查询键及迟到结果隔离。监控操作有同步门闩与失败反馈；此为L3前端事件修改，
不新增API、扩展插槽或后台调度契约。

策略设置复用`BasicFilterEditor`与`strategyBasicFilter`；添加数值字段需同时更新
模板注入、单位与前后端校验。配置中null表示取消限制，缺省继承；
新保存的`basic_filter.data_reference="previous_trade_day"`将七项数值条件按精确
上一交易日计算，未带该字段的历史配置保留原口径。新增`change_pct_min/max`使用
百分数数值（-50至50、最多1位小数）；行情`change_pct`仍为小数，执行时只转换一次。
`strategy/basic_filter.py`的参考值独立于当前行情、成交及估值价格；Polars、日线矩阵、
通用分钟回放与S02共享这一口径，不改写策略专属14:30条件。升级验证节后/周一、
缺昨日数据、禁用数值、原始股本与价格输入及历史快照兼容。此处不新增公共插件接口。
PATCH的basic_filter按字段合并，整段替换仍使用POST。详情可选
`definition_defaults`仅提供当前策略定义的原始可编辑快照，前端恢复到草稿后再保存，
不可重新引入“预览默认值即删除覆盖”的副作用。此项属于L3内部配置契约；
保留原参数`display_scale/unit`与专用分钟执行摘要，不声明新增插件接口。
详情数值编辑复用`BasicFilterEditor.alwaysEnabled`，无启用复选框或文字；
先兼容解析旧股票范围，再把非叠加策略数值草稿设为启用，恢复原始值同样处理。
保存才写入启用状态，模板开关、叠加策略原状态及历史结果兼容，不新增扩展接口。

s01的可选 `META.minute_execution="overnight_capacity_v1"` 属于内部L3执行契约，
保留 `minute_filter`；共享选股/卖出纯函数、实际持仓上下文与分钟组合撮合见
[s01接入](reference/s01.md)。升级须复核配置保存/失效、代码并列排序、T+1、
容量及汇总费用、缺数据和期限失败台账、模拟自动委托拒绝，以及新增结果字段缺省兼容。
不据此假设存在通用仓位/成本继承接口或新前端插槽。

以下为L3内部实现和升级约束，不代表新增的公共扩展API。

S02的`momentum_capacity_v1`连续回测须保留25日日线预热及原始价格/股本输入。
不使用的日线信号须同时从基础列和信号计算需求移除，不能只清计算需求或关闭
共享加载器校验。升级跑真实Parquet到专用执行器的回归，通用分钟/S01物化路径保持；
此处未新增API、Provider能力、前端插槽或迁移格式。

| 领域 | 升级时必须保留 |
| --- | --- |
| rustdx | 核心2.0.4、桥接0.3.0/API4；三组/物理端点/全局额度、共享许可、切站排空与精度世代、整窗参考核验；旧无参Fake注入兼容，详见[任务连接](rustdx-task-routing.md) |
| 历史采集 | 100证券/32自然日、准确缺口相交、嵌套排队/传输时限、坏页拒绝、证券隔离与取消；来源变化只完整重采一次，不复用旧页 |
| 任务结果 | 可选warning/remaining/skipped_symbols/gap_report，旧缺字段可读；路径指向运行目录CSV，不以成功认证历史完整 |
| 周K读取 | 复用`scan_parquet_compat/scan_daily_parquet`，覆盖缺列/多列两种分区顺序，不改回裸严格扫描 |
| 指数 | CORE四只/PAGE七只；`IndexKlinePeriod=1d|1w|1mo`，date标签/trade_date分开，省略start读完整本地历史，available_start_date可选，旧接口兼容 |
| 图表与修正 | `StockDailyKChart.externalData`、可用/可见根数分开、默认60根、周期重置、终态失效及5秒清理；`RepairDailyPanel scope=index`沿共享任务链 |
| 资产与数据源 | ETF退役、七项Provider路由；不把上游runner/逐笔/指标引擎映射为已实现能力，保留单位、复权及财务legacy字段 |
| 公共数据 | 新周期表同步只读视图、PublicPublisher、data_release、schema和清理；Platform先于消费者，用户配置和研究版本引用保持 |
| 财务空间 | 当前只计五张权威表的part.parquet，revision计financials/revisions/，旧storage字段兼容；快照/观察事件不可用统计修复改写 |

### SSE、设置与数据总览

`useQuoteStream`沿用原签名、查询失效、
告警及退避规则，内部`sharedQuoteStream`通过Web Locks和BroadcastChannel按
账户/租户/auth_version复用连接，取得锁后才换取短期票据。升级保留关闭持有页后
接管、共享游标换票恢复、账户切换释放与迟到响应隔离；缺少浏览器共享能力时沿用
单页连接，不宣称该降级路径消除了HTTP/1.1多页连接上限。没有新增后端API或扩展插槽。

死代码清理保留已注册路由、插槽、Provider、旧配置及API兼容；
`overview.invalidate_overview_cache`重导出与测试注入入口保留，见[清理依据](../reports/operations/project-cleanup-20261028/verification.md)。

Full公共版本绑定同步保留旧无`public_release`的股票快照注入；
带发布身份的真实快照仍须包含股票／指数两部分，缺项不能提交新绑定。
新绑定及在途租约由读端拥有，二开不能在巡检中关闭仍被请求持有的仓库。

系统设置简化为L3局部变更，复用`StrategyConcurrencyControl`、共享查询键、
原子preferences及Broker只读配置，不新增插槽或扩展API。配置整数范围1～999与
执行资源/部署上限分离，升级须同步API/Platform与Compute的配置读取校验。
策略内存和CPU总预算各为执行主机硬件的50%，独立模式与Broker共用
`strategy_resources.BUDGET_RATIO`；前端按资源响应的`budget_ratio`回显，
不硬编码百分比或改写已保存并发值。
旧`screener_auto_run`保留deprecated字段兼容，读取/写入均不能再启用；
二开不得重建页面挂载/切日期的自动计算effect，每策略仍由原手动监控规则API启停。
开源设置组件只显示仓库、最新版本号链接和发布时间；布局不再自动轮询或显示新版本提醒。

升级必须同时加载匹配的API/Platform/Worker/Compute及前端，
保留旧API省略字段、旧Parquet、SQL租户关系和用户已存配置。
本地修改不加版本，固定主线质量门产物后升级。

历史日历沿`market_time`既有版本化接口扩展2019–2026年，缺年仍返回未知并要求源证据。
研究快照不能跨日历身份复用；二开不得改成仅按工作日猜测，也不改写旧快照。

数据总览实时行情卡片使用`quotes`键，旧六周期
排序首次插入15分K后；已有quotes位置和显隐优先，不在读取时改写用户存储。
二开须保留实时行情独立开关/轮询查询与总开关的加载、保存、错误状态。
新增日K同步行复用`1d.enabled/time`和盘后管道，不创建新的后端任务或扩展API。
布局及兼容回归见[验收](testing.md#data-overview-layout)。
各类任务分组（含扩展数据）使用原生折叠控件，保留category键、实时更新和独立展开。
总开关仅移除可见文字，仍保留可访问名称和原保存契约。

自选查询挂载重读是关系兼容契约：持久缓存仅用于先显示内容，
不能因缓存尚在staleTime内而跳过服务端校验。升级保留`refetchOnMount: 'always'`
及新鲜旧缓存回归，避免快速刷新或旧客户端缓存掩盖已保存的策略关系。
本地补丁范围和实际运行验收见[记录](../reports/ui/local-watchlist-update-20261027/verification.md)。

### 周期查询与区间任务

六周期画像扩展复用 `services/kline_periods.py` 的表映射及周期配置，
Provider可选实现统一 `get_klines`，契约见[数据源协议](reference/integrations.md)。
已有1m/1d插件兼容；5m/10m/15m仅消费本地1m，1w仅消费本地日K，不要求Provider适配。
新增四表在仓库视图、画像schema/缓存、数据清除、发布清单及公共目录同步接线，
二开不能仅新增卡片或让同一数据混入原1分钟/日K分区。
用户配置仍走preferences及共享查询键，前端不绕过Provider直接取上游。

离线生成复用`KlineSyncConfig`及`/api/kline/sync_period`。
`syncKlinePeriod`仅传period时，5/10/15分钟和周K按本地基础分区区间调用`generate_period`；
1m/1d基础周期明确提交`start_date/end_date/mode`走共享区间任务，
使用`isLocalDerivedPeriod`区分本地派生周期；不得仅传period而误进入本地指标生成。
旧无范围日K API兼容作用于同一data_dir的`run_pipeline`，从本地日K/因子写入Enriched。
升级保留显式value/unit旧API调用、日K自动同步及LOCAL_DERIVED_PERIODS原定义，
不能因手工日K可本地生成而更改全局日K数据源路由。
须复核目标已有数据重算、零输出失败、分块幂等、取消发布和有界缓存换代；
没有新增Provider数据集、前端插槽或通用扩展接口。

个股六周期图表属于L3局部修改，复用共享详情、StockPanel和ECharts图表，不新增插槽。
`klineDaily`/`klineDailyQueryOptions`的可选周期缺省日K，查询键加入周期；
分钟响应保留完整`date`和`datetime`，非日K携带`freq`防止实时日K增量混入。
数量控制复用`visibleBars`，新增`zoomResetKey`与实际可见/可用根数回调；
二开升级须核对默认60根、50%增减、重复默认和鼠标缩放，不反转用户定义的方向。
`stock-preview.footer`的`view`仍为`daily | intraday`，其中`daily`表示所有K线模式；
现有apiVersion及旧不传周期的调用保持兼容。

K线区间获取为L3局部接线，数据总览复用`KlineSyncConfig`、`DatePicker`、
`/api/kline/sync_period`及周期服务，不新增插槽。
日期与时分控件复用`DatePicker`/`TimePicker`及`usePickerPopover`，保持行情首排样式；
扩展字段保留可清空、禁用、标签与日期上下界，弹窗内保持Portal焦点及Escape优先收起弹层。
不新增扩展注册接口或改动后端日期/时间格式。
持仓提醒的`LotExpiryPicker`为L3共用组件，新增批次与编辑股票均接
`/api/lots/expiry-options?base_date=YYYY-MM-DD`；域函数`strategy.lots.expiry_options`
返回`base_date/next_trading_day/five_trading_days`，后两项在日历未知时可为空。
快捷项排除基准当天，基准为买入日或北京时间今天；沿用`remind_date`保存，
手选日期可清空，加载失败或未知日历不覆盖原值。升级须保留该契约与共享查询键。
持仓提醒样式对齐为L3局部前端修改，复用`PageHeader/EmptyState/StockDataTable`，
不新增插槽、API、配置或schema。升级保留`lot.id`行键、批次/历史记录隔离及原监控失效键；
共享表格须置于统一横纵滚动容器内，以保留固定表头和超过100行时的虚拟化。
整批删除为L3内部接口 `DELETE /api/lots/batches/{batch_id}`，不是扩展插件API。
升级保留租户写事务/文件回滚、成员ID校验、两类托管规则清理和序号不复用；
前端沿 `ConfirmDialog` 与 `QK.lots/lotBatches/monitorRules` 更新，历史股票不传合成ID删除。
删除图标置于`PageHeader.right`新增批次后，保持自选图标按钮样式及选中批次上下文；
不新增独立信息/删除行，保留标签和按钮提示中的编号及可访问名称。
基础周期入口仅为1m/1d，`syncKlinePeriod`范围对象包含
`startDate/endDate/mode`；后端三项均必填，严格日期格式及闭区间校验先于建任务。
`run_now`新增keyword-only `override_end_date`，统一日K/因子窗口和范围清理；
显式范围不附带分钟采集或全库自愈删除，派生指标仍沿既有历史依赖重算。
升级保留旧缺结束日期到今天、后台六周期API、自动盘后流程及无新增字段的旧任务。
`/api/kline/repair_daily`仅保留兼容，并与`sync_period`复用`_start_kline_range_job`；
`run_repair_daily`的可选period缺省`1d`，旧日K调用保持兼容；
可选mode缺省`full`，`missing`接`services/kline_gaps.py`按证券/时间键补采。
升级保留公布/源证据日历、上市日期及已形成标签边界、100证券有界请求、
写锁内only_missing防覆盖、无缺口零请求及未补齐计数；Provider与能力解析必须在
本地缺口确认之后，不将字段异常视为缺记录。
日K遗漏仅用本地因子重算受影响证券，5/10/15分钟仅补完整本地基础桶，周K仅聚合本地日K，
不新增Provider数据集。非日K通过既有快照股票池与独立周期表，5/10/15分钟只读本地1分钟，
周K只读本地日K，其余沿Provider入口。
升级复核周期校验/能力路由、任务请求与结果、幂等写盘/缓存广播、取消边界及前端
对应日期/任务跟踪；不把零行或部分数据包装成完整修复。

本地派生分钟为L3局部修改，复用`aggregate_minute_period`及原`aggregate_ten_minute`
兼容入口；5/10/15分钟查询优先聚合本地1m，基础无结果时保留已有周期分区读取。
升级须保留raw/legacy_1m继承、完整桶遗漏补齐、只读日历缓存、无远程回退、
本地能力门控豁免及离线零输出反馈，实际验证见
[本地聚合记录](../reports/market-data/local-derived-minutes-20261026/verification.md)。未新增插槽或通用插件API。

专项目录/脚本路径采用[工程布局](reference/engineering.md#layout)；
按[生产工程](reference/engineering.md#engineering)形成开发闭环，并在
[变更映射](reference/engineering.md#trace)关联需求/方案/开发/测试/运营/运维。
现有插槽/注册器保持原契约，目标数据适配不能据文档示例虚构API。

### 策略归档、自选与共享组件

升级重点复核加载定义版本、运行快照、归档查询维度/热缓存revision、
单飞身份、Platform→Broker配置和受控弹窗接口；没有新增通用候选过滤插槽。
现有SQLite/PostgreSQL归档与原策略快照并存，清缓存不清历史，分钟缺日不回退。
二开调用共享Modal时使用size/fillHeight和portal容器，不再传旧overlayClassName。
每次扩展点或核心契约变更更新本说明，至少每月复核其真实定义与测试。

历史结果筛选/删除为L3局部变更，复用`StrategyResults`、screener API、集中查询键
和共享`ConfirmDialog`，不新增插槽。列表过滤在SQL分页前执行；
升级保留可选筛选参数及旧无筛选调用、租户业务写事务和显式单条删除边界。
归档新保存使用内部缓存标识配合revision，旧记录缺字段可读；
不能只清当前进程内存而忽略同ID删除后重建。前端删除刷新须包含对应日期的
“全部周期”查询，并取消该记录的旧明细请求。该候选不授权自动清理历史。

用户策略删除级联是另一个L3生命周期变更，沿原策略删除API，不新增插件或注册接口。
升级保留SQLite生命周期表/Full同名namespace、执行世代捕获和提交守卫，
全部历史身份清理、租户事务/文件失败补偿、研究任务lease撤销及前端旧查询取消。
同ID重建不能重置世代；世代不改变交易参数、历史指纹或选股买卖规则。
共享冻结输入和模拟交易账本不随策略删除；没有部署或追溯批量清理授权。

同日重复归档为`StrategyResults.save`内的L3修改，不新增API、schema或注册接口。
相同身份与日期下，单跑/批跑空字段等价，耗时与计算时间不参与重复判定；
真实股票/评分/信号/警告变化仍更新结果。升级保留重复结果的摘要更新、revision失效
及最新任务顺序检查，避免较早任务覆盖重复重跑确认的最新成功；旧归档继续兼容读取。

`strategy/targets.py`仅提供目标股票领域的
校验、动态成员解析和删除引用检查，不是通用CandidateFilter插槽。
升级复核 `config.load_override/list_overrides`、运行快照、Polars/矩阵、监控任务、
同租户保存/删除事务及CSV新组回滚；`target_symbols`只属于执行快照，
前端/二开不得将它作为用户可维护配置持久化。
新建策略源定义中的板块/多组选项同样校验所属账户，分组引用检查读取源默认值
与覆盖值合并后的有效范围；解除覆盖再恢复原始定义时重新保护分组。
Full复用`strategy_sources/strategy_overrides`的JSON及`watchlist_group_items`关系表，
无需增加SQL列；执行快照动态解析成员，历史结果保留当次成员。
自选清空复用`targets.ensure_clearable`，在文件锁或租户写事务内检查策略引用，
包括兼容的Cell全部清空入口；前端只根据`referenced_by`提示和禁用，不能替代后端校验。
旧文件重复证券读取时合并多组关系，“全部”维持一证券一条。监控启用入口重新加载
目标配置，各计算轮次仍动态解析；升级须保留这两处重读，不能固化启用时的成员列表。
管理员并发身份从认证权限构造任务，不采信页面请求字段；普通租户GET设置只读1。
前端卡片类型仅normal/large，历史mini/hidden在读取时回normal。
既有插槽/注册契约保持原定义，源码更新不代表运行环境已经升级。
管理分组CSV导出属于L3局部交互，位于`WatchlistGroups.tsx`，复用当前账户的
`api.watchlistList()`完整清单与`group_ids`成员关系，不增加插槽或后端API。
升级保留选中分组、默认仅代码单列、完整导出和卸载后的迟到响应保护；不能用
`watchlist.toolbar`的已筛选`symbols`替代完整成员，也不将股票列表当作完整用户备份。

CSV顺序修复属于L3局部修改：确认导入按候选数组过滤所选项；文件/租户`add_batch`
对首现去重输入倒序置顶，返回及持久化为输入正序。升级保留单股置顶、未涉及成员原序、
M:N关系、only_new、revision与共享查询失效。管理分组`onCreate`仅传名称/颜色，
移除文件参数及CSV控件；主页面批量导入继续可用，旧后端新组CSV接口保留兼容。
没有新增插槽、存储字段或扩展API，历史列表不自动重排。

`WatchlistEntry.strategy_ids`为可选字符串数组，
缺省为空；文件新增列表列，SQL复用`documents/watchlist_strategies`且完整业务导入保留关系。
关联更新走`PUT /api/watchlist/{symbol}/strategies`及当前租户可见公开策略校验；
批量添加、旧新组CSV、移组、置顶须保留关系列，证券删除和清空须在同一写事务内清理。
不能把股票的策略标记写入`basic_filter.watchlist_group_ids`或自动启动监控。
表格/卡片复用`WatchlistRelations`和共享Modal；目录独立全周期查询键
`QK.watchlistStrategyOptions()`不得与日K目录或加工后的选股目录共用缓存。
默认排序沿清单顺序，重置只清字段排序；页头导出是L3局部交互，`Watchlist.tsx`将
`selectedGroup`与组名传给`WatchlistExportButton`，读取完整`api.watchlistList()`后按
全部/未分组/具体组过滤，采用点击时的分类；不能使用插槽的已筛选symbols或行情行。
图标按钮保留可访问名称，复用代码单列CSV和账户/卸载保护，不新增API、查询键或插槽。
新增字段及路由不扩大已有插槽、注册或Provider契约。

官方目录、管理员默认空间和全局顶栏沿既有核心实现：
官方目录适配位于数据接入层，CRUD复用目录写锁与发布链；管理员复用注册预留/初始化
流程；页面复用PageHeader，弹层需避开工具栏滚动裁剪。升级复核对应历史证券状态、
租户权限/重试、共享缓存失效和前端交互测试。新增源码恢复工具只用于离线归档，
不提供在线替换或重启能力。调用链与实际证据见
[本次整合](../reports/engineering/production-baseline-20261025/verification.md)。

本文面向需要在当前仓库中二次开发的维护者、团队和 AI 编码代理。目标不是禁止修改源码，而是让新增页面、业务规则和定制逻辑尽量通过稳定边界接入，使后续合并上游版本时冲突更少、风险可验证。

修改任何代码前，仍须先完整阅读根目录的 [`CONTRIBUTING.md`](../CONTRIBUTING.md)。
金融数据口径、缓存、并发、数据源和测试要求以该文档为准；开始分支、上游基线、
当前 `main` 和生产版本的归类以[版本基线](version-baseline.md)为准。

Windows 启动预算属于 L1 配置，核心身份、
字节缓存、公共发布/回收、五档判定和收盘临时缓存属于 L3 聚焦修改。
复用 `PublicPublisher`、`PublicQuoteBridge`、`DepthService`、
`KlineRepository.update_provisional_enriched_asset` 和现有账户/查询接口；
不假设存在通用封板策略插槽、沙箱资源硬限制或新的租户迁移 API。
调用链、兼容与验证映射见[本次整合](../reports/engineering/local-consolidation-20261022/verification.md#local-contract-2026-10-22-本地变更整合与生命周期基线)。
原生 Windows 修改采用完整应用单机管理员空间，二开升级应备份本地身份文件
及整个业务目录；公共回收必须保留研究 input 与 metadata 中的版本引用。

## 1. 文档状态

本文同时描述现有能力和后续按需建设的代码扩展契约。两者不能混用：

| 状态 | 含义 |
| --- | --- |
| 已可用 | 当前仓库中已经存在，可以在确认调用链后直接复用 |
| 按需扩展 | 尚未实现；出现真实用例后才能增加，不能提前假设 API 存在 |

当前已可用的主要扩展能力：

- 自定义、AI 和叠加策略目录：`data/strategies/`。
- 数据源 Provider 与 `plugin.yaml` 机制：详见 [`plugin-development.md`](reference/integrations.md#plugin)。
- 扩展数据与声明式分析页面：适合不需要自定义 React 交互的页面。
- 前端源码扩展注册：`frontend/src/custom/<namespace>/extension.tsx`，支持静态页面、导航和已开放插槽。
- 后端源码扩展注册：`backend/app/custom/<module>.py`，支持 FastAPI 路由、启动钩子和通知格式化器。
- 当前前端插槽：`layout.navigation.extra`、`stock-preview.footer`、`watchlist.toolbar`。
- 当前后端继承点：`NotificationFormatter`。
- 因子平台与策略线的既有桥接（已实现，二开时直接复用、勿重复实现）：因子库一键生成单因子排名策略（`GenerateFactorStrategyDialog` → `custom_factor_*`）；策略触发器引用因子条件信号（`AddFactorSignalDialog`，`csg_f_*`）；自定义信号 AI 提示词含因子分组（`custom_signals_ai.py`）；策略回测因子归因（`strategy.py` 的 `factor_attribution`，覆盖 `meta.scoring` 非空的策略）。

尚未实现、只能在真实需求出现后增加的能力：

- 更多页面局部插槽。
- 候选过滤、评分、仓位、风控和回测成本等后端业务策略接口。
- 配置 schema 迁移注册表。

AI 在开始任务前必须通过代码搜索确认能力是否已经实现。找不到定义和测试时，应把示例视为设计规范，不得虚构导入路径或调用结果。

## 2. 二次开发分级

按升级风险从低到高选择实现方式：

| 级别 | 实现方式 | 适用场景 | 升级风险 |
| --- | --- | --- | --- |
| L1 | 配置、策略文件、扩展数据 | 已有契约能够完成需求 | 最低 |
| L2 | 前端插槽、路由注册；后端策略接口、注册替换 | 新页面、局部 UI、可替换业务规则 | 较低 |
| L3 | 直接修改核心源码 | 核心流程本身必须变化，现有扩展点无法表达 | 最高 |

选择原则：

1. 先确认现有功能能否复用，禁止平行实现第二套数据、策略、缓存或请求逻辑。
2. 只在存在真实二开需求的位置增加扩展点，不为未来可能出现的需求预埋通用框架。
3. 插槽或接口无法表达核心行为变化时，可以修改源码；必须缩小改动范围并补回归测试。
4. 不为了避开一次冲突复制完整页面、服务或引擎。复制会把一次显式冲突变成长期的隐式分叉。

## 3. 前端扩展规范

### 3.1 何时使用插槽

插槽适合在既有页面中增加局部内容：

- 操作按钮或工具栏命令。
- 筛选条件或表单字段。
- 表格列和详情面板。
- 个股详情、策略详情中的附加标签页。
- 设置页面中的独立配置区。

新增完整页面时应使用路由和导航注册，不要把整页塞入某个插槽。改变核心页面的数据流、状态模型或主要布局时，应直接修改核心代码并按 L3 管理。

### 3.2 当前插槽契约

核心页面通过已经实现的 `ExtensionSlot` 提供受控上下文：

```tsx
<ExtensionSlot
  name="stock-preview.footer"
  context={{
    apiVersion: 1,
    symbol,
    name,
    view,
  }}
/>
```

二开模块通过默认导出的注册清单接入，不修改核心文件：

```tsx
import type { FrontendExtension } from '@/extensions/types'

function NavigationExtra({ collapsed }: { collapsed: boolean; pathname: string }) {
  return collapsed ? null : <div>二开内容</div>
}

const extension: FrontendExtension = {
  id: 'company.navigation',
  apiVersion: 1,
  slots: [{
    name: 'layout.navigation.extra',
    id: 'company-summary',
    order: 100,
    component: NavigationExtra,
  }],
}

export default extension
```

插槽设计必须满足：

- `name` 和注册项 `id` 全局稳定、唯一。
- `context` 使用明确的 TypeScript 类型，并包含契约版本。
- 只暴露完成该插槽职责所需的数据和操作，不传递整个页面状态。
- 插槽通过公开回调修改状态，不直接访问父组件内部 store 或缓存。
- 单个扩展渲染失败应由错误边界隔离，并显示可定位的扩展 ID。
- 注册顺序确定，使用 `order` 后再按 `id` 排序，避免加载顺序导致界面漂移。
- 插槽内容必须遵守项目现有设计系统、响应式和可访问性要求。

弹窗直接复用当前 `frontend/src/components/Modal.tsx`，通过 `size` 选择
`sm/md/lg/xl`，对应480/720/960/1200px。标题、内容、页脚和关闭按钮使用
`modal-header/modal-body/modal-footer/modal-close`，不另写遮罩、定位或宽度。
`SettingsModal` 同样使用 `size`；长工作区可用 `fillHeight`，`maximized` 仅用于
用户主动最大化图表。portal、顶部距离、窄屏收缩、层级、ESC和焦点由公共组件处理，
嵌套弹窗也须复用它。升级时复核调用方尺寸和内容滚动，验证矩阵见
[`FR-UI-06` 弹窗验收](testing.md)。
弹窗里的日历、分组菜单等非模态浮层通过 `useModalPortalContainer` 取得挂载容器，
保持在当前面板的焦点范围内；页面上使用时仍挂到body。

当前开放：

```text
layout.navigation.extra
stock-preview.footer
watchlist.toolbar
```

各插槽 context 契约（均要求 `apiVersion: 1`，定义见 `frontend/src/extensions/types.ts` 的 `FrontendSlotContextMap`）：

- `layout.navigation.extra`：`{ collapsed, pathname }`，侧边栏导航底部。
- `stock-preview.footer`：`{ symbol, name, view }`，个股详情对话框底部（日K/分时图表下方）；`view` 为 `'daily' | 'intraday'`。适合个股附加面板：龙虎榜、资金流、外部研究链接等。
- `watchlist.toolbar`：`{ symbols, viewMode, selectedGroup, refresh }`，自选页工具栏末尾；`symbols` 为当前筛选视图中的标的，`refresh` 在扩展修改数据后调用以刷新自选增强数据。适合批量操作入口：自定义分析、导出、组合计算等。

新增插槽前必须有真实用例，并同时定义 context 类型、异常隔离和测试；不能只在类型表中预留名字。

### 3.3 当前路由与导航契约

完整页面通过注册表接入：

```tsx
const extension: FrontendExtension = {
  id: 'company.risk',
  apiVersion: 1,
  routes: [
    { id: 'company-risk', path: '/company/risk', component: CompanyRiskPage },
  ],
  navigation: [
    {
      id: 'company-risk',
      routeId: 'company-risk',
      label: '风险分析',
      icon: ShieldCheck,
      order: 500,
    },
  ],
}
```

路由注册与菜单注册已经解耦：页面可以存在但不显示在菜单中；菜单只能引用同一扩展内的已注册路由。首版只支持静态绝对路径。扩展路由不得覆盖核心或其他扩展路径，冲突时只禁用该扩展并输出明确错误。

新增核心页面时，须同时更新 `frontend/src/router.tsx` 的 `CORE_ROUTE_PATHS`，
包括隐藏页、兼容跳转及动态路由。`frontend/src/router.test.tsx` 从实际路由配置
核对保护范围，并验证冲突扩展的路由、导航和插槽全部移除，正常扩展继续可用。

完整前端模板位于 [`frontend/src/custom/_template/extension.tsx.example`](../frontend/src/custom/_template/extension.tsx.example)。

### 3.4 前端禁止事项

- 不直接在多个组件中拼接后端 URL，统一使用 `frontend/src/lib/api.ts` 或未来公开客户端。
- 不自行创建与现有 TanStack Query 重复的缓存；查询键仍由 `queryKeys.ts` 集中管理。
- 不用插槽绕过权限、数据口径或表单校验。
- 不通过 DOM 查询、全局事件或 monkey patch 修改核心组件。
- 不把整个核心页面复制到二开目录后长期独立维护。

## 4. 后端扩展规范

### 4.1 使用小粒度继承

后端允许二开类继承稳定、职责单一的抽象基类，再通过注册表或依赖注入接入。不要继承并覆盖大型编排服务。

当前已经实现的继承点：

- `NotificationFormatter`：在监控规则完成评估后统一调整通知文案，不改变事件结构和触发语义。

下列是可能适合的小粒度接口，但目前没有实现，不能直接导入：

- `CandidateFilter`：候选池过滤。
- `ScoringPolicy`：评分计算。
- `PositionSizingPolicy`：仓位计算。
- `RiskPolicy`：风险约束。
- `StrategyProvider`：策略发现。
- `MonitorConditionEvaluator`：自定义监控条件。
- `BacktestCostModel`：手续费和滑点模型。

不适合作为公共继承点的核心类：

- `StrategyEngine`。
- `BacktestEngine`。
- `ScreenerService`。
- `StrategyMonitorService`。
- `DataStore` 和仓库实现。
- FastAPI 主应用及生命周期函数。

这些类管理流程、缓存、并发或生命周期。覆盖其中的内部方法会让上游调整执行顺序后产生难以发现的语义错误。

### 4.2 当前基类与注册契约

后端模块从 `app.extensions` 导入稳定契约：

```python
from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    NotificationFormatContext,
    NotificationFormatter,
)

EXTENSION_ID = 'company.notice'
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


class CompanyNotificationFormatter(NotificationFormatter):
    def format_message(self, event: dict, context: NotificationFormatContext) -> str:
        return f"[公司规则] {event.get('message', '')}".strip()


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.register_notification_formatter(
        'company.notification',
        CompanyNotificationFormatter(),
    )
```

同一个 `setup` 可以通过 `registrar.include_router(router)` 注册独立 FastAPI 路由。核心路由冲突、重复 ID 或契约版本不匹配时，该扩展整体不注册，不留下半注册状态。

核心数据层初始化完成后，可选的 `startup(context: ExtensionContext)` 会收到数据目录和只读仓库协议。启动钩子失败只记录错误，不阻止主程序启动。

完整后端模板位于 [`backend/app/custom/_template.py.example`](../backend/app/custom/_template.py.example)。

### 4.3 后端契约要求

- 上下文优先使用不可变 `dataclass` 或 `Protocol`，不向扩展暴露整个 `app.state`。
- 抽象方法参数、返回值、单位、空值和异常行为必须有文档及契约测试。
- 注册 ID 全局唯一；重复注册默认拒绝，不允许静默覆盖官方实现。
- 默认实现必须存在。没有启用二开实现时，核心行为与当前版本一致。
- 单个可选实现加载失败时应禁用自身；金融结果无法可靠计算时必须 fail-closed。
- 注册表在启动完成后冻结，实时线程中不得动态替换实现。
- 破坏性契约变化必须提升 `api_version`，旧版本至少保留一个大版本的兼容期。

### 4.4 继承与组合的边界

继承只用于表达稳定的“是一种策略实现”关系。需要同时组合过滤、评分、通知等能力时，分别注册多个小实现，不创建拥有大量可选方法的万能基类。

优先组合的场景：

- 一个服务需要多个独立规则。
- 行为需要按资产类型或运行上下文选择。
- 扩展只需要装饰默认结果，而不是完全替换算法。
- 依赖缓存、仓库或通知服务，需要通过明确构造参数注入。

## 5. 直接修改源码

扩展点不是限制。核心流程必须变化时允许直接修改源码，但要把升级成本显式管理。

### 5.1 修改要求

- 一个提交只包含一个二开目的，不混入格式化、依赖升级和无关重构。
- 优先新增独立模块，再对核心入口做最小接线。
- 修改公共契约时同时更新后端模型、前端类型、调用方和测试。
- 修改数据写入时列出持久化、内存缓存、版本、SSE 和前端查询失效链路。
- 保留旧配置和旧数据读取能力；必须迁移时提供幂等迁移和回滚说明。
- 在 PR 描述中标记被修改的核心热点及未来合并上游时的复核点。

### 5.2 高冲突热点

以下文件集中管理启动、路由或公共契约，直接修改时需要重点复核：

```text
backend/app/main.py
backend/app/strategy/engine.py
backend/app/backtest/engine.py
frontend/src/router.tsx
frontend/src/components/Layout.tsx
frontend/src/lib/api.ts
frontend/src/lib/queryKeys.ts
```

高冲突不代表禁止修改，而是要求改动更小、测试更完整。若多个二开需求反复修改同一热点，应把共同接线能力提升为正式插槽或策略接口。

## 6. AI 开发工作流

AI 必须按以下顺序工作：

1. 完整阅读 `AGENTS.md`、`CONTRIBUTING.md` 和本文。
2. 检查 `git status`，保留工作区已有修改。
3. 搜索目标调用链、相邻实现、现有扩展点和测试。
4. 明确需求属于 L1、L2 还是 L3，并说明选择依据。
5. 判断目标插槽或后端基类是否真实存在，不依据本文示例虚构代码。
6. 写出最小改动计划和完成标准。
7. 先补能证明行为的测试，再实施必要改动。
8. 执行对应验证矩阵，检查最终 diff 和兼容性。

### 6.1 可直接使用的任务模板

```text
请在 TSP 当前仓库中实现：[具体需求]。

开始前完整阅读 AGENTS.md、CONTRIBUTING.md 和
docs/secondary-development.md，并先检查 git status、真实调用链和现有测试。

约束：
1. 先判断现有功能能否复用，并将方案归类为 L1/L2/L3。
2. 前端优先使用已经存在的受控插槽、路由或导航注册；后端优先使用已经存在的
   小粒度抽象基类和注册机制。必须用搜索和测试证明接口真实存在，不能根据设计文档
   虚构 API。
3. 若扩展点尚未实现，先说明最小可行方案；只有该需求确实需要时才新增扩展点。
4. 允许直接修改源码，但保持改动最小，不复制完整页面或核心服务。
5. 复用现有 API、数据仓库、缓存、查询键、组件和领域口径，不创建第二套逻辑。
6. 保持历史配置和数据兼容，扩展失败不能破坏未启用扩展的主流程。
7. 不覆盖已有修改，不提交、不推送，除非我单独确认。

完成后请列出：
- 方案分级及原因；
- 修改文件和关键契约；
- 对缓存、数据、API 和升级兼容性的影响；
- 实际执行的测试、构建和结果；
- 仍需人工确认的风险。
```

### 6.2 让 AI 设计扩展点的模板

```text
请只设计并评审以下二次开发需求的扩展边界，暂不修改代码：[具体需求]。

请基于当前仓库真实调用链回答：
1. 现有能力是否已经可以实现；
2. 前端应使用局部插槽、路由注册还是直接改源码；
3. 后端应使用哪个小粒度策略接口，为什么不继承大型核心服务；
4. 最小 context/Protocol 应包含哪些字段；
5. 默认实现、失败隔离、契约版本和测试如何设计；
6. 哪些抽象属于当前不需要的过度设计。

不要假设本文中的目标 API 已经实现，请给出代码证据和文件位置。
```

## 7. 验证矩阵

除 `CONTRIBUTING.md` 的通用要求外，二开还应按接入方式验证：

| 改动 | 最低验证 |
| --- | --- |
| 前端插槽 | 无注册、单注册、多注册排序、异常隔离、窄屏、前端构建 |
| 路由/导航注册 | 路径冲突、隐藏页面、无权限、直接刷新、未知路由 |
| 后端策略实现 | 默认实现、二开实现、重复 ID、版本不兼容、加载失败隔离 |
| 配置或契约变化 | 旧字段缺失、未知字段、更高版本拒写、迁移幂等 |
| 直接修改核心 | 受影响模块完整回归、缓存失效、历史配置、前后端联调 |

常用命令：

```bash
cd backend
uv run --frozen pytest tests/path/to/test_x.py -q
uv run --frozen ruff check app/path.py tests/path.py

cd ../frontend
pnpm build

cd ..
git diff --check
git status --short --branch
```

不得把“扩展已加载”当作业务验证。测试必须断言真实过滤结果、评分、路由输出、界面状态或失败隔离行为。

## 8. 版本与升级约定

- 二开分支应记录开始开发时的上游 Git Tag 或 commit，不能只写“基于 v0.x”。
- 正式发布使用不可变 Tag；二开升级优先合并 Tag，而不是持续变化的开发分支头。
- 公共插槽和后端扩展接口使用独立的 `api_version`，不要直接等同应用版本。
- 同一 `api_version` 内只做向后兼容的字段新增；删除、改名或改变语义必须提升主版本。
- 废弃字段先标记并保留兼容读取，至少跨一个大版本后再移除。
- 升级后必须重新运行二开契约测试，不能只依赖 Git 显示“无冲突”。

直接修改核心源码的二开分支可在升级前运行只读预检：

```bash
python3 scripts/upgrade_check.py <目标Tag或分支>
```

脚本不会执行 merge、修改索引或工作区。它会报告共同基线、双方修改的同一文件以及
Git 三方预演可识别的文本冲突。可附加 `--bundle .dbg/upgrade-review`，把已提交、
暂存、工作区差异和三方源文件备份到新目录；未提交内容仍不进入文本预演，
正式评估前应先提交到临时二开分支。

Docker、Compute、桌面产物的提交/文件清单及已安装产物差异备份，见
[构建清单与离线升级备份](reference/engineering.md#build)。生产代码通过重新构建发布
恢复；没有在线覆盖运行代码或自动重启入口。



## 9. 完成检查表

- [ ] 已确认需求属于 L1、L2 或 L3。
- [ ] 已证明使用的插槽、基类和注册 API 在当前代码中真实存在。
- [ ] 没有复制已有数据读取、缓存、API 客户端或完整核心页面。
- [ ] 前端扩展只获得必要 context，后端没有继承大型编排服务。
- [ ] 默认实现和未启用二开时的行为保持不变。
- [ ] 重复注册、加载失败、版本不兼容和空数据均有明确行为。
- [ ] 历史配置、策略和用户数据仍可读取。
- [ ] 已执行适用的测试、构建、Ruff 和 `git diff --check`。
- [ ] 最终说明包含升级风险和未来合并上游时的复核点。

## 10. 后续扩展原则

统一注册基础设施已经完成。后续只按真实业务需求增加能力：

1. 页面需要局部定制时，在真实位置增加一个类型化插槽及测试。
2. 后端业务规则需要替换时，从该调用链提取一个小粒度接口、默认实现和契约测试。
3. 不继承大型编排服务，不暴露整个 `app.state`，不复制核心流程。
4. 只有出现需要持久化的二开配置后，再增加 schema 迁移注册表。
5. 需要升级直接修改源码的分支时，使用 `scripts/upgrade_check.py` 预检，再执行真实合并和回归。

当前数据源扩展有七项独立路由；历史分钟与盘中落盘统一使用 `minute`。
设置页的四类展示由矩阵 `settings_groups` 定义：实时、K线、除权、财务。
K线选择一次保存日K与分钟K字段，候选须同时声明两数据集；七项内部矩阵和插件声明
继续供消费者判断能力。二开不能把界面类别 `kline` 当作新 Provider 数据集，
或因移除五档/公司行为卡片而删除其服务契约。
HTTP源编辑器的五组为实时/K线/除权/公司/财务，K线内保留daily/minute配置；
映射草稿仅在前端保留，保存时去除pending键。财务试拉复用metrics业务请求，
不引入新的财务因子计算契约。
旧 `full_minute` 声明仍可读取，但不自动转换为历史分钟能力；
旧 getter 为分钟路由的兼容别名，旧选源字段不再写入或参与能力增广。
升级时复核 `MinuteRefreshService` 与 `_resolve_minute_provider`、
可选 `get_intraday_batch` / `get_intraday_latest` 及缺能力不跨源回退。
公司行为必须复用
`data_providers/corporate_actions.py` 的事件校验与原台账，不能用复权系数代替现金账务。
扩展拉取复用[有界分页与两级配方](reference/market-data.md#pull)，源配置变化时拒绝旧结果。
AI 策略迭代、助手会话和秒级研究均已有专用接口，但不因此对 API Token 开放。
全部页面、能力和实际源码入口见[当前实现索引](architecture.md#capabilities)。

这种顺序遵循 KISS 和 YAGNI：先解决已经存在的升级冲突，不提前建设完整插件平台，也不阻止开发者在必要时直接修改源码。
