# S02 已取消成交额门槛的模板与说明修正

- 问题：已发布0.4.55示例仍含`amount_mean_20d >= 1.0`，套用后会恢复已取消的1亿元门槛。
- 当前契约：`overlay_filter=[]`；低成交额或缺成交额本身不影响S02预选、入池及买入资格。
- 调用链：示例覆盖 → `signal_sections.select_rows` → `overlay.effective_conditions` →
  `overlay.filter_minute_candidates` → 保存预选 → 分钟引擎只消费保存成员。
- 修正：清空示例叠加，配套信号同步已核实的0.8%涨幅下限及30秒确认；
  当前文档与历史记录区分。通用因子及显式历史条件保留。
- 合并适配：保留0.4.55独立示例ID`s02_atr14_example`与ATR参考日兼容处理；
  回归使用该ID、合成行情和隔离交易日历，不读取运行数据。
- 已复现：在原0.4.55模板上，新用例2失败、3通过；
  低成交额成员2只变0只，缺成交额触发`入场叠加条件缺少字段: amount_mean_20d`。
- 修正后验证：隔离环境141项后端回归通过，覆盖预选/买入成员保留、其他数值过滤、
  显式历史叠加、保存ATR、信号分区、共享因子及早盘回测。
  `make check`通过（87个结构路径、46份Markdown、8个需求模块及0.4.55版本）；
  backend工作目录下Ruff及CI增量Ruff通过，无新增告警。
- 发布边界：以0.4.55为基线提交合并；后继版本由main CI递增，已有标签保持。
  本任务不部署、不写运行配置、不重算历史或运行资金回测。

本轮后端命令（backend目录，独立DATA_DIR，`TICKFLOW_ENV_FILE=/dev/null`、
`MULTITENANCY_ENABLED=false`、`SERVICE_ROLE=standalone`、`POLARS_MAX_THREADS=4`）：

```bash
python -m pytest tests/test_s02_current_selection_contract.py tests/test_s02_saved_atr_entry.py tests/test_s02_selection_missing_atr.py tests/test_s02_selection_priority.py tests/test_minute_signal_sections.py tests/test_strategy_overlay.py tests/test_atr14_shared_factor.py tests/test_factor_registry.py tests/backtest/test_morning_backtest.py -q -p no:cacheprovider
```

闭环核对使用`git grep -n overlay_filter -- backend/app/strategy/overlay.py`及
`git grep -n -A22 'def select_rows' -- backend/app/strategy/signal_sections.py`：
覆盖由`effective_conditions`读取并传入`filter_minute_candidates`；新测试由pytest收集。
上述结果是本次本地验证，不替代PR或主线CI，也不表示两端已经部署。
