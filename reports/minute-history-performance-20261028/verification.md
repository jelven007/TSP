# 历史K线获取与数据修正性能修复与验证

日期：2026-10-28。源码基线：`700d25dad13b7b12c678e4443a893886299117a8`，
应用版本0.4.35；验证阶段未改号、提交、推送或部署，后续提交及推送以Git记录为准。
结论：确认5天分块及共用Provider从最新页反复扫描两个问题。
最终修复覆盖历史获取、全量/遗漏修正、股票/指数/ETF共用历史分页；
月度样本分页调用1344→128，遗漏修正432→176，输出内容一致。
第一阶段记录保留在下方，最终实现及第二阶段实测见“全面检查与最终修复”。
不能据此保证全市场所有月份均在十几分钟完成。

## 线上与本地的实际对照

取证时本地运行0.4.35，ECS运行0.4.34；二者原分钟分块实现相同。
数据源均为rustdx，股票池均5222只。ECS使用35工作线程/35连接、
`RUSTDX_MAX_PAGES=64`、8个Polars线程，Platform限8CPU/24GiB。
主机空闲内存约238GB、磁盘余102GB、无swap，未发现内存或磁盘空间不足。

| 已完成任务 | 实际闭区间 | 耗时 |
| --- | --- | --- |
| 本地1个月 | 2026-09-09～2026-10-09 | 25.23分钟 |
| ECS1个月 | 2026-09-15～2026-10-15 | 24.87分钟 |
| ECS再向前 | 2026-08-15～2026-09-15 | 48.31分钟 |
| ECS再向前 | 2026-07-17～2026-08-17 | 76.64分钟 |
| ECS再向前 | 2026-06-17～2026-07-17 | 105.92分钟 |
| ECS再向前 | 2026-05-17～2026-06-17 | 122.15分钟 |

“向前扩展1个月”使用本地最早非空分区作为锚点，重复点击会换到更早月份，
两端已有覆盖不同也会产生不同窗口。已有任务保留批次日志中，ECS取数中位数
从近期月约1.99秒/100证券升至较早月19.06秒，保存约0.22～0.31秒。
初次取证时ECS另有更早月份任务运行；最后只读复核时，该任务
`a18e81f8f3`已于04:04:19 UTC转为cancelled，阶段进度17%，没有活跃任务。
本地同类深历史任务也已取消。本轮没有发出取消或重启操作。

## 第一阶段：分块根因与修复

`sync_period`原来每5自然日一次调用Provider，每次按100证券取数。
rustdx的TDX接口以相对最新数据的偏移量请求，每次`_bar_records`从0开始、
每页最多800根，持续向前直到起点。一个月被拆成6～7次重复近期前缀扫描，
更早月份还须扫描更多近期页面；不少下载数据最后被日期过滤丢弃。

第一阶段修改`backend/app/services/kline_periods.py:383-385`，将1分K日期段扩为最多32天，
覆盖最长闭区间自然月，同时保留100证券批次；日K及本地派生分块不变。
32自然日每股理论至多7680个分钟标签，低于TickFlow的10000单股上限；
实际交易日更少。此数字是有界设计预算，不是实测全市场峰值内存。

数据仍经原`fetch_period/persist_period`、Provider、时区和价格基准、
`.minute-write.lock`原子合并、视图刷新及公共缓存失效。
无新API/Provider协议、配置或Parquet迁移，前端无需修改。

## 第一阶段：同环境修改前后实测

同8证券：000001.SZ、000002.SZ、000063.SZ、000333.SZ、
600000.SH、600036.SH、600519.SH、601318.SH。
同区间：2026-06-17 00:00:00至2026-07-17 23:59:59.999999。
调用真实rustdx/TDX；只把新取得的样本写入临时分区，
关闭临时副本的公共缓存刷新，未写生产数据。
ECS“后”使用独立Python进程中的等价修复函数，运行服务保持原代码。

| 环境 | 修改前 | 修改后 | 取数前→后 | 保存前→后 |
| --- | ---: | ---: | ---: | ---: |
| 本地 | 19.594秒 | 4.945秒 | 19.447→4.760秒 | 0.146→0.184秒 |
| ECS | 38.662秒 | 7.983秒 | 38.614→7.940秒 | 0.046→0.041秒 |

