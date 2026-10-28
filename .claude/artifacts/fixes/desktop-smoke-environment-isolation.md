# Bug: 桌面发布Smoke继承外部运行配置

> Status: FIXED
> Mode: (default)
> Severity: functional
> Author: AI assistant
> Last updated: 2026-10-28

## Symptom

macOS冻结应用按发布workflow的本地免登录smoke命令启动时，默认进入多租户API模式，
因缺少Cell配置而失败；本地仓库存在`.env`时，日志和数据还会落到外部配置目录。

## Expected

该步骤只验证显式standalone兼容模式，应屏蔽`.env`，将全部写入限制在runner临时
目录，并从同一目录读取`desktop.log`。

## Reproduction

- 原步骤：`.github/workflows/release.yml`中的“验证 macOS 和 Linux 本地版无需登录”。
- 实际失败：`CellSettings`缺少`id/control_dsn/data_dsn`。
- 回归测试：`tests/tooling/test_version_workflows.py`中的
  `test_desktop_standalone_smoke_isolates_runtime_configuration`。

## Hypotheses & diagnosis

| # | Hypothesis | Verdict | Evidence |
| --- | --- | --- | --- |
| H1 | workflow未显式选择其声称验证的standalone模式 | confirmed | 注入`/dev/null`、standalone和临时`DATA_DIR`后，冻结应用输出两个成功标记 |
| H2 | PyInstaller应用本体或rustdx桥接不可启动 | eliminated | 同一`.app`在隔离环境中启动、完成OpenAPI/插件检查并正常关闭 |

## Root cause

macOS/Linux产品默认已切换为多租户，但旧smoke仍假设默认本地免登录，并把数据目录
硬编码到可执行文件旁。步骤没有声明测试模式，也没有隔离仓库或runner环境。

## Fix

- `.github/workflows/release.yml`：使用`$RUNNER_TEMP/desktop-smoke`，设置退出清理，
  并显式注入`TICKFLOW_ENV_FILE=/dev/null`、standalone模式和`DATA_DIR`。
- 产品默认多租户行为未改变。

## Verification

- V-1：workflow回归测试在修复后通过。
- V-2：临时撤回workflow修复后测试重新失败，恢复后再次通过。
- V-3：macOS ARM64冻结应用在隔离临时目录中生成
  `DESKTOP_SMOKE_TEST_OK`和`DESKTOP_AUTH_BYPASS_SMOKE_TEST_OK`。
- V-4：smoke前后运行文件清单均无changed、missing或added项。

## Regression test

- 路径：`tests/tooling/test_version_workflows.py`
- 名称：`test_desktop_standalone_smoke_isolates_runtime_configuration`

## Pattern analysis

| 搜索方式 | 命中数 | 是否本次同类隐患 |
| --- | ---: | --- |
| 搜索旧可执行文件旁`data`路径及无隔离smoke命令 | 1 | 仅发布workflow该步骤，已修复 |
