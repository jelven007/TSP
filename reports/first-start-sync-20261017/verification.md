# 首次启动数据源与四板初始化修复验证

首轮验证对应应用版本 `0.4.0`。下文追加的原生运行时修复与真实全市场验收
基于 `0.4.1`，在独立修复分支完成；历史验证结果保留。

## 修复结果

- 设置页允许选择、保存和套用默认 rustdx，即使原生运行时尚未安装；可用性仍如实显示，提供安装说明。
- Standalone 与 Platform 首次启动自动创建可查询的后台任务，API/Gateway 不创建公共采集任务。
- 在实时采集启动前保存初始化状态，避免实时日线使新部署被误判为已有完整数据。
- 使用完整沪主板、深主板、创业板、科创板有效股票池，强制同步一年日线、对应除权、历史财务与股本、配置窗口内的分钟线、当前行情和五档盘口。
- 财务初始化重试仍拉历史，不能用旧文件掩盖本轮失败。实时快照独立保存并纳入发布，写入共用仓库锁。
- 初始化仅在各阶段和数据发布成功后完成；失败保留 pending，可手动或重启重试。关闭服务时协作取消并等待启动线程释放资源。

## 实际验证

后端受影响回归：

```text
pytest tests/test_initial_sync.py tests/test_rustdx_sync.py \
  tests/test_rustdx_financial.py tests/test_financial_shares.py \
  tests/test_instrument_stock_pool.py tests/test_pipeline_beijing_today.py \
  tests/test_pipeline_pull_types.py tests/test_pipeline_capacity.py \
  tests/test_job_stall_and_cancel.py tests/test_daily_batch_asset.py \
  tests/test_pipeline_and_monitor_fixes.py tests/test_main_mining_lifespan.py \
  tests/test_data_release.py tests/test_full_platform.py \
  tests/test_minute_routing.py tests/test_minute_timezone_contract.py \
  tests/test_rustdx_contracts.py -q
249 passed, 20 warnings
```

最后增加快照写锁后，初始化、数据发布和 Platform 三组回归 `52 passed`。
警告来自既有 Polars streaming 参数弃用。

前端全量 `31` 个测试文件、`157` 项测试通过，生产构建通过。
受影响文件 ESLint 无错误，Data 页面保留既有三条 Hook 依赖警告；
构建保留既有大包提示。新增后端模块和测试 Ruff 通过；
修改文件与 HEAD 对比没有新增 Ruff 诊断，基线仍有 `109` 项诊断。
`git diff --check` 和应用版本一致性检查通过。

## 首轮本地联调与限制

浏览器在本地数据源页面确认七项 rustdx 路由可点击，默认标识和安装说明可见；
实际点击单项路由及 rustdx「套用」后，七项目标源保持 rustdx、状态仍为未就绪。

源码 Platform 重启后健康端点正常，首次自动任务确实创建，来源为 startup；
因本机缺少 `tsp_rustdx_native`，任务明确失败，保留 pending，未误报 0 行成功。
初始化成功路径使用四板 Provider 受控数据，实际执行目录校验、Parquet 持久化、
财务累积、分钟存储、指标计算及发布，不将这些结果声称为真实全市场验收。

首轮未完成真实行情全量验收，也未部署生产。该原生运行时缺失问题已在下述
追加验证中修复。

## 追加：原生运行时修复与真实全市场验收

验收时间为北京时间 `2026-10-17 04:03:57`，环境为 macOS arm64、
CPython 3.11、本地源码 Platform。已构建并安装 `tsp-rustdx-native 0.1.1`，
连接池接口为 `POOL_API_VERSION=2`，固定依赖为
`jelven-rustdx-complete 1.12.2`、提交
`904b33e514d4e368e633622c96a9e63af8a53d37`。
构建使用现有 Git 授权与 Git CLI 获取固定源码。wheel 的 SHA-256 为
`36b72a5aa896ba8ed801a6de4e99a435e41f7ea8c8fa4bbcdf5edbf37ad5f1c1`。

