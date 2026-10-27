# 历史遗漏K线交易日历修复核对

核对日期：2026-10-26。源码基线：`6b86f724940682c802919c680e224a6d7510064e`，
应用版本0.4.31。修复位于独立工作目录的
`fix/historical-trading-calendar-20261026`分支，尚未提交、合并、发布或部署。
原主工作区并行研究及运行配置保留。

用户报错由内置日历仅覆盖2026年、备用指数日历仅近370天共同触发。
本轮补齐2019–2026年可靠官方日历；2019用于2020-01-01周K向前对齐2019-12-30。
不移除未知范围保护，不把周一到周五、停牌或缺行情当可靠日历。
根因、假设、复现和反向验证见[修复记录](../../.claude/artifacts/fixes/historical-trading-calendar.md)。

## 六类文档与实现

| 生命周期 | 本轮更新 | 关键口径 |
| --- | --- | --- |
| 需求 | [需求基线](../../docs/requirements.md)的FR-DATA-05 | 历史区间可检测，未知仍拒绝 |
| 方案 | [系统方案](../../docs/architecture.md) | 周一对齐、公布优先、源证据边界 |
| 开发 | [二开](../../docs/secondary-development.md)、[工程参考](../../docs/reference/engineering.md)、[数据接入](../../docs/reference/integrations.md#rustdx-日历与研究复现) | L3最小修复、官方来源、研究指纹 |
| 测试 | [验收指南](../../docs/testing.md)及四个测试文件 | 稳定复现、实际周K落盘、修订边界、旧快照 |
| 运营 | [发布与服务运营](../../docs/release-operations.md) | 源码/运行区分，升级后重试，覆盖另签收 |
| 运维 | [运维手册](../../docs/operations.md) | 待活跃任务退出、固定产物、保留配置/旧研究 |

生产代码仅修改[market_time.py](../../backend/app/market_time.py)，新增7个年度区间与
日历版本`SSE-2019-2026-20261026`。应用版本文件、锁文件、前端、Provider、
API、Parquet schema、用户配置均未修改。

## 实际验证

在隔离工作目录使用原主工作区已安装后端虚拟环境运行，测试输入位于临时目录，
不连接业务数据库、不执行全市场历史补采。

| 检查 | 实际结果 |
| --- | --- |
| `pytest tests/test_historical_trading_calendar.py tests/test_repair_kline.py -k 'actual_historical_request or historical_week' -q`，旧实现连续三次 | 每次4 failed；返回码均为1，失败消息与原报错一致 |
| 相同两模块，修复后全模块 | 113 passed，3条既有Polars弃用警告 |
| 保留新测试，进程中加载原v0.4.31日历，再跑原四项 | 4 failed，同一日历报错；证明回归能捕获原缺陷 |
| 下列26个受影响模块 | 549 passed、49 skipped、13 warnings，31.70秒 |
| 新增旧日历快照回归后的`test_data_release.py test_research_snapshot.py` | 33 passed，6.53秒 |
| `ruff check app/market_time.py tests/test_historical_trading_calendar.py tests/test_repair_kline.py tests/test_data_release.py tests/test_research_snapshot.py` | 通过 |

扩大回归实际命令：

```bash
python -m pytest \
  tests/test_historical_trading_calendar.py tests/test_repair_kline.py \
  tests/test_collection_contract_fixes.py tests/test_trading_day.py \
  tests/test_trading_minutes_elapsed.py tests/test_data_integrity.py \
  tests/test_data_release.py tests/test_research_snapshot.py \
  tests/test_health.py tests/test_function_health.py tests/test_full_runtime.py \
  tests/test_sector_minute_quality.py tests/test_market_scope.py \
  tests/test_full_market_provider_routing.py tests/test_paper_risk_policy.py \
  tests/test_paper_auto.py tests/test_paper_trading.py tests/test_full_paper.py \
  tests/test_paper_corporate_actions.py tests/test_full_research.py \
  tests/test_online_timeout_validation.py tests/test_kline_periods.py \
  tests/test_kline_periods_api.py tests/test_realtime_guard.py \
  tests/test_pipeline_and_monitor_fixes.py tests/test_final_sync_confirmation.py -q
```

该环境未配置一次性PostgreSQL的`TSP_TEST_POSTGRES_DSN`；相关用例跳过。
没有前端变更，不重复前端构建。源码回归不代表线上签收，也不代表实际行情完整。
对四个PostgreSQL相关模块补充`-rs`审计：7 passed、49 skipped，
全部跳过原因均为`requires a disposable PostgreSQL server`。

<a id="official-sources"></a>
## 官方日历来源

已逐项读取上交所原始公告全文；休市区间含首尾，周末无论行政补班与否均休市。

| 年份 | 最终休市区间（月-日） | 官方公告 |
| --- | --- | --- |
| 2019 | 01-01；02-04~02-10；04-05~04-07；05-01~05-04；06-07~06-09；09-13~09-15；10-01~10-07 | [年度](https://www.sse.com.cn/disclosure/announcement/general/c/c_20181220_4696473.shtml)、[劳动节修订](https://www.sse.com.cn/disclosure/announcement/general/c/c_20190418_4771364.shtml) |
| 2020 | 01-01；01-24~02-02；04-04~04-06；05-01~05-05；06-25~06-27；10-01~10-08 | [年度](https://www.sse.com.cn/disclosure/announcement/general/c/c_20191220_4969627.shtml)、[春节延长](https://www.sse.com.cn/disclosure/announcement/general/c/c_20200127_4991582.shtml) |
| 2021 | 01-01~01-03；02-11~02-17；04-03~04-05；05-01~05-05；06-12~06-14；09-19~09-21；10-01~10-07 | [年度](https://www.sse.com.cn/disclosure/announcement/general/c/c_20201224_5286949.shtml) |
| 2022 | 01-01~01-03；01-31~02-06；04-03~04-05；04-30~05-04；06-03~06-05；09-10~09-12；10-01~10-07 | [年度](https://www.sse.com.cn/disclosure/announcement/general/c/c_20211220_5662606.shtml) |
| 2023 | 01-01~01-02；01-21~01-27；04-05；04-29~05-03；06-22~06-24；09-29~10-06 | [年度](https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20221227_5714459.shtml) |
| 2024 | 01-01；02-09~02-17；04-04~04-06；05-01~05-05；06-10；09-15~09-17；10-01~10-07 | [年度](https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20231226_5733941.shtml) |
| 2025 | 01-01；01-28~02-04；04-04~04-06；05-01~05-05；05-31~06-02；10-01~10-08 | [年度](https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml) |
| 2026 | 原已实现区间保持不变 | [全年休市安排](https://www.sse.com.cn/disclosure/dealinstruc/closed/) |

2019年度原公告仅休5月1日，必须应用4月18日修订；
2020春节原至1月30日，必须应用1月27日延长；
2024年除夕2月9日休市，不能照行政假期漏掉。

## 兼容、运行与剩余边界

- 公布年份不请求网络日历；2018及更早、未公布未来年份继续要求可靠源证据。
- 原100证券请求上限、只追加写锁、取消、部分失败计数、价格/指标缓存契约保持。
- 日历版本/覆盖/内容哈希改变，新研究输入使用新身份。旧快照保留，须用其原构建回放；
  既有数据发布清单与新日历的研究指纹可能不一致，不能手动改写历史清单认证一致。
- 本地与线上运行进程未升级，本轮没有正式发布；更新运行代码后原任务才能使用新日历。
- 没有重置管理员、口令、SMTP、行情源或用户后续保存的配置。
- 未执行原多年补采任务；行情源支持的历史周期、实际行数、剩余缺口仍待真实获取后验收。
- 回滚使用原发布构建与匹配研究输入；回退后历史日历错误可能恢复，不删除用户行情。

## 最终检查

`make check`实际通过：87个必需路径、31个Markdown文件、8个需求模块及保留基线；
版本检查为0.4.31，`git diff --check`通过。没有手工修改应用版本。
修复前后2026年完整交易日集合相等。
新公布日历的SHA-256为`51fc950004885a1ea88d4c306a5ea2285e34ae3b2191412a5cad9c289f0336aa`。
最终逐文件diff未发现本轮范围以外的源码、前端、配置或运行数据修改。
