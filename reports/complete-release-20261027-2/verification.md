# 2026-10-27 第二批最新内容主线整合核对

本批基线为 v0.4.33（ded70245）。本记录只证明提交前源码核对与测试，不表示已经部署。
应用版本不在功能分支改号；合并后由远程 main CI 自动生成。

## 范围与契约

- 冻结的 61 个发布文件：42 个已跟踪修改及 19 个新增文件。同步分类默认收起，
  自选导出跟随当前分类，日期与时分共用弹层，系统设置简化并支持输入并发偏好。
- 策略页挂载、日期和资产切换只读取保存结果；手动运行及显式重载仍沿既有资源准入执行。
  旧 `screener_auto_run` 字段兼容读取并返回关闭。
- 管理员可保存 1～999 的偏好，实际并发仍受部署上限 16 及整机 25% 资源预算约束；
  租户权限和并发限制保持原契约。
- 日K历史仍使用 1d/value/unit 契约；本批不改金融数值、复权、证券类型或 Provider 路由。
  5/10/15分和周K继续沿既有本地派生链路。
- 原工作区、并行工作树与未发布研究资料已分别冻结和归档；不回灌旧版本残留。

## 实际验证

| 验证 | 结果 |
| --- | --- |
| 前端 `pnpm test`、`pnpm lint`、`pnpm build` | 83 文件、437 测试通过；lint/build 退出 0 |
| 后端 `pytest tests -q`，隔离 PostgreSQL | 4731 passed、12 skipped、180 warnings；退出 0 |
| 工具测试 | 134 passed |
| 增量 Ruff | 历史 57 项保持 57 项，新增 0 |
| mypy 定向检查、原生桥接 | 1 文件通过；原生桥接 ready |
| 文档路径、Markdown 和需求模块 | 87 路径、31 Markdown、8 模块通过 |
| `git diff --cached --check` | 退出 0 |

完整命令、输出与冻结身份保存在本批受控发布证据目录，后续部署报告独立记录实际主线
SHA、CI、产物身份、两端升级、恢复验证和浏览器验收。12 个跳过测试与历史 Ruff
告警不表述为通过；本批没有 Windows 桌面产物验收。

## 复审

结论：可合并。逐项检查规范、功能、闭环、注释及废代码，未发现阻断问题。
根因及回归对应
[系统设置](../../.claude/artifacts/fixes/system-settings.md)和
[日K画像](../../.claude/artifacts/fixes/restore-daily-profile.md)。

真实调用方核对：

```text
git grep -n -E 'TimePicker|usePickerPopover' -- frontend/src
  ':!frontend/src/components/TimePicker.tsx'
  ':!frontend/src/components/usePickerPopover.ts'
```

6 个匹配位于 DatePicker、KlineSyncConfig、ExtDataPullPanel；共享样式、当前分类
导出及并发控件另经 `git grep` 核对，生产调用分别来自 DatePicker/TimePicker、
Watchlist 和 System。新测试为框架入口，无悬空公共符号。

非阻断建议（仓库 P3 / review skill P2）：Screener 中“进入页面自动跑”的旧注释
已与手动执行行为不符；本批保留为注释整理建议，不影响运行或发布。

## 六类文档入口

需求更新见 [requirements](../../docs/requirements.md)，方案见
[architecture](../../docs/architecture.md)，开发见
[engineering](../../docs/reference/engineering.md)及
[secondary-development](../../docs/secondary-development.md)，测试见
[testing](../../docs/testing.md)，运营见
[release-operations](../../docs/release-operations.md)，运维见
[operations](../../docs/operations.md)。用户操作同步于
[操作说明书](../../操作说明书.md)。这些文档与本批实际行为一致，不将设计示例当作已有 API。
