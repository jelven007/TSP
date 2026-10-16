# 远程主线自动版本策略验证

日期：2026-10-17。实现分支：`fix/rustdx-valid-stock-universe`。
本次本地迭代保留应用版本 `0.4.2`，未推送或合并到远程 `main`。

## 行为

- 本地迭代、提交、本地合并、功能分支推送及 PR 不自动改号；检查版本格式及包元数据一致性。
- 远程 `main` 每次 push（含合并与批量推送）触发一次自动递增，使用既有进位规则。
- 自动提交同步应用版本、后端锁文件和当前适用文档；独立包、第三方依赖与历史报告不改号。
- 来源 SHA 写入提交 trailer；重跑和同一事件的并发工作进程复用已有版本提交。
- 普通推送遇到主线前进时重新获取并重试；权限拒绝明确失败，不强制推送。
- 后端和前端质量门检出自动版本提交；Docker 从成功 CI 的 artifact 获取同一提交。
- 自动版本提交使用 `GITHUB_TOKEN`，不递归触发 push workflow；桌面发布仍需显式输入版本。

## 验证结果

| 验证 | 结果 |
| --- | --- |
| `pytest scripts/test_versioning.py scripts/test_advance_version.py scripts/test_version_workflows.py -q` | 47 passed |
| `uv run --no-sync pytest tests/test_version_source.py tests/test_desktop_packaging.py -q`（backend 目录） | 20 passed |
| 新增及修改的 4 个 Python 脚本定向 Ruff | All checks passed |
| actionlint 1.7.7 检查 CI、Docker、桌面发布 workflow | 退出码 0 |
| `python scripts/check_version.py --current VERSION` | valid application version: 0.4.2 |
| 隔离副本自动递增至 `0.4.3` 后执行 `uv lock --check --offline --python <现有解释器>` | 退出码 0；126 个包解析通过 |
| `git diff --check` | 通过 |

Git 测试使用临时 bare remote，覆盖实际 merge、版本写回、重复运行、延迟事件、
并发集成、同一事件双工作进程、写权限拒绝、未推送提交及脏工作区。
文件测试覆盖版本进位/上限、全部当前字段同步、缺失字段或元数据不一致时不写入、
第三方同号依赖和历史报告保留。Docker 的提交选择测试实际执行 workflow 中的
Bash，验证缺失或非法 artifact 会停止构建。

## 生效条件与边界

工作流要在本次实现进入远程 `main` 后才启用。GitHub 仓库须允许 Actions 写入，
且主线保护规则须允许该自动提交；本次未变更远程权限，也未验证实际 GitHub 写回。
权限失败会阻止质量门，可修正权限后重跑。

版本分配先于质量门；测试失败仍保留版本号，重跑不会再加一次。
本次未运行前端构建或完整后端测试，未修改应用业务代码；远程 CI 仍保留完整质量门。
本次未生成发布标签、构建发布产物或部署生产环境。
