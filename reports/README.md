# 项目验证与历史证据索引

- [2026-11-02双时间清仓与参数展示整合](release-consolidation-dual-20261102.md)：
  早盘独立涨跌幅、真实续卖、失败账本、公共秒控件和S02展示副本；
  候选新验证、主线发布、两端升级和最近两版恢复分别签收。

- [2026-11-02复牌退出追加整合](release-consolidation-resume-20261102.md)：保留准备期间
  新部署的停牌持仓/复牌卖出，补齐盘中公告因果撮合和新执行身份账本展示；
  独立开发目录、原失败研究与用户配置保持，主线质量门和双端升级分阶段签收。

2026-11-02 [尾盘走强6月29日历史停牌修复](../.claude/artifacts/fixes/late-session-suspension-20260629.md)：
缺价证券为陕鼓动力，补录官方停复牌事件；本地池1561→1560，上涨48.27%、买点0，
诊断消失。170项回归、实际Compute与本地匹配安装验证通过，策略/信号/账本保持；
未新增完整区间收益研究或同步线上，旧公共快照不改写。

- [2026-11-02后续补丁统一发布](release-consolidation-20261102-followup.md)：日期、账户偏好、
  完整池／调度、通用分钟信号及晨盘收益整合，分阶段记录CI、产物、两端升级与两版保留。

2026-11-02 [自定义s02早盘清仓收益撮合本地验收](../.claude/artifacts/fixes/custom-s02-morning-backtest.md)：
通用分钟连续资金＋单一早盘出场、已完成日K叠加、精确次交易日成交；
最终后端91项/前端16项、无网络Compute合成收益及分片/余股/滑点检查通过。
本地匹配产物、角色健康、配置/11组状态保留及桌面/手机刷新签收；
没有新的真实行情收益研究或线上部署，不替换下方历史回测结论。

2026-11-02 [自定义s02线上同步签收](custom-s02-online-20261102.md)：
线上已有尾盘走强/早盘清仓绑定、均额与评分保持；匹配运行产物和前端已生效。
本次线上扫描2026-10-30完整池1552、选165、买0，卖缺前日归档仍待确认；
后端267项、前端39项、真实Compute18项和桌面/手机验收通过，schema7及账本保持。

2026-11-02 [完整当日股票池运行部署签收](daily-stock-pool-runtime-20261102.md)：
本地/线上完整池定义及08:00/09:00最新保留规则生效；本轮59项相关回归通过，
两端真实08:00各25个策略成功，S02池3193只；本地停用账户调度租约阻塞已修复，
线上08:00后新增1项策略待09:00生成，最新并行产物保留修复。

2026-11-02 [当日股票池仅保留最新一份](daily-stock-pool-retention-20261102.md)：
本地和线上已生效；09:00保存成功后原子清除该策略当天旧池，失败保留08:00，
191项相关回归通过，两个定时批次继续独立去重。

2026-11-02 [当日股票池08:00／09:00调度](daily-stock-pools-schedule-20261102.md)：
本地和线上已生效；185项回归通过，两端各25个生效股票策略真实盘前取数成功。
正式整点与临时验证明确区分，版本保持0.4.42。

2026-11-02 [自定义s02成交额叠加及评分恢复](custom-s02-overlay-score-20261102.md)：
20日已完成日K均额≥1亿元，原涨跌幅100%高值权重；
分钟日线叠加与真实触发价评分已在本地恢复，信号、全池及交易设置保持。

2026-11-02 [本地自定义s02分钟转换](custom-s02-minute-20261102.md)：
现有ID原地更新为尾盘走强/早盘清仓，清除日线残留；
补齐直接及冻结扫描的持仓传输，保留内置s02、信号参数和交易配置。

2026-11-02 [线上回测页失效策略选择修复](unknown-strategy-20261102.md)：
等待最新目录后才取详情，清理已删除旧选择并保留通用输入；
v0.4.42最小静态补丁已部署，双库恢复、七角色及六项浏览器场景通过，
102文件/537项前端验证通过，未运行新的收益回测。

2026-11-02 [本地最新组合与统一发布](release-consolidation-20261102.md)：股票池v7、股票范围、手动监控、S02全天、信号模板／选择及双环境升级门禁；当前为候选验证，最终身份和部署回执另行核验。

