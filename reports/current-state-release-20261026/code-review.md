# 精确候选复审

Review anchors：`backend/app/services/kline_periods.py:296`、
`backend/app/services/strategy_results.py:245`、
`frontend/src/components/data/ActiveJobCard.tsx:103`、
`frontend/src/components/WatchlistGroups.tsx:307`。
审查基线：`8ae79f17d5c03f75d29316f13ea164cb427ca98c`；日期2026-10-26。

## Intent and scope

用户要求整合最新源码、核对本地/生产运行、更新六类文档后完成合并发布及环境升级。
本次冻结包含周期查询/进度、页面设置兼容、分组保护/CSV、监控目标重读和历史归档。
每个生产改动均有对应源码/测试和生命周期文档；不含三组并行研究报告、运行数据、
凭据、虚构扩展API、依赖升级、schema升级或手动应用加号。

## Verdict and findings

结论：可提交/合并，必须在远程CI通过后执行合并，正式产物和生产切换仍各自过门。
当前未发现阻断问题；用户已有合并和升级授权，可继续执行。

| Category | Support | Action / risk |
| --- | --- | --- |
| 已修复契约快照 | `backend/tests/test_openapi_contract.py:100`；完整前后比较仅daily/minute可选freq | 已同步文档/指纹并全量复验，无破坏性开放面变更 |
| 页面设置原故障 | `frontend/src/components/data/PageSettingsModal.test.tsx:117`正常共享Modal/StrictMode路径 | Follow-up：原报错未复现，不声明已修复；取得实际报错后继续定位 |
| 线上运行/恢复/产物 | [发布复核](release-review.md) | 后续阶段门，切换前检查固定身份、活跃任务、一致备份/实际恢复与配置；未通过不切换 |
| 容量/交易日/邮件/模型/Windows真机 | [验证边界](verification.md) | Follow-up：旧边界保留，下一运营接收列出，不以烟测签收 |

Blockers：None for the source candidate。无新增风险例外或未经授权的覆盖/清理。

## Behavior exercise

后端4505、前端336、工具134、Rust6项通过；隔离Web、CI口径Ruff/mypy、
Lint/build与make check通过，详见[实际验证](verification.md)。
独立旧实现反向测试检出后端48条与前端29条失败，覆盖进度/周期、去重/删除、
分组/监控及CSV，未stash或回滚用户工作区。
页面设置无效保存值有失败回归；原点击故障没有失败复现，因此没有“原故障已解决”主张。
最后导入顺序调整按CI的first-party显式配置检查，只影响测试导入排列。
调整后的SQL历史/开放契约/监控启用/历史进度定向19项在新的临时PostgreSQL中通过。

## Failure-mode pass

| Lens | Result / evidence |
| --- | --- |
| Silent assumptions | OK；周期/资产/时间/空字段有校验，缺能力/源失败明确反馈，旧字段缺失可读 |
| Plausible wrong logic | OK；批次写盘后推进，阶段切换重置；相同结果推进任务顺序，迟到任务不覆盖 |
| Hallucinated APIs/imports/types | OK；全部复用真实Provider、QueryKeys、SQL事务、Modal与现有客户端 |
| Deleted-but-used code | OK；仅日K表单不再用ExtendHistoryPanel，旧历史API仍保留 |
| Unmotivated edits / scope creep | OK；文档状态与契约/质量门修复归本次整合，独立研究报告排除 |
| Missing edges | OK；SQL/SQLite回滚、跨租户/只读、同ID删除重建、空历史、失败取消和旧配置均有回归 |

## Code-quality dimensions

| Dimension | Status | Support |
| --- | --- | --- |
| Design | OK | 领域服务负责行为，API薄映射，Provider中立 |
| Functionality | OK | 数值/范围/身份/回滚回归及浏览器用户路径 |
| Complexity | OK | 小范围函数和原事务/分区锁，无平行框架 |
| Tests | OK | 全量及旧实现负验证，SQL使用一次性实例 |
| Naming | OK | period/stage_pct/cache_token含义在契约与文档解释 |
| Comments | OK | 解释最新任务顺序、跨进程重建和完整清单导出原因 |
| Style | OK | CI增量Ruff新增0、前端Lint/build、diff空白检查 |

## Public surface and operational artifacts

daily/minute新增可选freq，默认保持1d/1m；开放端点集合、响应components不变。
历史筛选/删除只在已认证私有业务入口，复用写权限/RLS/同源校验。
无schema/依赖/持久配置迁移；新内部cache_token兼容旧记录。
运行日志/任务进度沿用原设施；备份、固定产物、回退、启动顺序和观察交接见发布复核。
没有缺失的源码候选操作文档；产物/上线结果由实际执行另留证。
Sanity extras：已在本复审检查租户事务、缓存和接口兼容，无额外专项路由。
