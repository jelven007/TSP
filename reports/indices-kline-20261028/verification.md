# 指数页七只与日／周／月K修正验证

核对日期：2026-10-28。分支：`fix/indices-kline-repair-20261028`，
起点`18f6f5cb`，应用版本`0.4.34`。本次是未提交的工作区源码修改。
原工作区已有其他修改及删除，本报告只认证下方指数变更，不代表其他任务已发布。
没有更新本地运行副本或线上，没有执行真实七指数历史行情初始化。

## 用户要求与交付

| 要求 | 实现与证据 |
| --- | --- |
| 新增沪深300、上证50、深证100 | `index_const.PAGE_INDEX_NAMES`定义七只，服务端目录驱动指数页；初始化即使源目录为空仍保留七只，行情按七只批量请求 |
| 原四只和新增三只支持日／周／月K | 日K及Enriched沿用指数独立表，新增指数周/月表；通用指标按周期计算，前端复用个股图表及指标控件 |
| 右侧参照个股弹窗 | 身份/行情、日周月选择、60根默认及±50%数量、日期区间、指标、K线点选与分时；1600px并排，390px纵排 |
| 原同步指数日K改为数据修正 | 复用数据总览`RepairDailyPanel`及`SettingsModal`，默认七只、日K、近30天至今天 |
| 支持周K、月K及全量／遗漏 | 日K按当前Provider取数；周/月从本地指数日K聚合；全量覆盖旧值，遗漏按证券+日期真缺口追加 |
| 可跟踪、取消及失败反馈 | 共用持久JobStore、容量槽、取消及任务卡；管理写入经Platform，缺能力/基础日K/部分失败明确失败 |

固定七只：
`000001.SH`上证指数、`399001.SZ`深证成指、`399006.SZ`创业板指、
`000680.SH`科创综指、`000300.SH`沪深300、`000016.SH`上证50、
`399330.SZ`深证100。侧栏和市场总览继续使用CORE四只。

## 后台表与接口

| 表/目录 | 含义 |
| --- | --- |
| `kline_index_daily` | 指数原始日K，按`date`分区，`symbol+date`幂等 |
| `kline_index_enriched` | 指数日K稳定存储列，读取时计算通用指标 |
| `kline_index_weekly` | 本地日K聚合，`date`为周一，`trade_date`为最后实际交易日 |
| `kline_index_monthly` | 本地日K聚合，`date`为月初，`trade_date`为最后实际交易日 |

周/月按时间顺序取首开、末收、最高、最低，量额求和，未知量额保留空值。
MA/MACD/RSI/KDJ/BOLL在周期序列上计算；不使用股票复权、股本或涨跌停规则。
读接口按单指数和日期分区扫描，预读指标暖身；截止日期落在周期中间时重算边界柱。
已有本地日K但尚无周期表时可只读聚合显示，读接口不写盘或偷偷远程取数。
日K修正和常规同步均刷新受影响周/月整周期。

- `GET /api/index/catalog`：固定七只目录。
- `GET /api/index/kline?symbol=&period=1d|1w|1mo&start_date=&end_date=`：
  返回指数、周期、行、来源和范围；普通业务读取权限。
- `POST /api/index/repair`：`start_date/end_date/period/mode`，
  日期严格`YYYY-MM-DD`，结束不晚于北京今天；默认日K、full。
  每次必含七只；`data:manage`，API经管理面转发Platform。
- 旧`/daily`、`/minute`、`/sync_daily`保留兼容；
  分钟接口新增可选`with_prev_close=true`，旧调用默认不变。

股票`1m`仍表示1分K；指数月K单独使用`IndexKlinePeriod`的`1mo`，
没有扩大股票六周期菜单。指数外部数据接入不发送股票日K请求。
结束任务失效指数K线、分钟、行情、数据状态及任务查询；后端刷新视图、缓存并发送行情更新事件。

## 实际验证

环境：macOS，项目已有Python虚拟环境，Node 24.22.1，pnpm 9.10.0，
Playwright 1.64.1及本机Google Chrome。浏览器插件工具未提供可用接口，
按前端测试技能使用Playwright备用路径；未修改项目依赖。
以下日志保留完整命令输出，测试计数不跨重复运行相加。

| 检查 | 结果 | 原始输出 |
| --- | --- | --- |
| 后端13个相关模块 | 174通过，14条既有Polars警告 | [backend-tests.log](evidence/backend-tests.log.gz) |
| 前端全量84个测试文件 | 451通过 | [frontend-tests.log](evidence/frontend-tests.log.gz) |
| TypeScript及Vite构建 | 通过；ECharts大chunk提示仍存在 | [build.log](evidence/build.log.gz) |
| 前端ESLint | 通过 | [frontend-lint.log](evidence/frontend-lint.log.gz) |
| Python增量Ruff13文件 | 新增诊断0；新服务/测试0，原API4条与基线相同 | [ruff-comparison.json](evidence/ruff-comparison.json) |
| Chrome真实组件+模拟API | 8组检查通过；7×3周期请求、进度/取消/错误与窄屏 | [browser-results.json](evidence/browser-results.json) |
| 隔离反向验证 | 四组均预期失败，共6项回归失败 | [reverse-results.json](evidence/reverse-results.json) |
| 根结构/文档/版本/空白检查 | 通过；87条必需路径、31份主文档、8个需求模块、版本0.4.34 | [repository-check.log](evidence/repository-check.log.gz) |

