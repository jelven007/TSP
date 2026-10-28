# Bug: 本地后端测试继承正式运行配置

> Status: FIXED
> Mode: (default)
> Severity: functional
> Author: AI assistant
> Last updated: 2026-10-28

## Symptom

本地全量后端测试出现116项跨域失败，集中在AI用量、旧身份文件、邮件默认值、
watchlist租户状态和导入期全局对象。

## Expected

仓库标准测试入口应与CI一致，屏蔽真实`.env`并使用独立临时数据目录，不读取或写入
受控运行配置、平台偏好和用户数据。

## Reproduction

- 原命令：`make test-backend`
- 最小复现：auth、AI和邮件代表用例在真实运行配置下连续3/3失败。
- 回归测试：`tests/tooling/test_backend_test_isolation.py`

## Hypotheses & diagnosis

| # | Hypothesis | Verdict | Evidence |
| --- | --- | --- | --- |
| H1 | 本地Makefile遗漏CI已有的隔离环境 | confirmed | 非敏感配置摘要为`service_role=api`、多租户开启且平台目录已配置；按CI环境重跑原16个失败文件为209项全通过 |
| H2 | 116项分别是业务模块回归 | eliminated | 未改业务模块，仅隔离环境后完整后端套件全部通过 |

## Root cause

CI的后端任务固定设置`TICKFLOW_ENV_FILE=/dev/null`、standalone模式和临时
`DATA_DIR`，但Makefile的本地入口直接运行pytest。本机存在正式`.env`时，
pytest在导入期取得API多租户和平台目录，破坏了单测约定的standalone基线。

## Fix

- `Makefile`：`test-backend`创建一次性目录，注入与CI一致的运行模式并在退出时清理。
- `tests/tooling/test_backend_test_isolation.py`：锁定五项隔离契约。

## Verification

- V-1：回归测试在修复后通过。
- V-2：临时撤回Makefile修复后回归测试重新失败，恢复后再次通过。
- V-3：`make test-backend UV=/Users/bytedance/.local/bin/uv`结果为
  `4663 passed, 162 skipped`。
- V-4：原16个失败文件在隔离环境下为`209 passed`。

## Regression test

- 路径：`tests/tooling/test_backend_test_isolation.py`
- 名称：`test_backend_test_target_isolates_runtime_configuration`

## Pattern analysis

| 搜索方式 | 命中数 | 是否本次同类隐患 |
| --- | ---: | --- |
| `grep -R "pytest tests -q" Makefile .github/workflows` | 4 | CI、Docker和Release均已有隔离环境；Makefile是唯一遗漏入口 |
