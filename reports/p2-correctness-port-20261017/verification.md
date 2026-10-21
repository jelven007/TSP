# P2 正确性修复验收（A4–A6）

日期：2026-10-17。依据：[上游对比报告](../upstream-comparison-20261017/analysis.md)
第一批 A4–A6。起始 HEAD 为 `a95cc9dc2997fcf3099040a9ab3e193f37a22bd4`，
分支 `fix/rustdx-valid-stock-universe`，应用版本保持 `0.4.2`。
本次为工作区变更，未提交、推送或部署；保留此前 A1 策略校验改动。
A2/A3 及报告第二、三批 P2 项目不在本次范围内。

## 实现与兼容

按 L3 聚焦修改核心调用链；现有插槽/扩展接口不能表达这些行为。
参考上游 #480、#462、#473，按本地数据和事务契约实现，没有整批 cherry-pick。

| 项目 | 修复前 | 当前行为 |
| --- | --- | --- |
| A4 缓存换代 | 预热遇到换代后退出，历史缓存为空，首个请求重新计算 | 明确区分 ready/empty/stale/failed；后台仅 stale 最多尝试三轮，发布未完成时下一轮前等 5 秒；前台单次，磁盘错误不反复重算 |
| A5 固定卖出 | 大金额换算超过可卖仓位，被整单拒绝 | 取金额换算量与「T+1 可卖 − pending 卖单」的较小值，按 100 股取整；零数量记录可卖不足原因 |
| A6 分钟质量 | 正数/有限值检查允许昨收 100、分钟 1800 生成 +1700% | 统一价格基准、历史证券状态和涨跌停校验；异常点收益置空，返回质量计数和覆盖率 |

- A4：重建仍走共享重计算槽和现有 generation 检查；等待发生在槽外。
  只有 ready 通知派生缓存，完成回调始终释放 warming 状态；重复启动不增加线程。
- A5：复用现有账户写锁/SQL 事务、持仓、待卖单和事件回执。持有 500 股且待卖
  200 股时，大金额规则只再卖 300 股。`side=auto` 的全平退出待办与重试保持原行为。
- A6：新增 `services/sector_minute.py`，两个消费方复用同一读取和计算路径。
  原始分钟价的前收参考按 `prev_close × prev_ratio / target_ratio` 转到目标日口径，
  目标日以后的共同复权因子相消；不混用原始前收与旧前复权分钟。
  使用历史 SCD2 状态、交易所分板块/ST 规则、分位取整及已发布交易日历，
  正常涨跌停和新股前五个交易日合法大涨幅保留。
- 缺前一交易日价格时退化为当日首个有效分钟价；旧分钟价受非 1 复权因子影响时
  同样退化，并记录规则未知。证券历史、上市日、日历或价格口径不足时保留数据、
  标明未知，不套用固定 ±30% 阈值。历史文件损坏返回 `minute_reference`。
- 异常时间点保留，收益与对应成交额置空；成分分时不向前填充越过异常。
  轮动保留整桶异常缺口，最新时间不倒退到旧有效桶，领涨和切换强度返回 null。
  全部成分异常返回 `empty/minute_anomalies`，有其他有效板块时，全异常板块仍在
  自定义选择器中以空收益显示。
- 前后端契约同步：成分分时增加可选 `quality/coverage/excluded_rows`，
  轮动增加质量与成分覆盖字段。现有字段保持名称；轮动 `leader/rotation`
  对无有效数据的桶允许 null，外部消费者也应处理空值。存量无新增字段的响应仍可展示。
- 两个结果缓存键增加数据目录维度，复用原 TTL/失效入口；其他成分映射缓存未改。
  没有新增 provider、配置迁移、原始数据清洗或批量覆盖操作。

质量字段口径：

| 字段 | 范围与含义 |
| --- | --- |
| `quality.total_rows` | 整个股票分钟分区的原始行数 |
| `quality.valid_rows` | 可参与计算的行数，包含规则未知但价格为有效正数的行 |
| `quality.excluded_rows` | 分区总行数减参与计算行数，包含异常价格及错日期记录 |
| `quality.unknown_rule_rows` | 目标日缺少足够校验依据的行；可与异常行计数重叠 |
| `quality.unlimited_rows` | 目标日处于已确认无涨跌幅限制窗口的行 |
| 顶层/板块 `excluded_rows` | 当前成分集合内剔除的行数 |
| 成分分时 `coverage` | 最新时间点参与均值的成分数 / 全部成分数；正常停牌填充值可参与 |
| 轮动 `coverage` | 最新桶有有效分钟数据的成分数 / 全部成分数 |

覆盖率反映数据可用性，不等于价格规则全部已确认。

## 验证

环境：macOS arm64，现有 Python 3.11 虚拟环境；后端使用
`.tools/bin/uv run --no-sync`，未改变依赖。样本均在临时目录创建，不读取或覆盖生产行情。

先复现再修复：

- A4/A5 首批新增回归在旧实现上为 **7 failed, 35 passed**。
- A6 首批新增用例在旧实现上有 **18 failed**。
- 收尾新增“末桶全部异常”用例先观察到 `as_of=09:35`，预期 `09:40`；
  新增“全部成分异常但市场其他股票有效”用例先观察到原因误报 `no_member_bars`。
  两者随后修复，纳入最终定向集合。

最终定向命令（在 `backend/` 执行）：

