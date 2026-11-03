# 本地股票1分K、日K与Enriched清理记录

2026-10-27。按用户“清理本地环境里的1分K、日K等数据库”的要求，
已清空本地有效股票行情中的三类数据，恢复服务并完成核验。应用版本保持0.4.31。

| 数据 | 清理前行数 | 清理前文件数 | 清理后行数/文件数 |
| --- | ---: | ---: | ---: |
| 股票1分K | 88,922,873 | 141 | 0 / 0 |
| 股票日K | 3,684,692 | 724 | 0 / 0 |
| 股票日K Enriched | 3,684,692 | 724 | 0 / 0 |
| 合计 | 96,292,257 | 1,589 | 0 / 0 |

操作范围限定于
本地受控运行目录`$TSP_LOCAL_ROOT/data/`中的
`kline_minute`、`kline_daily`和`kline_daily_enriched`。
清理前无活动采集任务；临时暂停自动同步，停止本地8个服务后移动目标数据到
受控恢复目录，建立空目录，按既有Enriched发布协议更新stock generation，
将初始化状态标为`pending`，发布新的数据清单和公共行情快照后重启。
没有调用会清除其他行情、财务和业务数据的全局`/api/data/clear`。

实际验证：

- 源目录及`public/current`下三类目标数据均为0文件、0行；
  DuckDB按“无Parquet不建视图”的现有约定校验为0行。
- 管理员登录后的`/api/data/status`中`minute/daily/enriched`均为`null`；
  上轮已清理的`5m/10m/15m/weekly`也继续为`null`，两处目录均为0文件。
- 当前公共清单没有上述七类表的数据条目，stock generation与源标记一致。
- Platform、Compute、API、前端及Research/Realtime/Notifications/Scheduled
  全部运行；前端HTTP200；管理员登录成功。
- API与Platform的`/health/ready`均HTTP200、`ready=true`、
  `status=degraded`。空股票数据使指标缓存不可用；没有将该状态称为数据初始化完成。
- `initial_sync.json`真实状态为`pending`；恢复后无活动采集任务。
- 清理前后，非目标Parquet的2,438个文件大小与修改时间清单完全一致。
  ETF日K及Enriched各242文件、61,779行；指数日K及Enriched各242文件、
  134,000行；财务20文件、746,306行；股票目录1文件、5,222行。
  股票/ETF复权因子仍为13,552/28行；源与当前公共快照对应数据一致。
- 全局自动同步恢复为原值`true`，实时行情仍为`true`；
  完整偏好内容摘要与清理前一致，六周期同步配置与清理前完全一致。
  受控运行环境及其他配置在停写清理阶段内容摘要一致。
- 没有重置账户、策略或SMTP配置，也没有操作线上环境、提交、发布或改应用版本。
  工作区已有源码和并行研究文件保留。

数据发布清单为`1ba66c455b2446b5835767553168e7e1`；
公共快照为`139e900047034626998f7bf53c7e15bd`；
stock generation为`c8f59dedd9f3487892797e8900bef2c6`。
受控恢复副本位于
`$TSP_LOCAL_STATE/backups/stock-base-klines-20261027-004826`。
历史公共快照、研究输入和恢复副本继续保留，未宣称磁盘空间全部释放。

维护启动入口继续沿用上轮的09:00前官方目录恢复条件：
仅接受来源为交易所、完整且不早于上一已完成交易日的目录，保留真实
`as_of=2026-10-26`及当天下载失败记录。
本轮额外校验明确清理成功、三类源数据为空、初始化为`pending`、
当前公共清单与服务快照ID一致后，允许空股票快照启动消费者；
公共快照存在和Platform就绪条件仍保留。仅调整本地维护启动入口，
没有修改安装版应用源码或正常启动器。

执行与验证命令：

```text
backend/.venv/bin/python -m py_compile "$TSP_WORK_TMP/tspp-clear-stock-base-klines-20261027.py" "$TSP_WORK_TMP/tspp-local-maintenance-launcher-20261027.py"
backend/.venv/bin/python "$TSP_WORK_TMP/tspp-clear-stock-base-klines-20261027.py"
backend/.venv/bin/python "$TSP_WORK_TMP/tspp-verify-stock-base-cleanup-20261027.py"
backend/.venv/bin/python "$TSP_LOCAL_STATE/full-dev.py" status
```

清理及验证进程均退出0，验证结果为`verified`。
上述长任务通过受控临时脚本`tspp-background-20261026.py`后台运行并保存完成状态；
验证包含真实磁盘扫描、API调用、偏好内容摘要和保留数据行数核对。

自动同步设置保留原值，后续交易时段、盘后任务或自动模式下再次启动的首次同步
可能重新生成行情数据。本次完成的是当前有效股票K线清空。
此前四类周期清理见[上轮记录](verification.md)。
