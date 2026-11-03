# 分钟选源合并验证

基线：`8b669fb5` / `0.4.22`。范围对应 `FR-ADMIN-04`、`FR-DATA-03`，
属于 L3 核心路由契约调整。仅本地修改，未提交、推送或部署，不递增版本。

数据源页删除独立“全量分钟”路由、能力标签和编辑入口。
历史分钟与盘中全市场落盘统一读取 `minute_data_provider`，
复用 `_resolve_minute_provider`；rustdx 保留批量修复、增量及请求计数，
HTTP YAML 复用 `minute` 当日窗口。刷新开关、间隔、分区、标准字段、
北京墙钟和幂等合并保持；缺能力或协议失败不跨源、不发布部分成功。

旧偏好文件不改写。旧 getter 与响应字段保留分钟路由的只读别名，
旧 API 输入字段被忽略。旧 YAML 声明保留读取与试拉，不自动认证历史分钟能力。
TickFlow 原生全市场端点仍按真实权限门控，不因插件分钟声明补授该权限。
前端统一分钟服务查询键；切换路由、重载来源与修改监控参数时失效服务状态。

生命周期文档同步：需求基线、架构、CONTRIBUTING、测试、发布运营、运维，
并更新接入参考、二开指南、README、版本基线能力描述和操作说明书。
历史报告和版本号保持原值。

## 实际验证

后端在 `backend/` 使用 `uv run --frozen --no-sync pytest -q`：

- 分钟与选源：`test_unified_minute_source.py`、`test_minute_refresh.py`、
  `test_capability_matrix.py`、`test_capability_augment.py`、
  `test_full_minute_capability.py`、`test_custom_minute_config.py`、
  `test_custom_intraday_batch_window.py`、`test_data_provider_reset.py`、
  `test_preferences_cache.py`、`test_minute_routing.py`、
  `test_rustdx_sync.py`、`test_rustdx_provider.py`、
  `test_rustdx_contracts.py`、`test_second_acceptance_observer.py`：
  **288 passed**。
- 关联发布、桌面、质量、健康与初始化：`test_data_release.py`、
  `test_desktop_packaging.py`、`test_data_quality.py`、`test_health.py`、
  `test_initial_sync.py`：**86 passed, 1 failed**。
  失败是 `test_status_http_exposes_unknown_historical_evidence` 在
  `test_data_quality.py:75` 对尚未初始化的 `pipeline_jobs.job_store` 执行
  monkeypatch，报 `AttributeError: None has no attribute 'list_recent'`。
  在未修改的上述基线单独运行该测试得到相同失败，未扩大本次范围修复。
- 新契约测试实施前为 **6 failed, 1 passed**，证明矩阵、旧选源、API写入、
  后台选源和HTTP端点均能捕获原行为。

前端 `pnpm test` 定向运行 `DataSources.test.tsx`、
`DataSourceEditor.test.tsx`、`Monitoring.test.tsx`、
`Screener.test.tsx`、`lib/api/marketStatus.test.ts`：**19 passed**。
`pnpm build` 通过；保留既有大chunk提示。
新增编辑器测试暴露既有嵌套button警告，对应UI未在本次修改。

`make check` 通过：目录、文档契约/链接、版本和差异空白。
受改动Python文件Ruff与同文件基线比较：**新增告警0**；
当前116条、基线117条，不声称全仓lint干净。

浏览器使用实际构建与隔离配置、真实能力矩阵验证：
数据源页显示七项路由且无“全量分钟”；点击实时监控后保留盘中落盘、
60秒间隔、可操作控件；点击新增数据源确认单一“分钟K”编辑入口。
556×817窄屏无水平溢出，实际截图确认卡片和文字可读，无框架错误遮罩。
隔离环境的SSE未连接及外部更新清单下载失败不作为真实服务验证。

未运行生产部署、完整后端全量测试、全市场真实交易时段负载与平台安装包。
回滚恢复匹配代码即可，原配置与分钟文件保留；仅旧full_minute插件应先配置
真实minute能力后再启用盘中落盘。
