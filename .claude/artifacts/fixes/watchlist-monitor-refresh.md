# 自选多分组、清空保护与监控目标重读

> Status: FIXED
> Mode: default
> Severity: functional
> Last updated: 2026-10-26

## Symptom / Expected

同一股票可在多个分组中，“全部”只显示一条。策略引用的分组禁止清空；
任一组被引用时禁止清空全部，未引用组可以单独清空。每次启用策略监控读取最新目标。

## Hypotheses & diagnosis

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| 当前存储全局排斥同股跨组 | 排除 | `add_batch` 合并关系，跨组重复添加测试返回一个实体和两个组 |
| 旧文件重复证券直接进入列表 | 确认 | 旧单组文件的同股两行原样返回，新增归一测试失败 |
| 清空入口复用了删除引用保护 | 排除，缺少保护为根因 | 文件、SQL与Cell清空均未检查引用；测试在写前未抛异常 |
| 启用阶段已重读目标 | 排除，缺少重读为根因 | 保存启用规则没有调用目标加载器，相关回归失败；原计算轮次仍会动态解析 |

## Root cause / Fix

L3聚焦修改既有服务与API，不增加扩展框架或存储表。
旧文件读取按证券合并组关系，保留首条排序、备注和日期；仅构建快照，不在读取时改写文件。
`targets.ensure_clearable`在原自选锁/租户事务内保护全部与分组清空，兼容Cell入口；
冲突返回409，不修改成员、revision或审计。页面根据`referenced_by`禁用按钮并提示原因。
启用策略监控调用`config.load_override`重读目标，失败返回503且不启用；
内部展开成员不写入规则，计算轮次继续读取最新成员。

## Reproduction / Regression tests

- `backend/tests/test_watchlist_groups.py`：跨组重复添加、旧文件重复证券与元数据保留。
- `backend/tests/test_strategy_targets.py`：文件服务/API清空引用保护、共享证券保留。
- `backend/tests/test_full_strategy_targets.py`：真实SQL、Cell、HTTP、租户隔离、监控启停重读。
- `backend/tests/test_strategy_monitor_activation.py`：启用/停用/再启用重读及失败保留停用。
- `frontend/src/pages/Watchlist.test.tsx`：全部清空保护、切组、解除引用恢复。
- `frontend/src/components/screener/StrategyTargets.test.tsx`：关联组禁用、未关联组可清空。

## Verification

- V-1：文件与前端复现运行三次，业务断言稳定失败；SQL复现三条清空路径失败。
- V-2：修复后的相关后端171项通过，包括一次性PostgreSQL真实事务/API验证。
- V-3：前端68文件305项通过，`pnpm build`、`pnpm lint`通过。
- V-4：临时副本保留新测试、生产文件恢复为HEAD，后端五条与前端两条回归再次失败。
  未stash或修改用户工作区来做反向验证。
- V-5：Ruff逐文件对比HEAD，新增告警0；原模块已有告警未扩展清理。
- V-6：`make check`通过，版本仍为0.4.23，`git diff --check`通过。
- V-7：本机Chrome/Playwright以自选API模拟数据验证1440×1000、390×844；
  实际UI完成同股加第二组、全部一行、清空限制及未关联组清空后另一组保留。
  用户自选数据没有被写入或清空。截图及运行记录保留在本地临时目录。
  无页面脚本异常；控制台另有既有GitHub版本检查404和跨域失败，来源均为外部更新检查，
  不属于自选请求或本次交互失败，未扩大本次修复范围。

## Pattern analysis

搜索`def clear`、`def clear_group`与`ensure_clearable`，本次自选共有五条服务清空路径：
文件全部/分组、SQL全部/分组、Cell全部，均已接线。成员移入移出保留动态语义，
单证券移除没有扩展成新引用限制。策略目标解析与已有配置/任务快照契约保持一致。

## Limits / Handoff

本地功能分支未提交、未推送、未部署；没有版本加号或数据库schema迁移。
验证覆盖文件和临时PostgreSQL，浏览器自选写操作使用模拟接口；不代替生产部署验收。
需求、架构、测试、支持、运维与二开文档已同步。
