# Bug: Windows 前端构建改变声明文件换行，阻止正式发布

> Status: FIXED
> Mode: (default)
> Severity: blocker
> Author: jelven
> Last updated: 2026-10-26

## Symptom

v0.4.26 Windows 正式构建的便携启动、安装、GUI及卸载检查通过，
但封装校验拒绝 `source_dirty=true`，Release 未发布。

## Expected

固定提交的无修改构建保持清洁源码身份；真实源码修改继续拒绝发布。

## Reproduction

- 原正式工作流：GitHub Actions run 40254819266。
- 同标签只读诊断：run 40256413074，checkout 为
  `5651b33d489a5bc8ba521d0b0b25c7f420e1cbf4`。
- 初始 checkout 清洁；`pnpm build` 后唯一变更为
  `frontend/vite.config.d.ts`，Git diff 无内容变化。
- 源码与运行清单均记录同提交及 `source_dirty=true`。
- `backend/tests/test_build_manifest.py` 的
  `test_frontend_declaration_emission_preserves_clean_release_source`：
  Windows Git 自动换行场景连续三次失败，关闭自动换行场景通过。

## Hypotheses & diagnosis

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| 前端构建改变受跟踪文件，置信度中，先验50% | confirmed | 前端之后首次出现声明文件变更，真实Git临时仓库复现LF/CRLF状态差异 |
| 原生依赖安装生成未忽略文件，置信度中，先验35% | eliminated | 变更在依赖安装前出现；安装后仍只有同一声明文件 |

## Root cause

Windows Git 自动换行将声明文件检出为CRLF；TypeScript声明输出使用LF。
Git porcelain因此标记修改，即使规范化后的diff没有内容变化。
打包源码清单使用真实porcelain状态，严格seal按设计拒绝此脏清单。

## Fix

`.gitattributes` 为现有受跟踪的 `frontend/vite.config.d.ts` 固定LF，
沿用同文件对uv锁文件的做法；不改变清单生成、seal或发布门。
测试覆盖开启/关闭自动换行，以及真实内容修改仍被拒绝。
发布、测试手册同步维护这个构建约束。

## Verification

- V-1：回归修复后2项通过。
- V-2：仅撤回属性规则后，Windows场景再次失败；恢复规则后2项通过。
- V-3：固定该worktree的PYTHONPATH运行
  `pytest backend/tests/test_build_manifest.py backend/tests/test_desktop_packaging.py -q`：
  41通过。
- V-4：`ruff check backend/tests/test_build_manifest.py` 全通过。
- V-5：`make check`通过，含87目录约束、31文档、版本一致及diff检查。
- 未固定PYTHONPATH的第一次模块检查导入了旧运行工作区的desktop模块，1失败；
  已纠正测试环境，没有为该环境差异修改源码。
- Windows正式产物及新主线全量CI须在合并后独立验收，本记录不宣称已发布。

## Regression test

路径：`backend/tests/test_build_manifest.py`。
使用真实Git检出、声明重写、manifest CLI和seal，断言无修改可封装，
真实修改拒绝，不mock清洁状态或降低发布要求。

## Pattern analysis

实际搜索 `core.autocrlf|eol=lf|emitDeclarationOnly`：
属性规则2处，回归测试2处，TypeScript声明配置1处。
锁文件已固定LF，本次补齐唯一被诊断确认的受跟踪声明输出。
没有修改其他换行规则或引入生成文件忽略策略。

## Open questions / Follow-ups

合并后由main CI自动递增版本，重新构建同SHA全部所选产物；
旧v0.4.26标签不移动，失败产物不用于生产升级。
