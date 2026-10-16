# 初始化默认配置验收

日期：2026-10-17。基线：`e998a1c`，应用版本 `0.4.2`。

## 变更与兼容性

- 按当前数据页参数固化初始化默认值：分钟自动同步开启；市场环境盘后自动计算开启；
  市场环境每批回填 120 个交易日、预热 40 个日历日。
- 其余已匹配的默认值沿用，完整参数表见
  [配置文档](../../docs/configuration.md#数据页初始化默认值)。
- 前端数据卡片、设置初始值、市场环境“默认”快捷预设与后端一致；
  盘后时间的前端缺省值同步为 15:35。
- 竞价、盘中异动沿用进入页面自动查询；偏移异动缺省开启，
  个股详情共享同一开关和查询缓存。
- 已保存的参数与主动关闭状态优先；不重写已有偏好或行情数据。
- L3 最小源码调整：既有 preferences getter 与前端缺省值直接决定初始行为。
  API 字段、存储结构、数据源路由和缓存键保持兼容。

## 自动验证

修复前运行新增回归：后端 5 项失败，前端 2 项失败，均复现旧默认值行为。
修复后：

| 命令（在对应 backend / frontend 目录执行） | 结果 |
| --- | --- |
| `uv run --no-sync pytest tests/test_preferences_cache.py tests/test_preferences_concurrent_write.py tests/test_regime_builder.py tests/test_initial_sync.py tests/test_auction_snapshot.py -q` | 106 通过，1 项基线失败 |
| `uv run --no-sync pytest tests/test_pipeline_pull_types.py tests/test_pipeline_beijing_today.py tests/test_pipeline_capacity.py tests/test_minute_refresh.py tests/test_ext_presets_startup.py tests/test_quote_interval_settings.py -q` | 66 通过 |
| `pnpm test` | 32 个文件、160 项通过 |
| `pnpm build` | TypeScript 检查与生产构建通过；存在既有大包提示 |
| 变更 Python 文件 Ruff 与基线比较 | 前后均为 41 项既有告警，无新增 |
| `python scripts/check_version.py` | 版本一致，`0.4.2` |
| `git diff --check` | 通过 |

测试覆盖无偏好文件、已有偏好缺字段、保存关闭状态后重读、非法回填批次兜底；
前端覆盖三个异动页签首次自动查询、无历史快照、主动关闭、重新开启及卸载重挂后保留旧结果。
后端使用已有虚拟环境执行测试；本次未变更依赖。

基线失败为
`test_auction_snapshot.py::test_instrument_normalization_drops_delisted_labels`：
证券标准化已输出 `board` 字段，但旧测试的完整字典预期未包含该字段。
在隔离的 `e998a1c` 检出中复测，同样失败；不由本次默认值变更引入。

## 页面联调

使用新构建产物，通过本地现有服务及浏览器真实点击验证：

- 分钟K设置显示“自动同步已开启”、5 天、20 个交易日/段。
- 盘中异动进入即加载，当前过滤结果 487 行。
- 竞价异动进入即查询，当前显示“尚无可用竞价历史”和首次交易日采集说明。
- 偏移异动进入即显示“监控中”，开关 `aria-checked=true`，当前加载 139 行。
- 当前窄屏视口 592 × 714；偏移开关边界位于视口内，可操作。

限制：截图工具恢复后重试仍超时，未取得可归档截图；本轮未完成桌面尺寸的视觉验收。
页面状态与操作通过 DOM 和实际查询结果确认，不能据此宣称完整视觉验收通过。

## 回滚与交付

回滚本次提交即可恢复旧缺省值；没有配置迁移或数据覆盖。
本次为功能分支本地提交，不更新 `main`；后续更新主线时按版本规则递增。