2026-11-02 [策略设置直接选择已有自定义信号](existing-signals-20261102.md)：
入场/出场搜索多选、草稿应用、真实保存回显、桌面/手机与本地验证。

2026-11-02 [早盘清仓自定义出场信号](morning-liquidation-20261102.md)：
次交易日振幅阈值/截止时钟、持仓分日期、实时/历史信号及验证状态。

2026-11-02 [尾盘走强自定义入场信号](late-session-strength-20261102.md)：
五项可配置条件、完整当日池上涨占比及完成态双周期MA5；
141项后端、24项前端与真实Compute/本地页面重启核验通过，已在本地管理员账户创建。
应用版本未递增，未部署线上，未运行新历史收益回测。

2026-11-02 [S02新增筛选与次交易日全天卖出](s02-full-day-20261102.md)：
统一Excel收录本次92日五资金、逐月、滑点、费用、阈值归因及折叠全账本。
10万元收益18.28%→14.92%；100万元三档滑点于8月11日收盘容量不足而失败，
不发布完整区间值。源码候选与标准服务核对完成，未安装/部署，不覆盖旧每日基准。

2026-11-01 [本地整合、发布与两端升级](release-consolidation-20261101.md)：
本次统一源码、六类文档、临时清理及两版恢复；各阶段真实结果独立记录。

2026-11-01 [S02回测依赖修复](../.claude/artifacts/fixes/s02-backtest-panel-dependencies.md)：
仅修正专用日线计划，补真实Parquet加载和成交/失败边界回归；
后续本地API／Compute已生效，并补入公开停复牌证据后完成59日10万元验证；
五资金历史未重跑，具体结果和边界见修复记录及本次整合记录。

2026-11-01 [s02标识更新及退役验收](s02-migration-20261101.md)：
当前工作区与本地运行统一为s02，保留原15份研究任务和92份每日结果；
退役策略记录、相关副本及旧前端已清理，规则与收益沿用原执行，未上线。

2026-11-01 [策略系列清理验证](strategy-removal-20261101.md)：
对应源码、设置、结果和专属报告清理；其余22个策略与独立s01研究保留。

2026-11-01 [s01恢复盈利版本](s01-current/README.md)：当前研究入口恢复为
10%分钟容量、14:47～14:49分片买入、11:15/11:22早盘信号和13:30截止。
源码与2026-10-31干净重跑盈利版一致；已有五档收益6.24%～8.09%，本次未新跑全区间。
恢复配置与定向回归证据单独保存，安装环境未升级。

核对日期：2026-11-01。当前源码、已发布产物和最后已知运行状态统一见
[版本基线](../docs/version-baseline.md)。报告只证明其记录的提交、环境和窗口；
本次文档整理未重新探测生产，也不把旧测试计数计为本次结果。

2026-10-31安全复核见[文档脱敏与公开暴露处置记录](document-security-20261031.md)。
涉及的本地文档、证据和分享包已脱敏；原有校验值仍描述原始历史证据，
脱敏副本使用本次清单中的新校验值。公开主线、历史版本及冻结附件的剩余暴露单独列出。

2026-11-01 [ECS身份核验及历史镜像/缓存清理](ecs-historical-image-cache-cleanup-20261101.md)：
删除28个闲置镜像，构建缓存回读为0，实测可用空间增加约38.13 GiB；
当前/回滚镜像、容器、卷及受控配置保持，既有API就绪波动另行记录。
前一阶段的GHCR、Actions及本地处置见
[历史镜像与缓存记录](historical-image-cache-cleanup-20261031.md)。

2026-11-01 [API间歇就绪异常根因调查](api-readiness-root-cause-20261101.md)：
确认3个API进程仍绑定已由定时维护删除的旧公共发布，60次采样中45次503；
确定性复现3/3，调查时现有相关测试79项通过；该计数只属于原调查。

对应[闲置API发布绑定修复方案](api-public-release-binding-plan-20261101.md)已形成：
每进程巡检、仅发布变化时更新、共享读锁保护旧仓库、回收排他守卫及一致就绪读取；
方案阶段只生成设计；用户批准后的[实施记录](api-public-release-binding-implementation-20261101.md)
单独保存源码、反证、本地和Linux证据，代表性候选及生产6小时验收仍待完成。

