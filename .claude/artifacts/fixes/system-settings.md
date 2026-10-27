# Bug: 系统设置保留了自动运行和旧并发输入限制

> Status: FIXED
> Mode: default
> Severity: functional
> Last updated: 2026-10-27
> Baseline: ded70245 / v0.4.33

## Symptom / Expected

用户要求“策略页”改“并发策略”，删除进入策略页自动运行及重复并发标题，
首次显示推荐数字，支持手工输入1～999整数；“个股详情外链”改“个股外链”；
开源版本区域删除手动检查，只保留仓库链接、最新版本号与发布时间。

## Hypotheses & diagnosis

| 假设 | 置信度 | 结论与证据 |
| --- | --- | --- |
| 只删除设置开关即可移除自动运行 | 中 | eliminated：Screener的日线/分钟effect仍根据旧偏好true或默认true补算；后端getter/setter也允许启用 |
| 页面输入、API校验与Compute配置校验共同限制手工值 | 高 | confirmed：select只生成资源上限以内选项，Pydantic和ConcurrencyConfig均限制16；999保存与Broker重读测试失败 |
| 开源组件中的多余展示来自独立页面逻辑 | 高 | confirmed：OpenSourceReleaseMonitor直接提供check、已读和检查时间，简化该组件即可保留原共享查询与布局提醒 |

## Reproduction

修改测试先于生产代码。旧实现连续3次均前端17失败/13通过、后端4失败/26通过，
复现自动请求、数字输入缺失、999拒绝、旧自动偏好仍生效及开源手动检查仍存在。
修复后在隔离副本恢复HEAD的4个前端和3个后端目标生产文件，
前端20失败/12通过、后端4失败/26通过；恢复修复后前端32和后端30项通过。
原工作区未stash、回滚或覆盖其他修改；后续补充切日期停止后续任务和资源不可用拒绝用例。

## Fix / compatibility

L3局部变更，复用原设置、查询键、原子偏好存储和Compute资源准入。
并发控件用独立draft，失焦/Enter校验保存；首次null模式回显推荐数字，
手工值不被后台轮询覆盖，失败回到已生效值。“使用推荐值”保存null。
配置范围1～999与既有执行预算/部署上限分离，大值可保存并重读，
实际生效数仍受主机内存和CPU各25%的策略预算及部署上限约束。
并发降低不终止旧任务，不增加主机预算或Broker部署容量。

删除Screener页面进入、切日期/资产触发的日线和分钟自动补算；
旧字段保留deprecated兼容，读取恒false，旧客户端写入也不能开启。
每策略手动运行和监控开关仍沿原API，手动批量重跑继续覆盖日线与分钟；
批跑期间切换日期/资产后不继续创建上一上下文的分钟任务。
开源设置只显示仓库链接、最新版本号链接和发布时间，保留加载/空/错误反馈。
应用更新与Layout中的开源自动提醒继续复用原独立缓存。

## Verification

- 后端6文件93项通过：HTTP严格输入、1/999、持久化/重读、资源缩小、
  不可用/零预算拒绝、权限、IPC只读、旧任务收尾、共享预算和队列。
- 前端7文件65项通过：系统设置、Screener、开源API/store、StrategyCard、
  默认参数与监控设置；覆盖默认/旧偏好、输入边界、错误、租户只读、手动重跑及日期隔离。
- `pnpm build`、`pnpm lint`通过。`strategy_resources.py`和本轮后端测试Ruff通过；
  settings/preferences的57项历史告警与HEAD基线相同，没有新增告警。
- 本机Chrome/Playwright真实组件、API拦截：999/Enter保存及刷新保留、
  1000拒绝、恢复推荐、503失败保留生效值、手动分钟运行与监控POST/DELETE通过。
  1440×1000与390×844浅深色无水平溢出，pageerror为0；截图见[记录](../../../reports/system-settings-20261027/verification.md)。
- 六类生命周期文档与操作说明已同步；根`make check`和`git diff --check`通过。

## Limits

Browser plugin不可用，使用现有Playwright环境；浏览器接口拦截未写用户配置或行情。
Chrome本地网络访问检查阻止Vite HMR WebSocket；503是测试主动模拟的保存失败。
这些消息不影响页面交互，未认证热更新或真实Docker容量。
未提交、推送、更新本地运行副本或部署线上；版本保持v0.4.33。
前轮日K修复、日期控件/同步布局和研究文件全部保留。