| 数据/工作量 | 修改前 | 修改后 |
| --- | ---: | ---: |
| Provider窗口 | 7 | 1 |
| 协议请求（两端各自） | 1344 | 216 |
| 下载行数（两端各自） | 1075200 | 172800 |
| 从偏移0开始的请求 | 56 | 8 |
| 输出行数 | 42240 | 42240 |
| 日期分区 | 22 | 22 |

首轮四份输出及工作区实际修复函数复测的SHA256均为
`ec1751dd6ee5310269b5269e2dabc73665bd3303773a4d72f06889afc2de3c7d`。
工作区复测本地耗时7.503秒，仍为216请求/42240行，说明实时取数存在波动；
上表给出首轮各环境前后记录，不把单次最好耗时当作稳定SLA。
原始JSON、请求/保存分阶段数据及脚本见[evidence](evidence/benchmark.json)。
网络及盘中负载存在波动；没有单独隔离RTT、解析和共享源竞争，
所以只确认重复工作量，不断言ECS网络是唯一剩余差异。

## 第一阶段：回归与验证命令

统一使用`TICKFLOW_ENV_FILE=/dev/null MULTITENANCY_ENABLED=false SERVICE_ROLE=standalone
POLARS_MAX_THREADS=2`及独立临时`DATA_DIR`，显式`PYTHONPATH`指向待验证后端源码。
后端命令为`uv run --frozen --no-sync`，依赖复用现有已冻结环境。

| 验证 | 实际结果 |
| --- | --- |
| 新请求预算测试在原实现连续3次 | 每次2 failed / 1 passed，请求70次而预算13次 |
| 修复后新测试 | 3 passed |
| 在测试进程重新载入原基线的反向验证 | 2 failed / 1 passed |
| 11个受影响服务/API/Provider/存储模块 | 274 passed / 23 warnings |
| Ruff：修改的生产文件和新增测试 | All checks passed |
| `make check` | 仓库87路径、文档31文件/8需求模块、0.4.35版本及diff全部通过 |

邻接回归命令：

```bash
uv run --frozen --no-sync pytest \
  tests/test_minute_history_request_budget.py \
  tests/test_kline_periods.py tests/test_kline_sync_progress.py \
  tests/test_kline_periods_api.py tests/test_repair_kline.py \
  tests/test_rustdx_contracts.py tests/test_rustdx_provider.py tests/test_rustdx_sync.py \
  tests/test_minute_partition_merge.py tests/test_minute_adjust.py \
  tests/test_kline_sync_timezone.py -q
uv run --frozen --no-sync ruff check \
  app/services/kline_periods.py tests/test_minute_history_request_budget.py
```

测试覆盖31/32天内容一致、协议预算、长范围/100证券边界、进度、
周期表隔离、API成功/失败/取消、Provider失败、复权及并发原子写盘。
第一阶段生产源码只有一个行为表达式变化，没有调试日志。
反向验证没有stash、checkout或回退任何工作区文件。

## 全面检查与最终修复

用户要求全面检查后，追踪历史获取、数据修正两种模式及公共Provider调用链：

| 路径 | 重复扫描与最终处理 |
| --- | --- |
| 历史获取 / 1分K全量修正 | `sync_period`使用32天段；共用分页定位历史结束边界 |
| 1分K遗漏修正 | `kline_gaps._requests`按连续缺口调用`fetch_period`；Provider定位各缺口所在页 |
| 股票/指数/ETF日K、Provider周K | 共用`_bar_records`定位；按日期包含结束整日 |
| 历史复权因子 | 事件前收盘价读取共用日K分页，固定数值回归保持一致 |
| 5/10/15分K、股票周K、指数周/月K | 服务使用本地基础K线生成，不新增远程请求 |
| 盘中最新分钟/批次/报价、证券目录验证 | 直接短偏移请求，不经过历史分页；既有预算不变 |

