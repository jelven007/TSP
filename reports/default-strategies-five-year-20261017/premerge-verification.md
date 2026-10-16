# 合并前验证

日期：2026-10-17。对应 [PR #1](https://github.com/jelven007/TSP/pull/1)。
本记录补充研究阶段的 `verification.md`，覆盖用户授权推送、合并后的质量门。
本地应用版本保持 0.4.2；远程 main 集成后由 CI 自动递增。

## 阻断项与修复

首次全量后端检查为 3300 passed、6 failed、117 skipped。逐项核对实际实现后修正：

| 失败项 | 原因与处理 |
| --- | --- |
| 标的标准化 | 预期字典漏掉已存在的 `board` 字段，补充精确值断言 |
| 自动挖掘 API | 测试应用缺少实际路由所需的 `strategy_engine`，补齐真实引擎夹具 |
| enriched 增量一致性 | 测试日线缺少存储契约中的可空 `quote_time_basis`，补齐输入字段 |
| OpenAPI 端点 | 快照遗漏已有的 paper rounds / auto_skips 两个端点 |
| OpenAPI schema | 更新上述端点与本次可空策略分类字段对应的完整 schema 哈希 |
| OCR 自选去重 | MagicMock 虚构账户身份导致误走数据库分支，改用真实 Request 验证单机存储 |

未修改这些业务模块以适配测试，未放宽去重、结果一致性或契约断言。
五个修复文件的定向回归为 63 passed；随后全量回归为
**3306 passed、117 skipped、89 warnings，318.38 秒**。

首轮 GitHub CI 的后端 Job 在依赖安装阶段失败：Cargo 无法匿名认证访问
`jelven007/rustdx`，尚未执行后端测试。
改为 crates.io 精确依赖 `jelven-rustdx-complete = "=1.12.2"`，
Cargo.lock 固定发布包 SHA256：
`8a880ef2ae2f2514d05ded71d8898f6de5af777c8dc29cb2f1d0e8824cf81747`。

发布包 `.cargo_vcs_info.json` 指向
`9dfc024c9662fa5091099c1f17610569ee2eca8b`。已逐字节核对全部 41 个 Rust 源文件及
`Cargo.toml.orig`，与旧固定提交 `904b33e` 一致；两个提交的差异仅为六个发布文档。
桥接和 Python 的来源标识同步为实际发布提交，版本仍为 rustdx 1.12.2 / bridge 0.1.1。
原生包的 uv 缓存键加入 Cargo 声明、锁文件和 Rust 源码，避免已有环境复用旧桥接。

## 实际验证

- 全量前端：164 passed；`pnpm lint` 零错误、77 条存量 warning；`pnpm build` 通过。
- 隔离的新 Python 环境：`uv sync --extra dev --frozen` 成功构建、安装默认桥接。
- `python -m app.plugins.rustdx.client` 成功，输出 1.12.2 / 9dfc024 / bridge 0.1.1。
- 切换发布包后的 installation/provider/contracts/sync/metadata 回归：113 passed。
- `cargo test --locked --manifest-path backend/native/rustdx_native/Cargo.toml`：
  6 passed；macOS 显式将 `PYO3_PYTHON` 指向项目解释器。
- 已有隔离环境再次普通 `uv sync --extra dev --frozen`：检测缓存键变化并重建桥接成功。
- 本次追加修改的 Python 文件：Ruff 原有 11 条、当前 11 条，新增 0 条。
- `scripts/check_version.py --current VERSION` 通过，应用仍为 0.4.2。
- `git diff --check` 通过。

全量后端及脚本命令（从仓库根目录）：

```bash
PYTHONPATH=backend POLARS_MAX_THREADS=4 NUMBA_NUM_THREADS=4 \
  backend/.venv/bin/python -m pytest backend/tests \
  scripts/test_versioning.py scripts/test_advance_version.py \
  scripts/test_version_workflows.py scripts/test_research_default_strategies.py -q
```

原生构建验证在隔离环境完成，没有替换正在运行的主工作区环境。
后续远程 CI 及合并状态以 PR 的当前检查记录为准；本记录不预先宣称远程检查通过。

## 兼容性与复核

结论：本地已发现的阻断项修复并验证；远程质量门通过后可合并。
已复核相对 main 的完整文件清单和本次追加 diff，保留既有六个前置提交的历史，
不纳入主工作区尚未提交的其他任务修改。
无需迁移用户策略、证券数据或账户；源码环境正常同步会重建桥接。
若手工管理旧 wheel，应按 `docs/rustdx-data-source.md` 重建。

研究的数据限制、适用范围与回滚方法仍以 `analysis.md`、`verification.md` 为准。
本记录不是 Windows 打包、实盘交易、生产部署或严格 PIT 验收。
