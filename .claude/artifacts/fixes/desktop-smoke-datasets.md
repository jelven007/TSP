# Bug: 桌面自检的 rustdx 数据集契约过期

> Status: FIXED（源码回归通过，冻结安装包仍须单独验收）
> Mode: --quick
> Severity: blocker（阻止正式桌面发布）
> Last updated: 2026-10-26

## Symptom / Expected

v0.4.24 桌面发布运行 40250368241 已生成 Windows 安装包，但便携版 EXE
自检退出 1。预期默认 rustdx 插件声明的七项能力完整加载后通过自检。
该版本没有发布正式 Release，本地和 ECS 未切换。

## Reproduction

修改 `backend/tests/test_desktop_packaging.py` 的成功样本，使其读取真实
`backend/app/plugins/rustdx/plugin.yaml`，而不是待测 `_RUSTDX_DATASETS`。
在 v0.4.24 旧实现下，定向 pytest 三次分别失败，均报告：

```text
rustdx datasets mismatch:
expected=['adj_factor', 'daily', 'depth5', 'financial', 'minute', 'realtime']
actual=['adj_factor', 'corporate_actions', 'daily', 'depth5', 'financial', 'minute', 'realtime']
```

## Hypotheses & diagnosis

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| 桌面自检仍要求旧六项能力 | confirmed | EXE 日志与读取真实插件清单的三次失败一致 |
| 原生插件或应用无法启动 | eliminated（就本次退出原因） | 原生安装检查通过；应用启动和管理员自检成功，随后在数据集比较处退出 |

## Root cause

插件清单和 Provider 已增加 `corporate_actions`，桌面自检常量未同步。
旧测试从该常量生成成功样本，无法发现真实插件清单与校验器不一致。
反向调用链为冻结 EXE → `_run_smoke_checks` → `list_plugins` →
`_validate_smoke_plugins`；错误在旧常量的精确集合比较处引入。

## Fix

`backend/app/desktop.py` 的预期集合补充 `corporate_actions`，继续拒绝
缺失、不可用或数据集不完整的插件。成功测试改用真实 YAML 清单。
无 API、数据库、配置、用户数据或依赖变更。

## Verification

- V-1：旧实现定向回归连续三次失败，异常与冻结 EXE 日志一致。
- V-2：修复后桌面打包模块 `19 passed`。
- V-3：独立进程临时移除新常量项，回归重新失败，工作目录源码未修改。
- V-4：两份 Python 文件按 CI 导入口径 Ruff 检查通过。
- V-5：正式冻结 EXE、真实窗口、安装/卸载以及更新清单另由新版本发布验收；
  当前记录不预先签收。

## Regression test

`backend/tests/test_desktop_packaging.py::test_smoke_plugin_validation_accepts_default_rustdx_provider`
读取真实内置插件元数据，防止待测常量和测试样本同时陈旧造成误绿。

## Pattern analysis

实际搜索 `_RUSTDX_DATASETS` / `datasets mismatch`，只命中桌面声明/比较
及原成功样本；后者已改为真实清单。没有扩展修复其他初始化或网络行为。
日志里的交易所目录 403 是另一个数据源状态，不是本次 EXE 退出的异常。

## 生命周期与发布

| 领域 | 本次影响 |
| --- | --- |
| 需求 | 既有桌面完整启动与默认插件能力要求，需求编号不变 |
| 方案 | 局部 L3 校验修正，继续使用同一插件注册/能力契约 |
| 开发 | 单项常量与真实元数据回归，无新接口或扩展点 |
| 测试 | 三次复现、19项模块回归、反向验证及 Ruff；冻结产物另验收 |
| 运营 | 未通过 EXE 自检不发布安装包，不把源码标签称为稳定 Release |
| 运维 | 旧服务保留；修正经 PR/CI 自动版本后重新构建固定产物，不移动 v0.4.24 |

源码与测试的修正不代表新安装包验收通过。主线 CI 自动分配后继版本；
本次不得手工改号、重新指向旧标签或跳过原发布门禁。