`RustdxProvider._bar_records`现在根据实际页的最早时间做指数探测和二分查找，
定位第一张可能包含结束边界的页，再连续读取至开始边界。
不按日历估算偏移，停牌和页内升/降序均按真实时间处理。
每页最多800根；原`RUSTDX_MAX_PAGES`默认64、最多80仍约束全局页偏移，
不会把定位后的64页解释为额外历史深度。
探测结果只在当前证券当前调用中复用，相同页最多下载一次；
不同修正调用重新取源，历史更正可立即可见。
重复/缺时间/失败探测安全回退有原保护的顺序扫描；
实际所需页失败仍拒绝结果，协作取消继续传播。

遗漏修正仍分别请求连续缺口，不跨已有键或午休合并窗口；
返回数据先与缺口键相交，`only_missing=True`在分区写锁内重读当前快照。
已有值和并发补入值不被覆盖，没有遗漏时零Provider请求。
`result.requests`继续表示逻辑缺口窗口数；以下分页计数为Python `bars`调用数，
不包括原生桥接内部重试，不把两个计数混用。

### 最终同环境实测

同上8证券，完整区间仍为2026-06-17～2026-07-17。
三个方案为原5天/顺序分页、32天/顺序分页、32天/定位分页。
本轮复测与第一阶段相隔一段时间，数值存在网络/源负载波动。

| 环境 | 原实现 | 只修日期段 | 完整修复 | 取数原→最终 | 保存原→最终 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 本地 | 26.993秒 | 5.042秒 | 4.201秒 | 26.808→4.053秒 | 0.184→0.147秒 |
| ECS临时进程 | 29.435秒 | 3.530秒 | 2.929秒 | 29.381→2.883秒 | 0.052→0.044秒 |

| 每环境工作量 | 原实现 | 只修日期段 | 完整修复 |
| --- | ---: | ---: | ---: |
| 分页调用 | 1344 | 216 | 128 |
| 下载行 | 1075200 | 172800 | 96000 |
| 输出行 / 分区 | 42240 / 22 | 42240 / 22 | 42240 / 22 |

所有月度结果SHA256仍为`ec1751dd6ee5310269b5269e2dabc73665bd3303773a4d72f06889afc2de3c7d`。
完整修复分页调用减少90.48%；ECS本轮样本耗时降低90.05%。
这些是8证券样本结果，不能线性推断5222证券完整任务时长。

遗漏样本为2026-06-17一天：先获取真实1920行，保留已有1896行且标记close=99，
每证券留下09:32、09:33、14:59三个缺口，再独立计时真实修正。
初始取样和预置写盘不计入修正耗时。

| 环境 | 原修正 | 完整修复 | 分页调用 | 新补 / 剩余 |
| --- | ---: | ---: | ---: | ---: |
| 本地 | 3.613秒 | 1.402秒 | 432→176 | 24 / 0 |
| ECS临时进程 | 3.152秒 | 1.055秒 | 432→176 | 24 / 0 |

下载行345600→116800，逻辑缺口请求仍为2个。
四份完整分区1920行及SHA256均为
`7f1b6d9d31a4f8fdb251d0132b6421213899095de1b3cca62716cca7ac30ab1b`，
同时证明已有标记值和所补真实值一致。
原始数据见[evidence/pagination-benchmark.json](evidence/pagination-benchmark.json)，
复现脚本及原分页源码快照作为工作区辅助证据保留；提交保留原始基准JSON。
ECS通过受控独立进程临时加载候选方法和32天分段，只写tmpfs临时Parquet；
运行服务、生产行情和用户配置没有加载或保存本次候选。

### 最终回归

