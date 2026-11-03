# 提交与合并复审

Review anchors：`backend/app/services/strategy_results.py:210`、
`backend/app/api/screener.py:390`、`frontend/src/pages/Screener.tsx:176`、
`backend/app/multitenancy/compute_broker.py:72`。

结论：可合并；未发现阻断问题。复审范围为基于7823b84b的完整暂存差异，
包含用户已有153文件及本批文档/测试夹具修正。用户要求整合最新现状并完整发布，
本批不拆掉其已有功能；不手工递增应用版本。

## Intent / scope / contracts

意图：把策略结果持久化、全局策略预算、调度/向导/共享弹窗和研究界面纳入
最新主线，同时更新六类生命周期文档。差异与该整合范围一致。
配置、依赖锁、数据库schema均无破坏性迁移；新增结果namespace复用既有租户事务，
独立运行使用SQLite。cached新增可选日期/资产/周期；无日期保持原快照语义。
共享Modal移除旧尺寸覆盖参数，仓库全部调用方改为size/fillHeight/portal。

## Behavior verification

| 行为 | 证据与失败控制 |
| --- | --- |
| 日期/名称/版本/参数/资产/周期隔离、重启恢复、空成功与旧任务晚完成 | `test_strategy_result_history`、`test_screener_history_api`，旧单日缓存不能满足这些断言 |
| 租户隔离、配置列表上限、回滚后热缓存 | 真实PostgreSQL的`test_full_strategy_history`；外租户返回空，无权拒绝，回滚后不可命中 |
| 分钟缺目标日拒绝 | `test_minute_strategy`；旧回退逻辑会错用最新日期 |
| 动态预算/旧任务释放、资源不可用和配置输入 | `test_strategy_concurrency`及`test_full_compute`；包括预算缩小时旧分配继续占用 |
| 目录独立维护、初始化等待、调度保存和休市保护 | `test_pipeline_catalog_readonly`、`test_pipeline_schedule`、初始同步与偏好测试 |
| 前端日期恢复/迟到响应、历史明细、权限状态 | Screener/历史Dialog/API就近测试与本批全量282项 |
| Modal嵌套焦点、ESC、遮罩拖选、尺寸 | Modal及SettingsModal测试；已有桌面/窄屏联调记录在策略归档设计证据 |
| 两步向导与失败重试、研究图表/候选 | Onboarding、useECharts、ResearchCandidatesDialog和StrategyBacktest就近测试 |

本批后端4381 passed/7 skipped，工具134 passed，前端282 passed，
Rust6 passed、原生桥接自检、mypy、Ruff增量无新增、构建/文档检查通过。
全量初次失败和真实夹具修正过程详见verification；没有吞掉失败或改弱业务断言。

## Failure-mode pass / code-quality dimensions

| Dimension / failure mode | Status | Support |
| --- | --- | --- |
| Design | OK | 归档服务与身份独立，API仍调用引擎/仓库，未复制Provider |
| Functionality | OK | 成功后归档，失败503保留旧记录；分钟目标日严格读取 |
| Complexity | OK | 租户复用documents事务，内存32MiB；摘要不加载全部明细 |
| Tests | OK | 正常/空/失败/迟到/回滚/RLS及UI状态；全量实际运行 |
| Naming | OK | 遵循既有Python/TypeScript约定 |
| Comments | OK | 修正摘要旧注释、快照契约和全角标点；无新增调试输出 |
| Style | OK | 前端lint、mypy、Ruff增量、diff --check |
| Silent assumptions / plausible-wrong logic | 已检查 | 名称与逻辑版本不同，输入默认值规范化；当前/历史/实时分别匹配 |
| Hallucinated APIs / deleted-but-used code | 已检查 | 实际git grep验证新调用与Modal迁移；前端构建与后端API契约通过 |
| Unmotivated edits / missing edges / scope creep | 已检查 | 整体已有变更属于用户授权整合；仅补必要文档/夹具/注释 |

实际运行：
`git grep -n -E 'persist_result|capture_identit|limit_local_strategy|StrategyResultHistoryDialog|StrategyConcurrencyControl|wait_for_catalog_jobs|useModalPortalContainer' -- backend/app frontend/src`
返回API单跑/批跑、pipeline_runner、Broker服务、Screener页面、System页面和
DatePicker/Watchlist调用；新符号都有真实消费方或框架入口。
业务写事务先锁tenant_bindings，结果保存串行且服从RLS；DocumentSession不记热缓存，
回滚值不能进入后续相同revision。日志只记录日期/资产/周期，不输出私有结果或凭据。

## Findings / operational artifacts

| Category | Support | Next action | Risk |
| --- | --- | --- | --- |
| 阻断或必须修正问题 | none | 无 | none |
| 运行环境接线 | 本地/生产Broker须读取Platform偏好 | 升级时加入config-file，保留旧资源上限，实际查询pending=false | 发布阶段门禁 |
| 产物/生产验收 | 源码测试不等于正式产物或部署 | 按release-review执行CI、镜像清单、停写备份/恢复和完整运行验收 | 发布阶段门禁 |
| 外部场景 | Windows真机、真实邮件/模型、容量/完整交易日/异机DR | 保持未签收，扩大承诺前另验收 | 非本批源码合并阻断 |

需求、架构、开发、二开、测试、运营、运维和恢复说明已同步。
没有缺少的源码合并必需工件。发布/回退执行者为当前授权会话，
后续响应由ECS维护者接收。PR/main CI须重新核验，不能依据本地结果绕过远程失败。

Override / commit posture：用户已授权，可在独立记录复审receipt后提交；
正式生产升级仍逐阶段满足release-review门禁。
