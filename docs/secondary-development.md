# 代码二次开发与 AI 扩展指南

2026-10-27整合升级必须同时加载匹配的API/Platform/Worker/Compute及前端，
保留旧API省略字段、旧Parquet、SQL租户关系和用户已存配置。
本地修改不加版本；固定主线质量门产物后升级，见[本批记录](../reports/latest-release-20261027/verification.md)。

历史日历沿`market_time`既有版本化接口扩展2019–2026年，缺年仍返回未知并要求源证据。
研究快照不能跨日历身份复用；二开不得改成仅按工作日猜测，也不改写旧快照。

2026-10-27数据总览是L3局部布局变更：实时行情卡片使用`quotes`键，旧六周期
排序首次插入15分K后；已有quotes位置和显隐优先，不在读取时改写用户存储。
二开须保留实时行情独立开关/轮询查询与总开关的加载、保存、错误状态。
新增日K同步行复用`1d.enabled/time`和盘后管道，不创建新的后端任务或扩展API。
布局及兼容回归见[验收](testing.md#data-overview-layout)。

2026-10-27自选查询挂载重读是关系兼容契约：持久缓存仅用于先显示内容，
不能因缓存尚在staleTime内而跳过服务端校验。升级保留`refetchOnMount: 'always'`
及新鲜旧缓存回归，避免快速刷新或旧客户端缓存掩盖已保存的策略关系。
本地补丁范围和实际运行验收见[记录](../reports/local-watchlist-update-20261027/verification.md)。

2026-10-26当前源树与部署核对见[本批整合](../reports/repair-missing-release-20261026/verification.md)。
本批L3改动继续使用现有Provider、归档仓库、租户事务、查询键和共享Modal，
不新增通用扩展API。合并与升级须复核API/Worker/Compute的周期和cache_token契约，
历史配置/分区/schema保持兼容；实际版本以[版本基线](version-baseline.md)为准。

六周期画像扩展复用 `services/kline_periods.py` 的表映射及周期配置，
Provider可选实现统一 `get_klines`，契约见[数据源协议](reference/integrations.md)。
已有1m/1d插件兼容；5m/10m/15m仅消费本地1m，1w仅消费本地日K，不要求Provider适配。
新增四表在仓库视图、画像schema/缓存、数据清除、发布清单及公共目录同步接线，
二开不能仅新增卡片或让同一数据混入原1分钟/日K分区。
用户配置仍走preferences及共享查询键，前端不绕过Provider直接取上游。

2026-10-27离线生成属于L3局部变更，复用`KlineSyncConfig`及`/api/kline/sync_period`。
`syncKlinePeriod`仅传period时，5/10/15分钟和周K按本地基础分区区间调用`generate_period`；
日K使用作用于同一data_dir的`run_pipeline`，从本地日K/因子写入Enriched。
升级保留显式value/unit旧调用、1分钟历史获取、日K自动同步及LOCAL_DERIVED_PERIODS原定义，
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

K线修正为L3局部接线，复用现有`RepairDailyPanel`、修正API及周期服务，不新增插槽。
2026-10-27前端入口仅选1m/1d，`api.repairDaily`新增第4可选endDate参数；
后端可选`end_date`同时限定full/missing，严格日期格式及闭区间校验先于建任务。
`run_now`新增keyword-only `override_end_date`，统一日K/因子窗口和范围清理；
显式范围不附带分钟采集或全库自愈删除，派生指标仍沿既有历史依赖重算。
升级保留旧缺结束日期到今天、后台六周期API、自动盘后流程及无新增字段的旧任务。
`/api/kline/repair_daily`与`run_repair_daily`的可选period缺省`1d`，旧日K调用保持兼容；
可选mode缺省`full`，`missing`接`services/kline_gaps.py`按证券/时间键补采。
升级保留公布/源证据日历、上市日期及已形成标签边界、100证券有界请求、
写锁内only_missing防覆盖、无缺口零请求及未补齐计数；不将字段异常视为缺记录。
日K遗漏仅用本地因子重算受影响证券，5/10/15分钟仅补完整本地基础桶，周K仅聚合本地日K，
不新增Provider数据集。非日K通过既有快照股票池与独立周期表，5/10/15分钟只读本地1分钟，
周K只读本地日K，其余沿Provider入口。
升级复核周期校验/能力路由、任务请求与结果、幂等写盘/缓存广播、取消边界及前端
对应日期/任务跟踪；不把零行或部分数据包装成完整修复。

本地派生分钟为L3局部修改，复用`aggregate_minute_period`及原`aggregate_ten_minute`
兼容入口；5/10/15分钟查询优先聚合本地1m，基础无结果时保留已有周期分区读取。
升级须保留raw/legacy_1m继承、完整桶遗漏补齐、只读日历缓存、无远程回退、
本地能力门控豁免及离线零输出反馈，实际验证见
[本地聚合记录](../reports/local-derived-minutes-20261026/verification.md)。未新增插槽或通用插件API。

专项目录/脚本路径采用[工程布局](reference/engineering.md#layout)；
按[生产工程](reference/engineering.md#engineering)形成开发闭环，并在
[变更映射](reference/engineering.md#trace)关联需求/方案/开发/测试/运营/运维。
现有插槽/注册器保持原契约，目标数据适配不能据文档示例虚构API。

2026-10-25本批L3变更为策略结果生命周期、全局并发预算、共享Modal及调度分离，
范围见[最新记录](../reports/current-state-release-20261025/verification.md)。
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

同日重复归档为`StrategyResults.save`内的L3修改，不新增API、schema或注册接口。
相同身份与日期下，单跑/批跑空字段等价，耗时与计算时间不参与重复判定；
真实股票/评分/信号/警告变化仍更新结果。升级保留重复结果的摘要更新、revision失效
及最新任务顺序检查，避免较早任务覆盖重复重跑确认的最新成功；旧归档继续兼容读取。

目标股票/分组CSV/角色并发候选是L3聚焦修改。`strategy/targets.py`仅提供该领域的
校验、动态成员解析和删除引用检查，不是通用CandidateFilter插槽。
升级复核 `config.load_override/list_overrides`、运行快照、Polars/矩阵、监控任务、
同租户保存/删除事务及CSV新组回滚；`target_symbols`只属于执行快照，
前端/二开不得将它作为用户可维护配置持久化。
自选清空复用`targets.ensure_clearable`，在文件锁或租户写事务内检查策略引用，
包括兼容的Cell全部清空入口；前端只根据`referenced_by`提示和禁用，不能替代后端校验。
旧文件重复证券读取时合并多组关系，“全部”维持一证券一条。监控启用入口重新加载
目标配置，各计算轮次仍动态解析；升级须保留这两处重读，不能固化启用时的成员列表。
管理员并发身份从认证权限构造任务，不采信页面请求字段；普通租户GET设置只读1。
前端卡片类型仅normal/large，历史mini/hidden在读取时回normal。
当前候选未改变应用版本或生产部署，既有插槽/注册契约保持原定义。
管理分组CSV导出属于L3局部交互，位于`WatchlistGroups.tsx`，复用当前账户的
`api.watchlistList()`完整清单与`group_ids`成员关系，不增加插槽或后端API。
升级保留选中分组、默认仅代码单列、完整导出和卸载后的迟到响应保护；不能用
`watchlist.toolbar`的已筛选`symbols`替代完整成员，也不将股票列表当作完整用户备份。

CSV顺序修复属于L3局部修改：确认导入按候选数组过滤所选项；文件/租户`add_batch`
对首现去重输入倒序置顶，返回及持久化为输入正序。升级保留单股置顶、未涉及成员原序、
M:N关系、only_new、revision与共享查询失效。管理分组`onCreate`仅传名称/颜色，
移除文件参数及CSV控件；主页面批量导入继续可用，旧后端新组CSV接口保留兼容。
没有新增插槽、存储字段或扩展API，历史列表不自动重排。

2026-10-27自选关系候选是L3局部变更：`WatchlistEntry.strategy_ids`为可选字符串数组，
缺省为空；文件新增列表列，SQL复用`documents/watchlist_strategies`且完整业务导入保留关系。
关联更新走`PUT /api/watchlist/{symbol}/strategies`及当前租户可见公开策略校验；
批量添加、旧新组CSV、移组、置顶须保留关系列，证券删除和清空须在同一写事务内清理。
不能把股票的策略标记写入`basic_filter.watchlist_group_ids`或自动启动监控。
表格/卡片复用`WatchlistRelations`和共享Modal；目录独立全周期查询键
`QK.watchlistStrategyOptions()`不得与日K目录或加工后的选股目录共用缓存。
默认排序沿清单顺序，重置只清字段排序；全量导出读取完整`api.watchlistList()`，
不能使用插槽的已筛选symbols。新增字段及路由不扩大已有插槽、注册或Provider契约。

2026-10-25整合中的官方目录、管理员默认空间和全局顶栏属于L3核心修改：
官方目录适配位于数据接入层，CRUD复用目录写锁与发布链；管理员复用注册预留/初始化
流程；页面复用PageHeader，弹层需避开工具栏滚动裁剪。升级复核对应历史证券状态、
租户权限/重试、共享缓存失效和前端交互测试。新增源码恢复工具只用于离线归档，
不提供在线替换或重启能力。调用链与实际证据见
[本次整合](../reports/production-baseline-20261025/verification.md)。

本文面向需要在当前仓库中二次开发的维护者、团队和 AI 编码代理。目标不是禁止修改源码，而是让新增页面、业务规则和定制逻辑尽量通过稳定边界接入，使后续合并上游版本时冲突更少、风险可验证。

修改任何代码前，仍须先完整阅读根目录的 [`CONTRIBUTING.md`](../CONTRIBUTING.md)。
金融数据口径、缓存、并发、数据源和测试要求以该文档为准；开始分支、上游基线、
当前 `main` 和生产版本的归类以[版本基线](version-baseline.md)为准。

2026-10-22 整合沿用现有扩展边界：Windows 启动预算属于 L1 配置，核心身份、
字节缓存、公共发布/回收、五档判定和收盘临时缓存属于 L3 聚焦修改。
复用 `PublicPublisher`、`PublicQuoteBridge`、`DepthService`、
`KlineRepository.update_provisional_enriched_asset` 和现有账户/查询接口；
不假设存在通用封板策略插槽、沙箱资源硬限制或新的租户迁移 API。
调用链、兼容与验证映射见[本次整合](../reports/local-consolidation-20261022/verification.md#local-contract-2026-10-22-本地变更整合与生命周期基线)。
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