## 最新发布与当前数据边界

| 材料 | 用途与边界 |
| --- | --- |
| [v0.4.39部署](current-release-20261030/deployment.md)、[验证](current-release-20261030/verification.md)、[评审](current-release-20261030/review.md) | PR #35、main自动版本、固定产物、本地/ECS切换、恢复和正常重启；当前正式提交为bac72d9a83c03890cbb954e0e13782b8421d6073 |
| [财务存储统计](financial-storage-20261030/verification.md) | 当前五表与历史快照分开显示；源码修改和独立修复产物有各自身份，不自动归为正式发布内容 |
| [财务历史并集](financial-history-unify-20261030/verification.md) | 原快照/观察事件保留、两端并集与公共副本逐文件校验、A→B→A回放；原记录148文件/74,104,316字节仅代表该验收时点 |
| [分钟采集可靠性](minute-collection-reliability-20261029/deployment.md) | v0.4.38历史升级、队列预算、准确缺口、逐证券隔离、CSV与双库恢复证据 |
| [301110.SZ缺口](minute-301110-20261029/verification.md)、[隔离与补采](minute-301110-20261029/skip-and-backfill.md) | 源端历史深度、真实剩余键与备用接口限制；任务成功不代表全历史补齐 |
| [指数一致性](index-parity-20261030/verification.md) | 专项数据/环境对照，以报告内实际窗口和限制为准 |
| [三组连接实施](rustdx-task-affinity-20261029/verification.md) | 组/端点/全局额度、切站、完整来源核验；已由v0.4.39承接，初始源码报告保留原状态 |
| [rustdx业务实测](rustdx-business-20261029/verification.md) | 固定证券池、跨端模块身份、真实请求和写盘；上游固定tag来源与本地运行证据分开 |
| [历史策略交付重置](strategy-results-cleanup-20261030/历史交付重置.md)、研究起点（附件保存在受控归档） | 历史已清理研究/002交付移出，当前新增研究与结果索引归零；预置22个策略保持完整 |

## 功能与工程依据

| 专题 | 保留原因 |
| --- | --- |
| [统一发布与ETF退役](complete-release-20261028-3/review.md) | 股票/固定七指数范围、迁移门槛、指数保护、源身份和完整质量门 |
| [数据采集统一](data-collection-unification-20261028/verification.md)、[周K渐进schema](weekly-parquet-schema-20261028/verification.md) | 闭区间、本地缺口优先、读兼容与真实固定样本 |
| [指数三周期](indices-kline-20261028/verification.md)、[指数交互](index-page-ui-20261028/verification.md) | 独立周/月表、遗漏保护、权限/任务、根数控制和浏览器验证 |
| [历史分页性能](minute-history-performance-20261028/verification.md)、[状态缓存](data-status-timeout-20261028/verification.md) | 同环境请求/内容基准、缓存局部世代和失效边界 |
| [项目精简](project-cleanup-20261028/verification.md) | 已删除实现、依赖与冻结构建兼容的逐项依据 |
| [六周期发布](kline-release-20261026/verification.md)、[本地派生](local-derived-minutes-20261026/verification.md) | 周期表、查询、任务、价格基准、本地计算与取消契约 |
| [自选运行](local-watchlist-update-20261027/verification.md)、[数据布局](data-overview-layout-20261027/verification.md) | 关系/CSV/缓存挂载、共享开关与旧设置兼容 |
| [最新功能整合](latest-release-20261027/verification.md)、[归档与调度](current-state-release-20261025/verification.md) | 持久结果、全局准入、共享弹窗、目录/行情分离与各阶段验收 |
| [生产工程整合](production-system-consolidation-20261023/verification.md) | 原需求编号、目录迁移、生命周期与冻结清单；requirement-ids.json仍为文档回归输入 |
| [工程归并](production-integration-20261024/verification.md)、[生产基线](production-baseline-20261025/verification.md) | 初始化/认证/SMTP、目录/菜单/示例、恢复点与完整环境兼容 |
| [文档合并](document-consolidation-20261024/verification.md)、[原始依据](document-consolidation-20261024/historical-evidence.md) | 历史实施与目录迁移结论，原日期/版本/计数保持 |
| [页面数据分析](page-data-analysis-20261024/README.md)、[功能核对](documentation-sync-20261023/verification.md)、[PRD补齐](prd-completion-20261023/verification.md) | 历史页面、数据血缘和需求依据；旧ETF材料按退役前时点阅读 |

