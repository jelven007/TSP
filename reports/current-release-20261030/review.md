# 2026-10-30 最新版本整合与发布评审

本轮由用户明确授权：先汇总本地最新版本并同步需求、方案、开发、测试、
运营与运维文档，再合并仓库、删除已合并分支、发布新版本并升级本地与线上。
起点为应用0.4.38、main提交`5b273174f2e2a8ac539fcea363fa02315a61ebe9`。
应用版本只由远程main CI递增一次，本文不预先指定新版本。

## 发布范围

| 变更 | 最终契约 | 需求归属 |
| --- | --- | --- |
| rustdx三组任务绑定 | realtime/minute/history各8业务+1探测；全局24+3及物理端点额度共同约束；当日候选按1/4/7、2/5/8、3/6/9分配，分钟盘中回填6并预留2 | FR-DATA-05 |
| 失效会话与来源完整性 | 切站排空、精度世代、锚点重采和完整参考窗口核验；错误来源不得拼接为完整金融数据 | FR-DATA-05 |
| 指数全历史读取 | 未指定开始日时读取本地实际分区范围；日/周/月共用既有指数接口，用户显式日期优先 | FR-DATA-05 |
| 实时轮询界面 | 数据总览采用1～10秒、整数步长滑块；保留现有保存接口、源端限速与已存值，用户操作前不改配置 | FR-DATA-05 |
| 目录分页回归修复 | 搜索内容未变化时不启动防抖，避免初次加载250毫秒后覆盖用户已选页码 | 最终整合回归 |
| 历史发布说明同步 | 上轮0.4.38已部署事实与本轮待发布状态分别标注 | 文档生命周期 |

原生核心仍为2.0.4，桥接独立升级为0.3.0/API4。必须重新构建各平台原生桥接，
仅替换Python文件不能完成升级。不新增Provider数据集、二开插槽或数据库schema。
股票与七只指数、金融单位、北京时间、分区锁、单写Platform、公共发布及缓存契约保持。

完整起始文件及哈希已保存在受控发布目录。未跟踪的研究、交易明细、
行情缺口CSV和运行证据原位保留；本轮发布只收录必要源码、测试、文档与脱敏验收摘要。
基线中的1,083个未跟踪文件约375MB，其中单个缺口CSV超过100MB，不作为发布源码提交。
其他会话晚到的改动单独记录，不覆盖或自动纳入冻结发布。

## 六类文档映射

| 生命周期 | 本轮更新入口 | 需保持的事实 |
| --- | --- | --- |
| 需求 | docs/requirements.md | 任务分组、指数全历史、轮询界面与初始化未完成项 |
| 方案 | docs/architecture.md、docs/rustdx-task-routing.md | 逻辑组/物理端点/跨进程额度与来源核验 |
| 开发 | CONTRIBUTING.md、docs/secondary-development.md、docs/reference/integrations.md | 桥接API4、实际接线、旧配置与二开兼容 |
| 测试 | docs/testing.md、verification.md | 最终整合源码、正式产物、两端运行分别验证 |
| 运营 | docs/release-operations.md、操作说明书.md | 用户操作与已知历史缺口，任务完成不等于历史齐全 |
| 运维 | docs/operations.md、docs/version-baseline.md | 固定revision、停写备份恢复、配置保留、重启与回退 |

## 发布顺序与停止条件

1. 冻结并留存本地全部改动，复审最终发布diff，执行受影响回归、前端测试/lint/build、
   原生测试/fmt/clippy、文档/版本/锁文件/固定上游身份检查。
2. 功能分支提交并整合指数修复，通过PR质量门后合并main；CI自动改号、测试并打标签。
   同次main集成对应一次版本递增。
3. 核对CI实际revision、源码包、macOS桥接、Linux双架构镜像及完整Runtime/Compute身份，
   完成隔离业务样本，再发布服务端Release。
4. 等待实际采集及研究任务自然结束，停写备份行情、配置、身份库和业务库；
   实际恢复到临时库并逐表对账后才切换本地。
5. 本地登录、六功能、三组原生身份、真实任务及完整重启通过后，按同样规则升级ECS，
   保留管理员、注册/找回、SMTP及用户后续修改的配置。
6. 最后核对main与标签、两端版本和数据/配置；删除确已整合的分支。
   未提交工作树先留存，不强制清理。

任一质量门、来源核验、产物身份、恢复对账或健康检查失败均停止后续切换。
切换失败使用本轮备份、原源码、原Runtime/Compute与匹配配置恢复，不迁移或清空行情。

## 验收状态

发布前新鲜验证及实际运行验收逐项记录在[验证记录](verification.md)。
发布、升级和分支删除在有实际证据前保持待完成。
上轮0.4.38的两端部署为历史基线，不能用于认证本轮三组连接已经生效。

2026-10-30最终候选自审：三组源码与上轮已复审身份一致；指数响应继承
`extra=allow`契约，新增起始日期能穿过实际HTTP响应；新符号经真实`git grep`
核对Provider、Python facade、原生许可和界面调用方。没有发现候选范围内的P0/P1。
分页防抖问题经确定性失败回归修复后纳入本轮。审查由当前代理执行，
不声称独立人工或代理评审。

冻结后并行会话写入的财务存储`backend/app/api/data.py`、
`backend/tests/test_data_status_storage.py`和`frontend/src/lib/api.ts`晚到类型改动
及`frontend/src/pages/Data.tsx`财务展示原位保留。本轮采用冻结的指数API与滑块版本，
不能将尚未整合的财务工作包装为发布成果。

```text
━━━ Dev Code Review ━━━
Verdict   : READY
Scope     : 79 files · staged candidate; parallel unstaged finance work retained
Intent    : 三组采集保留完整来源，指数读全本地历史，整合界面与生命周期文档
Languages : Rust, Python, TypeScript; historical JSON evidence separately retained
Axis Check
  规范   ✓
  功能   ✓
  闭环   ✓
  注释   ✓
  废码   ✓
Findings
  none in the reviewed candidate
Commit
  fix(data): bind collection tasks and preserve complete local history
```

闭环真实命令：
`git grep -n -E 'quotes_for_task|incremental_bars_with_count|validate_bar_window|reference_view|bar_anchor|available_range|set_background_connections|quota::acquire' -- backend/app backend/native/rustdx_native/src`。
实际输出19处，覆盖indices.py:58/60、index_kline.py:22/36、
client.py:228/271/295/302/308/310、provider.py:306/441/444/491/1452、
原生lib.rs:442/763；未跟踪routing.py在stage后纳入正式候选。
79文件包含36,000余行历史JSON证据及锁文件，按技能规则不作逻辑逐行评审，
执行脱敏模式、文件身份和必要链接检查；源码调用链与回归单独认证。
