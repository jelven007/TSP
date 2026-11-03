# 分钟采集正式发布审查

日期：2026-10-29。用户已明确授权提交、推送，并按正式流程升级本地与ECS。
本记录是提交前检查，不代表主线质量门、产物发布或生产升级已经通过。

## Review Scope

- 原始要求：修复本地分钟任务失败和同类问题，严谨复核两端性能；纳入已授权的逐证券隔离。
- 精确范围：独立功能分支的41个暂存文件，+7,642/-73；其中CSV为5,521行。
- 作者为当前及同期修复会话；逐行diff、行为回归和源码指纹独立核对。
- 暂存树：`23101be288a221be3a7430b020de3001b1660a74`；基线`f4d8c11b766bf62c269d8fca05b7ef6d9a559e42`。
- 无关研究、其他修复文件、运行配置、账号及SMTP未进入该暂存集合。

## Review Anchors

| Anchor | Changed Location | Why It Matters |
| --- | --- | --- |
| 时限 | `backend/native/rustdx_native/src/lib.rs:229,316,600` | 区分排队与传输，保留多页请求预算 |
| 缺口 | `backend/app/services/kline_gaps.py:164,236` | 32日/100证券有界扫描，精确缺口相交并重读 |
| 来源 | `backend/app/plugins/rustdx/provider.py:424,482,546` | 完整重采一次，拒绝必需坏页 |
| 终态 | `backend/app/api/kline.py:1287,1700` | 实际warning进入既有任务终态 |

## Intent Match / Verdict

意图一致，未发现范围蔓延。结论：READY，可提交。
用户本轮授权涵盖正式流程所需的提交、推送、PR归并和两端升级。
无须覆盖未解决问题；仍须完成主线与部署门禁后才能切换常驻服务。

## Code-Quality Dimensions

| Dimension | Status | Support Or Reason |
| --- | --- | --- |
| Design | OK | 沿原Provider、周期服务、原生池和分区锁 |
| Functionality | OK | 快照227项通过，源值、取消、缺口及异常路径有结果断言 |
| Complexity | OK | 未新增框架、依赖、配置或连接额度；内存和请求窗口有界 |
| Tests | OK | 新回归检出旧实现，实际TCP覆盖排队、慢传输、财务竞争及池恢复 |
| Naming | OK | 沿现有Python/Rust命名 |
| Comments | OK | 解释预算和精确缺口的原因，没有新增调试输出 |
| Style | OK | 增量Ruff42项既有、0新增，格式/diff检查通过 |

## Findings / Blocker List

未发现阻断或需修改后合并的问题。新增CSV原CRLF触发Git空白检查，
已在发布副本规范为LF，并以CSV解析对比确认全部5,520个缺口键未变化；
用户打开的原工作区CSV保持原字节。

## Behavior Exercise

| Changed Behavior | Failing-Without-Change Test? | Evidence | Gap |
| --- | --- | --- | --- |
| 队列/传输预算 | 是 | 旧分钟连续3次失败，除权同类失败；最新两端TCP各14通过 | Windows仅编译 |
| 多日遗漏窗口 | 是 | 基线反向11失败/2通过；真实请求约38,100→1,400 | 不认证全市场SLA |
| 必需坏页拒绝 | 是 | 初始6失败；坏时间/重复页均不提交该证券历史 | 源保留范围独立 |
| 完整重采与隔离 | 是 | 来源反向8失败/1通过、隔离反向5失败/4通过 | 三四月仍缺5,520键 |
| 混合schema及进度 | 是 | 两种分区顺序、100/101/201证券及32/65/364日断言 | 无 |

## Failure-Mode Pass

| Failure Mode | Checked? | Finding |
| --- | --- | --- |
| Silent assumption | 是 | 使用真实日历及上市日期；未知日历不当作休市 |
| Plausible-but-wrong logic | 是 | 精确缺口相交、锁内only_missing及落盘重读 |
| Hallucinated API/import/type | 是 | `git grep --cached`确认已有调用；不新增扩展API |
| Deleted-but-used code | 是 | 仅删除整批失败汇总，健康证券继续执行 |
| Missing edge case | 是 | 空、坏页、失败、取消、并发、边界和恢复均有回归 |
| Scope creep | 是 | 41文件全部关联本次分钟及追加隔离契约 |

调用证据：`git grep --cached -n -e record_unfilled -e _record_rows -e _bar_records_once -- backend/app backend/tests`，
返回12处匹配，覆盖定义及实际消费。暂存内容秘密形态检查无匹配。

## Missing Artifacts / Specialist Sanity Check

| Artifact | Needed? | Gap | Owner |
| --- | --- | --- | --- |
| 主线质量门和正式镜像 | 是 | PR/main CI及固定SHA产物尚待生成 | 当前发布执行会话 |
| 两端停写备份及恢复 | 是 | 升级前执行，不以旧备份替代 | 当前发布执行会话 |
| 数据迁移 | 否 | schema/格式未变，不重跑初始化 | 当前发布执行会话 |
| 运维及回滚指引 | 是 | 已有发布手册；执行状态需继续留证 | 当前发布执行会话 |
| 新信任边界威胁模型 | 否 | 未新增权限/认证/外发能力 | 当前发布执行会话 |

内部补充检查了artifact身份与运行升级边界，不以代码审查代替部署验收。

## Verification Limitations

冻结快照227 passed / 17既有warnings，38.46秒。七个生产代码文件与已验证工作区
SHA256一致；此前最新全量4,650 passed / 162 skipped / 173 warnings。
快照make check通过87路径、31文档、8需求模块、v0.4.37及diff检查。
正式提交继续保持v0.4.37；新版本只由远程main CI递增。
