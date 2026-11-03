# Market Data Schedule Implementation Plan

**Goal:** 完成用户指定的15:30采集截止/日K、周六09:00周K、15:35当日修正及初始化默认值。

**Architecture:** 共用market_time窗口与preferences；修正模块组合已有range repair和任务存储。排队/日期/重启去重为修正模块的小型持久状态，不平行创建写盘框架。现有前端ScheduleOverview/KlineSyncConfig按相同契约展示。

**Tech Stack:** Python/FastAPI/APScheduler/Polars；React/TypeScript/Vitest。

工作目录：`/Users/bytedance/Documents/TSP-market-data-schedule-20261103`。测试使用原仓库虚拟环境Python模块方式，PYTHONPATH指向工作目录backend，避免旧shebang。原工作区仅有其他任务未跟踪报告，不能覆盖。

## 1. 时间默认与校验

- [ ] 先更新/增加backend/tests/test_market_time.py、test_pipeline_schedule.py、test_kline_periods.py、test_preferences_cache.py中的新期望，测试确认RED。
- [ ] backend/app/market_time.py统一窗口和下午poll end至15:30；保持连续竞价时段不变。
- [ ] preferences.get_pipeline_schedule默认15:30；set_pipeline_schedule最早15:30。
- [ ] kline_periods.get_configs：分钟/日Ktime默认15:30，周K09:00；分钟windows从统一常量读取。
- [ ] save_config：日K最早15:30；周K09:00合法且不套用日K校验；旧自定义偏好保持。
- [ ] quote_service._FINAL_DEADLINE close_final及_market_phase调整15:30，同步当前注释/日志。
- [ ] 找出初始化实际事实源与其他15:35/16:00默认及验证，按本次范围更新。

运行：`PYTHONPATH=<worktree>/backend <original>/backend/.venv/bin/python -m pytest tests/test_market_time.py tests/test_pipeline_schedule.py tests/test_kline_periods.py tests/test_preferences_cache.py -q`，cwd工作目录backend。

## 2. 当日15:35修正

- [ ] 先写backend/tests/test_kline_correction.py，覆盖同日范围/周期顺序、日期冻结、休市/自动关闭、执行槽忙不丢请求、成功重启去重、失败/取消终态。
- [ ] 新模块backend/app/services/kline_correction.py组合run_repair_daily(period=1d/1m/5m/10m/15m, mode=full, start=end=day)。
- [ ] 复用JobStore、run_with_capacity、执行槽，不将其他活跃任务当成修正结果；持久请求状态用于繁忙等待/重启。
- [ ] daily_pipeline注册交易日15:35请求和有界等待重试；apply_data_sync_mode控制新任务。仅standalone/platform拥有市场调度。
- [ ] 每周期返回error/remaining未完成时保留准确记录；任务错误不被JobStore.succeed误认证。发布新data release及公共release、刷新缓存沿已有路径，旧公共快照保留。
- [ ] 适配前端任务kind/stage展示及相关后端测试，不增加无关任务或自动修历史。

运行：同一Python运行test_kline_correction、test_manual_sync_defaults、test_full_schedules、现有repair_daily及任务生命周期相关测试。

## 3. 页面

- [ ] ScheduleOverview.test先确认新窗口、日K15:30、周K09:00、新修正行顺序/关闭状态RED。
- [ ] ScheduleOverview修改时间回退与固定行，新行放K线组末尾；实时行情/扩展同共享窗口文字同步。
- [ ] KlineSyncConfig统一窗口文字、ScheduleEditor日K最早时间、Data.tsx日Khint调整。
- [ ] 如需可见修正开关/状态，复用preferences/API现有响应和查询键，不伪造running状态。
- [ ] 前端相关测试及`pnpm build`；浏览器按已有插件验证桌面与窄屏，测试场景使用隔离预览而非用户真实配置。

## 4. 交付验证

- [ ] 六类受影响生命周期文档同步：requirements、architecture、secondary-development、testing、operations、release-operations；不得在历史审计报告中追溯改写旧时间。
- [ ] 初始默认值/任务配置明确写入事实源；现存用户偏好保留及运行未部署状态明确。
- [ ] `dev-verify`完成前加载，按实际fresh命令记录测试、构建、Ruff、scripts/check_docs.py、scripts/check_version.py、git diff --check。
- [ ] 写根工作区reports中的交付报告，以computer链接交付；代码在独立worktree，无提交/推送/发布。
- [ ] 不追加收益回测，不把新任务源码完成说成历史行情已修复或双端已升级。
