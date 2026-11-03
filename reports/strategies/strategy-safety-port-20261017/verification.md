# P1 策略代码校验修复移植验收

日期：2026-10-17。范围：上游 GH #475 的策略 AST 校验和加载失败处理。

## 来源与改动

- 上游：`shy3130/tick-stock-panel`，修复提交
  `b22a970097f99bdece126bc1d9e93e6ad71beaed`。
- 本地功能分支：`fix/rustdx-valid-stock-universe`；开始时 HEAD
  `c49a289b643fdacdba80c65066b1809332e5bf04`。
  并行版本工作完成后，验收 HEAD 为
  `a95cc9dc2997fcf3099040a9ab3e193f37a22bd4`，应用版本仍为 `0.4.2`。
  本项修复保留为工作区变更，未提交、推送、部署或修改版本。
- 按 L3 直接修改核心：现有扩展点无法替代校验器和模块加载前的安全判断。
  定向移植，不整体 cherry-pick 上游提交。

修改文件：

- `backend/app/strategy/ai_generator.py`：危险内建从直接调用检查改为全部 Load
  引用检查；封堵别名、dunder 属性及常量拼接、模块对象句柄、危险文件/进程
  属性和通配导入。额外拒绝相对导入与 `from` 导入 dunder 符号，防止它们避开
  模块白名单及 Name/Attribute 检查。
- `backend/app/strategy/engine.py`：辅助文件不再特批 `vars()`；
  读取、解码、语法或校验异常停止加载，不能继续创建并执行模块。
- `backend/tests/test_strategy_safety_validation.py`：64 个回归用例，危险载荷
  仅供 AST 解析；加载失败用例在创建模块前设置模拟阻断，未执行危险载荷。
- `docs/strategy.md`、`docs/testing.md`：同步禁用能力、兼容边界和测试命令。

保留本地 `app.strategy.builtin.factor_rank_research` 导入路径、META 解析、
策略注册、provider 路由与租户隔离。未更改 API 字段和前端。

## 复现与验证

环境：macOS arm64、现有后端 CPython 3.11 虚拟环境；使用仓库
`.tools/bin/uv`，`--no-sync` 避免同步或改变依赖。

旧代码上先运行新增测试：

```bash
cd backend
../.tools/bin/uv run --no-sync pytest tests/test_strategy_safety_validation.py -q
```

当时 62 项用例结果为 **49 failed, 13 passed**。
修复后同命令对最终 64 项用例结果为 **64 passed**。
覆盖 23 个实际内置策略文件；另验证合法 Polars/矩阵代码、行情 `market.open`、
本地因子研究入口、类型注解、主程序 guard、文档字符串及合法辅助依赖。
测试不扫描或读取真实 `data/strategies/`。

最终受影响调用链定向回归：

```bash
../.tools/bin/uv run --no-sync pytest \
  tests/test_strategy_safety_validation.py \
  tests/test_strategy_code_save.py tests/test_strategy_registry.py \
  tests/test_ai_generator_prompt.py tests/test_strategy_market_data.py \
  tests/test_strategy_build_stream.py tests/test_strategy_publish.py \
  tests/test_strategy_scoring.py tests/test_full_compute.py tests/test_full_ai.py \
  tests/backtest/test_factor_rank_research.py tests/backtest/test_matrix_strategy.py -q
```

结果：**153 passed, 11 skipped**。
11 项真实 PostgreSQL 用例因未配置 `TSP_TEST_POSTGRES_DSN` 跳过，不算通过。
无数据库的租户入口测试确认危险代码在计算服务调用及私有存储写入前被拒绝。
HTTP 测试确认保存失败不落盘、拒绝更新不改变旧文件或运行时策略。
Compute 边界测试确认容器仍禁网、只读、非 root、限制资源且不挂载凭据。

后端全量：

```bash
../.tools/bin/uv run --no-sync pytest -q
```

结果：**3294 passed, 6 failed, 117 skipped, 89 warnings**，耗时 182.23 秒。
全量运行收集的是新增 64 项之前的 62 项版本；新增两项随后在上述最终定向
回归中通过。全量失败均经当前实现独立复测和修复前实现复测确认：

| 基线失败用例 | 相同失败表现 |
| --- | --- |
| `test_auction_snapshot.py::test_instrument_normalization_drops_delisted_labels` | 返回新增 `board` 字段，预期字典未包含 |
| `test_auto_mining.py::test_auto_start_contract_with_pool` | 自动挖掘启动返回 503，预期 200 |
| `test_enriched_incremental_memory.py::test_incremental_batches_preserve_values_and_generation[False]` | 实际增量结果多出 `quote_time_basis` 列 |
| `test_openapi_contract.py::test_open_contract_snapshot` | 快照缺少 `/api/paper/auto_skips` 和 `/api/paper/rounds` |
| `test_openapi_contract.py::test_open_contract_schema_fingerprint` | 当前 schema 指纹与已有快照不同 |
| `test_watchlist_ocr.py::test_add_batch_reports_net_new` | 净新增计数返回 3，预期 2 |

修复前复测方式：独立 Python 进程从 `git show HEAD:<path>` 读取并在内存中
恢复原 `ai_generator` 和 `engine` 模块，然后运行这六个用例；未回滚或改写
工作区。六项再次全部失败，表现与当前代码独立复测一致。
这是对失败用例的基线核验，不代表已运行完整基线测试。

静态检查：

```bash
../.tools/bin/uv run --no-sync ruff check \
  app/strategy/ai_generator.py app/strategy/engine.py \
  tests/test_strategy_safety_validation.py
git diff --check
```

新增测试 Ruff 全通过。两个既有模块的告警从 61 条降至 59 条；
按文件、规则和消息计数比较，没有新增告警；本次修改行也无告警。
保留既有中文标点和其他历史告警，未全文件格式化。
`git diff --check` 通过。

## 影响与限制

- 校验在生成、保存和加载路径执行，不进入每次实时计算热路径；
  模块属性探测按模块名缓存。未提出性能提升主张。
- 拒绝保存或更新发生在源码写入及 reload 前，旧文件和已加载策略保持原状。
  成功保存继续使用既有缓存失效和注册更新流程。
- 旧策略若使用危险内省、dunder、文件读写或模块句柄，现在会明确拒绝；
  应改用显式参数、策略上下文和公开行情接口。合法辅助模块仍可加载。
- AST 仅封堵已知模式，不是完整沙箱。多租户私有策略继续在受限容器运行；
  单机文件加载仍在本进程执行。模块属性探测失败沿用上游静态名单兜底。
- 未执行生产部署、真实租户数据库验收及新镜像构建；未改前端，因此未运行
  前端构建。不将本次结果表述为整仓质量门通过或生产安全隔离已完全验收。
- 回退应通过撤销本项代码和文档变更完成，不涉及数据迁移；回退也会恢复
  已知校验漏洞。发布前仍须处理上表既有全量测试失败。