新增24项`test_rustdx_history_request_budget.py`，覆盖相关资产/周期旧窗口、
准确缺口预算和已有值、无缺口零请求、UTC/日K结束日、未知量额、停牌、
空/短/重复/缺时间页、探测故障回退、目标页故障、取消及跨调用源更新。
原实现18项初版为10 failed / 8 passed；
测试进程仅恢复原分页反向验证也为10 failed / 8 passed。
后续补入取消测试时，测试替身曾把继承BaseException的取消对象当返回值，
修正替身后验证取消正确传播；生产实现未因此改变。
完整26文件邻接回归实际402 passed / 24 warnings，警告为既有Polars弃用/排序提示。
工作区文件与隔离候选逐字节相同。
同步后在实际工作区再次执行同一26文件回归：402 passed / 24 warnings（18.73秒）；
四个修改/新增Python文件Ruff全部通过。
最终工作区反向验证只选择10个性能预算用例，恢复原分页后10 failed / 14 deselected，
说明测试能检出本次根因，且没有回退工作区文件。
`make check`通过87个必需路径、31个Markdown文件/8需求模块、0.4.35版本及diff。
工作区辅助证据为同目录的`pagination-workspace-green.txt`、`pagination-ruff.txt`、
`pagination-workspace-reverse.txt`与`pagination-repository-checks.txt`。

```bash
uv run --frozen --no-sync pytest \
  tests/test_rustdx_history_request_budget.py tests/test_minute_history_request_budget.py \
  tests/test_kline_periods.py tests/test_kline_sync_progress.py tests/test_kline_periods_api.py \
  tests/test_repair_kline.py tests/test_rustdx_contracts.py tests/test_rustdx_provider.py \
  tests/test_rustdx_sync.py tests/test_rustdx_financial.py tests/test_minute_partition_merge.py \
  tests/test_minute_adjust.py tests/test_kline_sync_timezone.py tests/test_minute_timezone_contract.py \
  tests/test_minute_history_days.py tests/test_custom_provider_indices.py tests/test_daily_batch_asset.py \
  tests/test_index_daily_live.py tests/test_index_minute_history.py tests/test_index_kline_periods.py \
  tests/test_index_minute_api.py tests/test_etf_adj_factor.py tests/test_corporate_action_provider.py \
  tests/test_custom_intraday_batch_window.py tests/test_realtime_guard.py tests/test_write_lock_scope.py -q
uv run --frozen --no-sync ruff check app/plugins/rustdx/provider.py \
  app/services/kline_periods.py tests/test_rustdx_history_request_budget.py \
  tests/test_minute_history_request_budget.py
```

## 六类文档、责任与兼容

| 分类/维护责任 | 更新文件 | 当前事实及复核条件 |
| --- | --- | --- |
| 需求/数据负责人 | `docs/requirements.md` | FR-DATA-05全量/遗漏及相关资产请求预算，实际月份及范围验收 |
| 方案/后端负责人 | `docs/architecture.md` | 日期分块、页时间定位、单次复用与异常回退、100证券边界 |
| 开发/变更作者 | `CONTRIBUTING.md`、`docs/secondary-development.md`、`docs/reference/engineering.md` | L3最小变化、无新协议、原缓存/锁及升级复核 |
| 测试/验证负责人 | `docs/testing.md`、两份新增请求预算测试 | 全量/遗漏内容、请求数、源更新与异常、不用预测代替实测 |
| 运营/支持负责人 | `docs/release-operations.md`、`操作说明书.md` | 获取窗口随本地锚点变化，不承诺固定全市场耗时 |
| 运维/部署负责人 | `docs/operations.md` | Platform加载新源码后生效，等待旧任务终态 |

源码和日志事实变化立即复核，至少每月核对文档；源深度、批量上限或窗口变化
使本报告性能数字失效，必须重新测量。旧任务、旧分区、用户配置和价格基准保留。

## 发布及剩余验证边界

本轮已把修复和回归放入工作区供审查，未更新本地运行副本或ECS服务。
旧任务沿启动时原逻辑；最后取证时线上原任务已经取消，服务版本仍为0.4.34。
上线须依既有主线CI版本和固定产物流程进行，并让Platform加载匹配后端。
保留行情、账号、管理员、口令、SMTP及用户后续保存的配置；
本次没有修改这些配置，也没有重新做重启登录验收。

未执行新的全市场重拉，未验证全市场新耗时/峰值内存或Windows运行。
TDX默认最多64页/证券，历史深度有限，旧任务“成功”与百万行输出不认证完整覆盖；
取证中的更早任务曾出现大量零行批次，这需要逐证券/日期另验收。
修复减少月内重复分段及每个历史窗口的近期前缀扫描，
没有虚构更早历史、跨任务缓存或全量初始化完成。