```bash
../.tools/bin/uv run --no-sync pytest \
  tests/test_sector_minute_quality.py tests/test_sector_rotation.py \
  tests/test_dimension_intraday.py tests/test_ext_data_dimension_members.py \
  tests/test_enriched_warmup_retry.py tests/test_repository_cache_dependencies.py \
  tests/test_enriched_generation.py tests/test_enriched_range_warming_guard.py \
  tests/test_enriched_history_window_cache.py tests/test_enriched_refresh_memory.py \
  tests/test_paper_auto.py tests/test_paper_trading.py tests/test_full_paper.py \
  tests/test_paper_risk_policy.py tests/test_paper_t1_available_at_order.py \
  tests/test_minute_adjust.py tests/test_instrument_history.py tests/test_price_limits.py -q
```

最终结果：**248 passed, 15 skipped, 2 warnings**，18.47 秒。
15 项数据库用例需要 `TSP_TEST_POSTGRES_DSN`，本地未配置，跳过不算通过。

实现主体完成后运行后端全量 `../.tools/bin/uv run --no-sync pytest -q`：
**3329 passed, 6 failed, 117 skipped, 89 warnings**，226.93 秒。
这次全量早于最后新增的 API/边界用例和收尾修改；最后状态以定向验证为准。
六项失败身份与 [A1 基线核验](../strategy-safety-port-20261017/verification.md) 相同：

| 用例 | 本次全量表现 |
| --- | --- |
| `test_auction_snapshot.py::test_instrument_normalization_drops_delisted_labels` | 结果多出 `board` 字段 |
| `test_auto_mining.py::test_auto_start_contract_with_pool` | 503，预期 200 |
| `test_enriched_incremental_memory.py::test_incremental_batches_preserve_values_and_generation[False]` | 增量结果多出 `quote_time_basis` 列 |
| `test_openapi_contract.py::test_open_contract_snapshot` | 快照缺少已有 `/api/paper/auto_skips`、`/api/paper/rounds` |
| `test_openapi_contract.py::test_open_contract_schema_fingerprint` | schema 指纹与基线不同；本次新增响应字段也改变了指纹 |
| `test_watchlist_ocr.py::test_add_batch_reports_net_new` | 返回 3，预期 2 |

不能将指纹项完全归为无关失败。本次未更新整份旧快照以吸收其他差异；
新增真实 HTTP 序列化测试验证成分分时成功/全异常/基准失败与轮动空桶响应。
整仓质量门尚未通过，合并发布前仍需核对并更新契约基线及处理其余失败。

前端 `pnpm test`：**32 文件、160 项通过**。最后组件变更后再次 `pnpm build`：
**通过**，仅保留已有大于 500 kB 的分包提示。

静态检查：

- P2 修改的 9 个 Python 文件按「文件、规则、消息及出现次数」与 HEAD 对比，
  Ruff 告警 **123 → 122，无新增**；新服务、三处相关测试和卖出/轮动服务均无告警。
  剩余 122 项属于 `api/ext_data.py`、`tickflow/repository.py` 存量告警。
- 4 个前端文件 ESLint 与 HEAD 对比：**0 错误，4 条已有 Hook 依赖警告，无新增**。
- `git diff --check` 通过；没有清理无关存量告警。

## 合成样本性能

同一机器、同构临时样本，使用真实指标计算。A4 对照将新后台入口替换为单次刷新，
在首次计算注入 generation 变化；A6 对照从 HEAD 提取原分钟收益函数。
不代表真实生产并发、全市场数据或冷磁盘性能。

| 场景 | 修复前/单次刷新 | 修复后 |
| --- | --- | --- |
| 200 股、37,600 日线行，预热后历史缓存 | 未就绪 | 就绪 |
| 同样查询首次耗时 | 190.919 ms | 0.404 ms |
| 后续两次查询 | 0.359 / 0.294 ms | 0.364 / 0.425 ms |
| 预热加三次请求总计算次数 | 2 | 2 |
| 1,000 股、240,000 分钟行，三次收益读取 | 15.873 / 12.826 / 11.596 ms | 40.710 / 26.336 / 27.500 ms |

A4 将原先落在首个请求的重算移到后台，没有减少此次样本的总计算次数。
A6 增加证券历史、复权参考和规则校验，读取耗时有所增加，继续由既有结果缓存
摊销；未声称分钟校验提升性能。预热墙钟受运行顺序/JIT 影响，不用于提速结论。

## 界面与文档

通过独立 Vite 验收入口挂载真实 `SectorRotationCard` 和成分弹窗，
使用临时样本生成的 API 结果及明确的末桶空值夹具，未操作真实运行数据。
已检查部分异常、全部异常、规则未知、读取失败和末桶空值；浅/深色及
390 px 容器完成检查。截图是组件隔离验收，不代表线上完整页面验收。

- [成分覆盖率与剔除提示（浅色）](./dimension-quality-light.png)
- [全部异常（深色，390 px 容器）](./empty-dark-narrow.png)
- [末桶空值（深色）](./last-bucket-dark.png)

同步文档：`docs/features.md`、`docs/paper-trading-plan.md`、
`docs/architecture.md`、`docs/testing.md`。

## 剩余边界与回退

- 未执行真实 PostgreSQL 事务验收、Linux/Windows 验收、新镜像构建或生产部署。
- 未知历史/基准数据仍可参与计算并有明确提示；不能宣称所有异常已被自动识别。
  当前只按价格与已知交易所涨跌幅规则校验，不覆盖全部特殊交易制度或所有数据字段。
- 连续三轮都发生换代时退出预热并记录日志，后续请求仍可能承担冷缓存计算。
- 回退本项服务、前后端契约及文档即可，无数据迁移；应保持两处分钟消费方与共用
  服务同时回退，避免缺少导入。回退会恢复原有换代冷缓存、超额卖出拒单与异常污染风险。