原生可用性检查通过，真实样本行情、日线和分钟读取成功。
运行中的 `/api/settings/capability-matrix` 七项能力均为
`effective=rustdx`、`usable=true`。

第一轮真实同步发现目录提前纳入 `001381.SZ` 皇冠新材和
`301718.SZ` 通则康威，两者前收盘价均为协议空值，日线为空，
上市日期、总股本和流通股本均为零，导致日线严格覆盖校验失败。
Provider 现仅对空前收盘价候选核验最近日线和上市信息；
确认待上市才排除，有上市证据、历史数据或缺失字段的证券仍保留。
核验协议失败时不更新目录，继续保护上一份有效快照。

修复后重新启动自动初始化任务 `a5ea1dbdfd`，任务终态为
`succeeded`、进度 `100`，`stage_errors=[]`、`skipped_stages=[]`。
全部数据通过真实 rustdx 原生协议获取，以下统计直接核对持久化 Parquet：

| 数据集 | 行数 | 股票数 | 覆盖 |
| --- | ---: | ---: | --- |
| 有效股票池 | 5,224 | 5,224 | 沪主板 1,702、深主板 1,495、创业板 1,409、科创板 618 |
| 官方日线 | 1,251,391 | 5,224 | 2025-10-17 至 2026-10-16，242 个交易日 |
| enriched 指标 | 1,251,391 | 5,224 | 与日线日期、股票覆盖一致 |
| 一分钟 K | 8,756,160 | 5,224 | 2026-10-08 09:31 至 2026-10-16 15:00，7 个交易日 |
| 历史财务指标 | 36,447 | 5,224 | 2024-12-31 至 2026-09-30，8 个报告期 |
| 利润表 | 36,447 | 5,224 | 同上 |
| 资产负债表 | 36,447 | 5,224 | 同上 |
| 现金流量表 | 36,447 | 5,224 | 同上 |
| 历史股本 | 36,447 | 5,224 | 同上 |
| 除权因子 | 4,707 | 3,512 | 仅保存真实除权事件，无事件证券不补造因子 |
| 当前实时行情 | 5,224 | 5,224 | 独立快照，来源 rustdx |
| 当前五档盘口 | 5,224 | 5,224 | 独立快照，来源 rustdx |

分钟初始化沿用默认参数 5 及已有自然日余量换算，因此实际持久化覆盖 7 个交易日；
`(symbol, datetime)` 去重计数与总行数一致。日线、指标、分钟和五张财务表
均无缺失股票或范围外股票。10 只证券的最后有效日线早于最近交易日，
逐只通过原生最新 K 线及既有标准化规则核对一致；零成交停牌占位按既有契约过滤。
配置中的指数、ETF 同步也完成，分别为 554 只 / 134,001 行和
264 只 / 61,706 行。

`initial_sync.json` 已持久化 `status=completed`、`universe_size=5224`，
数据 release 为 `25798de0c25d4e21ae92fb4850211504`。
公共目录自动发布版本为 `79ed7c752dd74b1296add7d79c4ef841`，
其中数据 release 与 completed 标记一致，包含当前行情与盘口。
对股票维表、日线、指标、分钟、除权、财务、快照和 release 的
528 个文件逐个比较源目录与公共版本 SHA-256，全部一致。
运行中的 `/api/data/status` 日线、指标、分钟行数与文件统计一致。

本次代码修复增加 6 项待上市及错误路径用例，先复现失败再修复通过。
受影响 Python 回归 `139 passed, 20 warnings`，警告为既有 Polars 弃用；
原生 Rust 测试 `6 passed`。Provider 与对应测试 Ruff、
`git diff --check` 及应用版本一致性检查通过。
本次验证覆盖本机运行环境；没有部署生产或生成其他操作系统的安装包。