首次扩大后端测试时，现有`.env`使子进程按多租户API角色导入，
一项旧JobStore冷启动测试未创建全局写端而失败（173通过、1失败）。
未改源码或运行配置；使用隔离standalone环境和临时数据目录重跑后174项全部通过。
发布测试也使用独立源/目标目录及显式世代标记，不绕过公共发布验证。

<a id="commands"></a>
### 可重跑命令

将工具目录加入PATH：

```sh
export PATH="/Users/bytedance/.local/bin:/Users/bytedance/.trae-cn/binaries/node/versions/24.22.1/bin:$PATH"
```

在`backend/`执行：

```sh
PYTHONPATH=/Users/bytedance/Documents/TSPP/backend \
TICKFLOW_ENV_FILE=/dev/null MULTITENANCY_ENABLED=false SERVICE_ROLE=standalone \
DATA_DIR=/tmp/tspp-indices-test-runtime \
uv run --frozen --no-sync pytest \
  tests/test_index_kline_periods.py tests/test_repository_index.py \
  tests/test_index_daily_live.py tests/test_index_minute_history.py \
  tests/test_index_minute_api.py tests/test_quote_index_merge.py \
  tests/test_custom_provider_indices.py tests/test_data_release.py \
  tests/test_kline_periods_api.py tests/test_job_stall_and_cancel.py \
  tests/test_job_record_persistence.py tests/test_job_persistence_failures.py \
  tests/test_heavy_job_limiter.py -q
```

在`frontend/`执行`pnpm test`、`pnpm build`、`pnpm lint`；
在仓库根执行`make check`、`git diff --check`。
临时浏览器脚本及反向验证脚本复制到证据目录，仅供检查内容与重建临时环境，
其路径指向本轮本机缓存和临时目录，需要按重跑环境调整。

### 回归有效性

新增目录/修正回归在实现前已重复验证失败；并发补充回归也连续三次失败，
确认快照读完才取指纹会把并发写入的新值覆盖。
修复把指纹取值移至读取前，写锁内检测变化并重读，已有记录保持不变。
相关测试验证并发写入的`close=777`不会被遗漏请求的旧值覆盖。
周/月基础缺口的错误也避免被泛化“无数据”覆盖。

反向验证完全在`/tmp`源码副本，工作区未stash或还原：

| 临时撤回 | 结果 |
| --- | --- |
| 目录恢复四只 | 七只目录断言失败1项 |
| 仓储恢复实施前版本 | 独立周/月存储、只读和指标回归失败2项 |
| 指纹检查恢复旧顺序 | 并发遗漏保护回归失败1项 |
| 指数页面恢复实施前版本 | 新三只与数据修正回归失败2项，原日期隔离用例通过 |

## 浏览器截图

所有行情、指标与任务值是测试fixture，不是真实市场/运行数据。
业务请求全部拦截；真实React、共享弹窗和ECharts在浏览器中运行。
预期的403/502错误专门验证错误展示，其余console/pageerror为0。

- [桌面浅色](screenshots/desktop-light.png)、[桌面深色](screenshots/desktop-dark.png)
- [桌面修正弹窗](screenshots/repair-desktop-dark.png)
- [窄屏浅色](screenshots/mobile-light.png)、[窄屏分时](screenshots/mobile-light-intraday.png)
- [窄屏修正弹窗](screenshots/repair-mobile-light.png)
- [窄屏深色只读账户](screenshots/mobile-dark-reader.png)

## 六类文档与自审

需求：`docs/requirements.md`，沿`FR-RESEARCH-02/FR-ADMIN-05`；
方案：`docs/architecture.md`；开发：`CONTRIBUTING.md`及
`docs/reference/engineering.md`；测试：`docs/testing.md`；
运营：`docs/release-operations.md`；运维：`docs/operations.md`。
同步`docs/secondary-development.md`、`操作说明书.md`和文档/报告索引。

本次代码逐项核对目录、取数源、周期聚合、分区幂等写、独立表、
范围/空值、读端不写盘、管理权限、执行容量、取消/失败、公共发布、
查询键和日期迟到隔离；未发现剩余阻断项。
精确范围以实施前16文件快照的增量及新增服务/回归为准，
不把同一工作树中历史清理、SSE共享、系统设置等其他变更归为指数改动。
没有提交、推送、部署、清理真实数据或手工递增版本。

后续运行环境升级须加载匹配前后端及Platform，再通过真实Provider执行日K修正，
核对七只实际缺口后生成周/月K。源码测试及任务100%均不替代该数据覆盖验收。

发布时原始日志与patch采用无损gzip附件，解压内容及原始SHA-256保持不变；未改写历史测试结果。
