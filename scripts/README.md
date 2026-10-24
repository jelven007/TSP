# 仓库脚本

根`make check`统一目录、文档、版本与diff检查；对应质量门见[生产工程](../docs/reference/engineering.md#engineering)。

仓库级 CLI 保留在此目录；依赖 `app` 的运维、验收和分析模块见
[backend/scripts](../backend/scripts/README.md)。实际目录与迁移表见
[工程目录](../docs/reference/engineering.md#layout)。

| 位置 | 用途 | 入口示例 |
| --- | --- | --- |
| 根目录版本工具 | 格式、一致性、主线 CI 递增、标签与发布准备 | `python3 scripts/check_version.py --current VERSION` |
| `build_manifest.py`、`upgrade_check.py` | 构建清单与只读升级预检 | `python3 scripts/upgrade_check.py <目标Tag>` |
| `check_docs.py` | Markdown链接/锚点/围栏、八模块/需求编号 | `python3 scripts/check_docs.py` |
| `check_repository.py` | 检查源码目录和关键入口 | `python3 scripts/check_repository.py` |
| `acceptance/` | 候选镜像构建、计算验收、持续验收启动 | `python3 scripts/acceptance/build_full_candidate.py --help` |
| `release/` | 最新及五个历史源码归档、清单和离线核验 | `python3 scripts/release/source_recovery.py --help` |
| `performance/` | 两个工作区的内存预算对比 | `python3 scripts/performance/benchmark_memory_budget.py --help` |
| `research/` | 策略试验、质量研究与结果汇总 | `PYTHONPATH=backend python3 scripts/research/research_quality_strategies.py --help` |

以上命令从仓库根执行。版本和发布工具的稳定路径继续用于 GitHub Actions，
本地不运行 `advance_version.py` 改号。脚本测试集中在 `tests/tooling/`。
构建与研究输出使用明确的本地路径，不能写入应用源码目录。

`release/source_recovery.py` 使用标准库和 Git；`create` 要求输出为新目录，
`verify` 检查清单及六份归档。它不更新版本、不删标签、不覆盖旧备份；
操作步骤见[版本保留](../docs/release-operations.md#31-版本保留)。