<a id="history"></a>
## 规划、未完成调查与历史兼容

| 材料 | 保留原因与状态 |
| --- | --- |
| [系统目标](system-target-20261023/verification.md)、[设计输入](design-inputs-20261023/README.md)、[二期分析](phase2-architecture-20261016/architecture-analysis.md) | 目标组件、ClickHouse设计与原图；万人/分布式容量未签收 |
| [第一步](phase2-step1-20261016/implementation.md)、[第二步](phase2-step2-20261016/implementation.md)、[单机持续窗口](single-host/continuous-acceptance.md) | 已有阶段实施及百用户证据，持续窗口/故障恢复仍按各自状态判断 |
| [inode修复](online-timeout-20261021/result.md)、[业务池调查](online-timeout-20261021/watchlist-pool-investigation.md)、[延长观察](online-timeout-20261021/extended-validation.md) | 旧实例故障与未确认根因，不能因新实例健康或日期较早删除 |
| [v0.4.8](online-upgrade-20261018/deployment-result.md)、[v0.4.11](online-upgrade-20261020/deployment-result.md)、[回填](history-backfill-20261020/result.md) | schema与恢复兼容历史，历史行情覆盖不适用于新实例 |
| [四站32完整性试验](rustdx-four32-integrity-repair-20261029/report.md)、[流水线试验](rustdx-four32-pipeline-20261029/report.md) | 未采用的独立方案、来源差异与性能成本；不作为当前三组实现或部署证据 |
| [上游比较](upstream-comparison-20261017/analysis.md)、[移植](upstream-ports-20261018/verification.md)、[主线归并](main-consolidation-20261018/verification.md) | 历史取舍及当前仍引用的修复依据 |
| [策略安全](strategy-safety-port-20261017/verification.md)、[金融正确性](p2-correctness-port-20261017/verification.md)、[P2观察](p2-remaining-20261017/verification.md) | 安全、金融边界与未完成观察 |
| [仓库清理](repository-cleanup-20261024/verification.md) | 旧副本/缓存移出范围与历史检查结果 |
| [旧版本交付清理](deliverables-version-cleanup-20261030/verification.md)、[历史保留规则更新](deliverables-version-cleanup-20261030/retention-two-prior.md)、当前恢复集合（附件保存在受控归档） | 原阶段884个旧交付文件移出；随后三版上限是历史快照，2026-10-31已调整为v0.4.38、v0.4.39共两版，归档及原始清单存入本机受控目录 |
| [文档工程更新与清理](documentation-refresh-20261030/verification.md) | 当前文档入口、历史流水消重、85项垃圾文件清单及本次实际验证 |
| [本地三项目数据清理](local-data-cleanup-20261031/verification.md) | TSP分享/测试缓存、rustdx可重建打包副本及AIGC旧探测材料；逐文件删除依据和实际核验 |
| [历史镜像与缓存清理](image-cache-cleanup-20261031/verification.md) | 已完成下载用途的中转镜像、闲置构建/包缓存及VM空闲块回收；当前与前两个版本运行镜像保持 |
| [本地历史版本两版保留](version-retention-20261031.md) | 当前和一个历史版本共两版；清理v0.4.37镜像/副本、旧导出包及重复源码，唯一差异和数据备份保持 |

## 保存与清理规则

新报告按“专题-日期”归档，记录提交/未提交差异、环境、窗口、命令、实际结果、
失败/跳过、限制和必要的恢复位置。截图与机器结果随对应报告保存，
当前手册只维护现行规则并链接证据。

不按日期或体积直接删除报告。未提交工作、当前发布原始证据、需求检查基准、
用户策略/结果、缺口清单、未完成调查和必要恢复点均保留。
无有效引用的重复全文、已完成下载的分片、可重建镜像层和系统元数据可按
[维护规则](../docs/maintenance.md)清理并记录准确范围。
历史hash/文件清单描述原采集时点，不是当前目录清单；清理副本须注明并修正入口，
不篡改原测试计数、失败或验收结论。
