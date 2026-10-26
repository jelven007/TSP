# 当前主线与目标规划的证据索引

核对日期：2026-10-26。本批集成前冻结基线为
`4d944cdf66c02ec020c98ea961cb6bdc2b079056`，应用版本0.4.29。
完整变更与实际检查见[最新整合](kline-release-20261026/verification.md)；
最终主线版本由CI分配，生产状态另见[版本基线](../docs/version-baseline.md)。

本目录保留当前契约、目标决策、生产兼容或未完成签收仍需要的证据。
<a id="history"></a>
以下历史记录只证明其标注的版本/环境/窗口，不能作为当前生产或新目标通过记录。

| 保留材料 | 与当前主线或规划的关系 |
| --- | --- |
| [六周期K线发布整合](kline-release-20261026/verification.md) | 六周期修正、个股多周期/数量、缓存隔离、文档责任、本地/ECS核对及独立质量门 |
| [最新现状与发布](current-state-release-20261025/verification.md) | 153文件冻结、六类文档、全量验证、策略归档/并发、UI/调度、本地与线上版本更新 |
| [生产基线整合](production-baseline-20261025/verification.md) | 官方目录、管理员空间、顶栏/数据总览、示例目录、六类文档和源码恢复工具的本次验证 |
| [完整本地整合](production-integration-20261024/verification.md)、[五个历史恢复点](production-integration-20261024/version-retention.json) | 本轮全部变更、目录、生命周期映射、全量验证、自审与恢复口径 |
| [页面与数据分析](page-data-analysis-20261024/README.md) | 页面/菜单数量、数据血缘与上游分类；原根目录五份分析的可移植归档 |
| [目标设计输入](design-inputs-20261023/README.md) | 融合方案、ClickHouse八表/SQL和用户原图是统一目标的依据 |
| [系统目标整合](system-target-20261023/verification.md)、[二期架构分析](phase2-architecture-20261016/architecture-analysis.md) | 统一目标和二期3.1–3.6的来源与范围；保留其引用的[414cc483历史发布](deployment-20261015-414cc483/release.md) |
| [二期第一步](phase2-step1-20261016/implementation.md)、[第二步](phase2-step2-20261016/implementation.md) | 阶段实现、百用户证据和仍待完成的持续验收 |
| [单机实现](single-host/implementation.md)、[候选验证](single-host/verification.md)、[持续窗口](single-host/continuous-acceptance.md) | 现有七角色、隔离计算、故障恢复与未签收窗口 |
| [v0.4.8最终发布](online-upgrade-20261018/deployment-result.md) | 当前schema v6的升级/备份/回退兼容历史；配套原始JSON保留 |
| [v0.4.11最终发布](online-upgrade-20261020/deployment-result.md)、[历史回填](history-backfill-20261020/result.md) | 最后已知生产和实际日线覆盖边界 |
| [inode修复](online-timeout-20261021/result.md)、[连接池调查](online-timeout-20261021/watchlist-pool-investigation.md)、[延长验证](online-timeout-20261021/extended-validation.md) | 生产问题、待确认根因和未取得终态的窗口，不能因日期较早删除 |
| [22策略原始研究](default-strategies-five-year-20261017/analysis.md)、[策略质量升级](strategy-optimization-20261019/analysis.md)、[五策略实施](five-strategy-parallel-20261019/implementation.md) | 默认参数未被替换的研究依据、原价/费用/共享资金口径、候选与后续观察 |
| [主线整合](main-consolidation-20261018/verification.md)、[最新源码整合](local-consolidation-20261022/verification.md#全工作区变更整合与交付记录) | 当前主线的兼容与集成证据，及其引用的原修复结果 |
| [上游对比](upstream-comparison-20261017/analysis.md)、[移植结果](upstream-ports-20261018/verification.md) | 当前整合记录引用的取舍、已移植与保留边界 |
| [策略安全](strategy-safety-port-20261017/verification.md)、[正确性](p2-correctness-port-20261017/verification.md)、[其余P2与观察](p2-remaining-20261017/verification.md) | 安全/金融修复和未完成观察仍被主线整合引用 |
| [功能文档核对](documentation-sync-20261023/verification.md)、[PRD补齐](prd-completion-20261023/verification.md) | 当前页面、数据能力、需求与实现/测试的对应依据 |
| [生产体系整合](production-system-consolidation-20261023/verification.md) | 203个需求、目录迁移及本地适用回归的冻结证据；旧清单只代表当时文件状态 |
| [本次清理](repository-cleanup-20261024/verification.md) | 被替代材料、重复副本和缓存的移出清单、恢复信息及检查结果 |

无当前入口引用且已被现行规范/整合记录承接的旧独立回执已移出工作区。
这不改变历史测试结果，也不把现行说明当作旧测试原始证据。
清理前后均不删除生产备份、运行数据、凭据或Git历史。

新证据按专题与日期保存，记录完整提交/本地差异、版本、环境、数据窗口、命令、
结果和限制。截图随对应报告保存，不再建立缺少元数据的散图集合。

本轮正文消重、六类主手册与六份专题参考的迁移和验证见
[文档合并记录](document-consolidation-20261024/verification.md)。
原分散的一期实施、技术复核、因子/行情/rustdx验证集中在
[历史依据](document-consolidation-20261024/historical-evidence.md)，保留原日期/版本/计数，
复跑使用当前手册路径。
