# 行情与K线调度调整验证记录

2026-11-03：六项要求已在独立源码工作目录实现并验证。代码尚未提交、合并或部署，当前运行环境的偏好配置未修改。

| 项目 | 实现后的初始化规则 |
| --- | --- |
| 实时行情周期采集 | 交易日09:15–11:45、13:00–15:30；15:30停止开始下一轮周期采集 |
| 1/5/10/15分K周期采集 | 与行情共享上述采集窗口，下午截止15:30 |
| 日K及盘后管道 | 默认交易日15:30，日K设置最早允许15:30 |
| 周K | 默认每周六09:00，由本地日K加工，不套用日K盘后时间下限 |
| 当日K线修正 | 固定交易日15:35，位于数据总览→数据同步→K线采集与加工的末尾 |
| 初始化参数 | 后端默认值、前端回退值、统一窗口和六类生命周期文档已同步 |

所有时间均为北京时间。市场实际交易时段仍为09:30–11:30、13:00–15:00。新的缺省时间不会覆盖用户已保存的日K/周K自定义时间；运行环境保存的旧配置需要在发布时另行核验。

修正任务顺序为日K、1分K、5分K、10分K、15分K，起止日期均固定为触发当天。日K沿原管道重取并计算，1分K走已有数据源，后三种分钟K从本地1分K加工。使用已有个股目录，不重新同步目录、不自动回算历史。

15:35是登记请求的时间，不代表任务在同一秒完成。前序任务占用执行槽或已有活跃数据任务时，修正请求保存为等待状态，30秒检查一次；执行时读取实时有效的能力配置。成功同日去重且跨重启保留，失败/取消不自动循环重跑，跨日等待或重启中断明确记录。日K被跳过、返回零行、周期返回错误/剩余缺口/日历核验警告均不能认证为修正成功。总开关关闭或交易日证据未知时不执行。

修正复用JobStore、执行槽、容量限制、缓存刷新、SSE及data release链；最终汇总成功后发布带`kline_correction`原因的data release。Platform公共数据仍由已有PublicPublisher发布，各阶段沿既有写盘与发布语义。本次不新增Provider、插件API或SQL迁移。

全量修正保持merge-upsert；不能据此宣称旧异常时间键已删除、停牌成交已补造，或今天所有股票的数据已完成真实修复。

## 验证结果

| 验证 | 结果 |
| --- | --- |
| 后端16个相关测试文件 | 355通过，0失败；23条既有Polars等警告 |
| 前端数据组件及Data页面 | 12个文件，79通过，0失败 |
| TypeScript及Vite生产构建 | 通过 |
| Ruff相对4453ecc4增量检查 | 20个Python文件，历史告警85条，新增0条 |
| 文档检查 | 37份Markdown、8个需求模块通过 |
| 版本一致性及diff格式 | 通过，版本保持0.4.46 |
| 浏览器实际交互 | 744px/360px组件宽度；分组展开、修正末行、三种时间、开关暂停/恢复已验证；无横向溢出、无控制台错误 |

后端通过命令（在源码工作目录的backend下执行）：

```sh
PYTHONPATH="$CANDIDATE_BACKEND" "$FROZEN_PYTHON" -m pytest tests/test_market_time.py tests/test_pipeline_schedule.py tests/test_kline_periods.py tests/test_preferences_cache.py tests/test_kline_correction.py tests/test_minute_refresh.py tests/test_repair_kline.py tests/test_manual_sync_defaults.py tests/test_pipeline_capacity.py tests/test_pipeline_catalog_readonly.py tests/test_quote_poll_cadence.py tests/test_ext_presets_startup.py tests/test_ext_pull_time_window.py tests/test_kline_periods_api.py tests/test_pipeline_and_monitor_fixes.py tests/test_pipeline_beijing_today.py -q
```

前端在frontend目录使用已有依赖及TRAE自带Node 24.22.1执行`vitest run src/components/data src/pages/Data.test.tsx`与`npm run build`。系统PATH没有node/pnpm，未重复安装依赖。浏览器使用隔离组件预览，未操作真实账户配置；验收后已关闭预览服务并移除临时入口。

原工作目录`TSP-market-data-schedule-20261103`位于受控归档，分支`feat/market-data-schedule-20261103`，基线`4453ecc4`。上方命令的机器路径以受控变量表示，原始命令另行保全。主要实现：[当日修正](../backend/app/services/kline_correction.py)、[后台调度](../backend/app/jobs/daily_pipeline.py)、[页面任务清单](../frontend/src/components/data/ScheduleOverview.tsx)。

本次完成的是源码和隔离验证；未进行线上交易日定时实跑、真实行情补采、回测或双端升级。工作区并行产生的资金档位规则及研究文档修改保持原样，未纳入本次功能实现。
