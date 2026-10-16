# 首次启动数据源与四板初始化修复验证

应用版本保持 `0.4.0`。

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

## 本地联调与限制

浏览器在本地数据源页面确认七项 rustdx 路由可点击，默认标识和安装说明可见；
实际点击单项路由及 rustdx「套用」后，七项目标源保持 rustdx、状态仍为未就绪。

源码 Platform 重启后健康端点正常，首次自动任务确实创建，来源为 startup；
因本机缺少 `tsp_rustdx_native`，任务明确失败，保留 pending，未误报 0 行成功。
初始化成功路径使用四板 Provider 受控数据，实际执行目录校验、Parquet 持久化、
财务累积、分钟存储、指标计算及发布，不将这些结果声称为真实全市场验收。

真实行情全量验收需在包含固定 rustdx 原生桥接的环境执行；本轮未部署生产。
