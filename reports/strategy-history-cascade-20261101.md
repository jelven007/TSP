# 策略删除级联清理验证

日期：2026-11-01。状态：已实现并同步工作区，未提交、推送或部署；应用版本仍为0.4.39。
来源：[实施方案](../.claude/artifacts/plans/strategy-history-cascade.md)。

删除自定义、AI或叠加策略时，按稳定ID删除全部日期、名称、逻辑版本、参数、
资产和周期的历史选股结果。Full同时删除直接关联的策略回测、优化、步进任务及附件
和研究候选；独立模式删除关联候选、本地回测溯源清单及回测重连结果。
正在运行的关联研究任务失去提交资格，旧执行快照及缓存不能重建已删结果。
新建相同ID后允许新执行，旧执行仍拒绝写回，历史身份指纹保持兼容。

源文件、历史和删除世代在Full同一租户事务提交；本地结果写入通过SQLite事务串行，
用户文件通过异常补偿恢复。只读源、结果删除失败、SQLite提交失败和符号链接越界
均不会返回删除成功。前端取消旧查询、移除目标历史/明细并刷新目录及研究结果，
失败保留确认框和错误提示。

其他策略、其他租户及模拟交易成交/持仓账本保留。共享冻结研究输入、行情数据、
独立因子研究/挖掘批次和仓库报告不作为该策略的直接结果删除。
本次不修改选股、买入、卖出、仓位或策略参数，也未执行新回测。
此前已删除策略的遗留数据未追溯批量清理，尚未操作实际运行数据。

## 实际验证

所有数据库验证使用临时PostgreSQL套接字和随机双库/角色，退出fixture后清理；
独立模式使用临时数据目录、禁用环境文件、standalone角色及4个Polars线程。
通过显式`PYTHONPATH`分别加载隔离与共享源码，避免复用虚拟环境的editable路径误读。

| 检查 | 实际结果 |
| --- | --- |
| 修复前反证 | 原源码下7个关键用例全部失败：历史残留、迟到归档、存储故障仍删除、旧缓存重建、旧候选保存、租户研究历史残留和迟到研究提交 |
| 最终隔离工作树后端回归 | 201 passed，49.28秒；5条既有Polars弃用警告 |
| 同步后共享工作区后端回归 | 201 passed，50.72秒；同样5条既有警告 |
| 前端定向回归 | 隔离及共享均18 passed，包含删除成功/失败、历史筛选和目标范围设置 |
| 前端构建 | `pnpm build`通过，TypeScript与Vite成功；既有Browserslist时效提示 |
| 浏览器夹具 | 本地Chrome/Playwright，1400×900浅色与390×844深色，确认文案/按钮完整可见；删除成功关闭、失败保留错误提示均通过 |
| Ruff | 新增仓库/删除服务、租户服务及测试全部通过；其余修改文件既有告警75→74，新增告警0，未全仓格式化 |
| 仓库根检查 | `make check`通过：87个必需路径、37份当前Markdown、8个需求模块、版本0.4.39与diff空白 |
| 全部报告链接 | 未通过：此前发布绑定报告引用`docs/testing.md#public-binding-sync`，当前文档无该锚点；本次未修改此前报告 |

后端回归范围：`test_strategy_history_cascade`、原策略删除/代码保存/结果归档、
Full策略级联/归档/研究/候选构建、旧缓存、screener缓存/历史/渐进批跑、
data release、研究候选、optimizer API、真实spawn回测worker、本地研究复测及挖掘候选。
新增接受用例覆盖HTTP业务权限/同源/RLS、事务回滚、SQLite提交失败文件恢复、
同ID重建、执行快照延迟捕获、历史及回测清单/旧缓存的迟到写回。

前端命令：

```bash
pnpm exec vitest run src/components/screener/StrategyDelete.test.tsx src/components/screener/StrategyResultHistoryDialog.test.tsx src/components/screener/StrategyTargets.test.tsx
pnpm build
```

当前会话未提供Browser插件工具，因此浏览器验证使用独立Playwright与本机Chrome，
不使用用户浏览器配置或实际账户。截图为隔离模拟接口夹具：
[桌面](strategy-history-delete-1400.png)、[窄屏](strategy-history-delete-390.png)。
首次夹具缺少Router导致空页面，补齐夹具后通过，没有据此修改产品源码。
早先下载等待被终止后依赖已缓存，最终离线执行成功；浏览器夹具不替代真实API权限测试。

## 发布与边界

源码候选自审未发现阻断项。生产及Windows尚未验证，不宣称已上线。
升级须使API/Worker使用同一删除防护实现，并保留业务备份及SQLite生命周期表。
本地异常补偿不是跨文件和SQLite的断电/崩溃原子事务，突然终止仍需按备份核对恢复。
SQLite逻辑删除和PostgreSQL行删除不承诺立即收缩数据库文件或安全擦除旧磁盘页。
没有新增公共扩展API、配置开关或生产PostgreSQL schema迁移。
