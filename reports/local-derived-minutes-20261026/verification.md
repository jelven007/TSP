# 5/10/15分K本地计算验证

需求日期：2026-10-26。开始基线：`6b86f724940682c802919c680e224a6d7510064e`，应用版本`0.4.31`。
本次为L3局部修改，复用周期服务、修正API、仓库写锁、任务状态和共享设置弹窗。

## 行为核对

| 入口 | 本次行为 | 验证依据 |
| --- | --- | --- |
| 周期同步 | 5/10/15分K按本地1分K计算并保存到各自分区 | 三周期服务测试，Provider调用设置为立即失败 |
| 离线补齐 | 本地计算后入库，无上游分钟能力也能提交 | 后端任务测试、前端组件测试及浏览器操作 |
| 全量修正 | 本地重算并覆盖相同证券/时间键 | 修正服务与API测试 |
| 遗漏修正 | 只补缺少的记录，不覆盖旧值；分别要求5/10/15根基础分钟齐全 | 缺口、并发保护和完整基础桶测试 |
| K线查询 | 优先计算当前本地1分K，live也保持本地；无基础结果时兼容已有周期表 | 查询/API跨日、过期派生表和空结果测试 |
| 空基础数据 | 离线零输出任务失败并提示先补1分K；遗漏显示已补/剩余数量 | 任务失败及不完整桶测试 |
| 日历 | 遗漏检测使用公布日历或已有可靠缓存，不触发上游日历请求 | 未知日历及禁止远程请求测试 |

完整标准交易日分别生成48/24/16根。复用09:30、13:00起算的收盘标签，
覆盖OHLC、量额、午休、跨日、证券隔离、重复分钟和未知值；继承基础分钟的
`raw`或`legacy_1m`价格口径。保留原`aggregate_ten_minute`调用入口。
对应视图、画像缓存及图表查询失效继续使用现有持久化和任务收尾链路。

## 自动验证

合入工作区后，相关后端15个测试模块曾完整通过：**325 passed，15个已有弃用警告**。
收尾期间共享文件继续出现周K的并行修改；本报告不认证该独立功能，最终分钟验证
排除名称含`week`或参数含`1w`的用例。期间一次完整重跑遇到并行编辑中的测试缩进错误，
后续缩进已由并行修改恢复；一次重跑中未完成的周K遗漏路径失败，不能作为分钟缺陷或全量通过。
最终上述分钟范围：**307 passed，20 deselected，14个已有弃用警告**。

后端命令（在`backend/`执行）：

```bash
uv run --frozen --no-sync pytest \
  tests/test_kline_periods.py tests/test_kline_periods_api.py \
  tests/test_kline_sync_progress.py tests/test_stock_preview_kline_periods.py \
  tests/test_repair_kline.py tests/test_rustdx_provider.py \
  tests/test_custom_minute_config.py tests/test_minute_refresh.py \
  tests/test_minute_adjust.py tests/test_minute_partition_merge.py \
  tests/test_minute_timezone_contract.py tests/test_kline_sync_timezone.py \
  tests/test_kline_detail_transport.py tests/test_minute_routing.py \
  tests/test_market_time.py -q -k 'not week and not 1w'
```

前端`pnpm test src/components/data src/pages/Data.test.tsx`：**49 passed / 11 files**。
`pnpm build`通过，保留已有ECharts大包提示。修改界面的定向ESLint通过。
根目录`make check`通过：87个目录/文件契约、31份Markdown及8个需求模块、
版本`0.4.31`一致性、`git diff --check`均通过。
本次服务及测试Ruff曾通过；API文件与冻结基线同为44个既有诊断，按诊断代码/消息比较无新增。
并行周K编辑后出现的格式诊断属于后续共享文件状态，不声称整个仓库lint全绿。

## 浏览器验证

使用可用的Browser工具，无Playwright替代。临时组件验证页位于工作区外，
URL为`http://localhost:3061/minute-qa.html`，视口676×815；API仅返回模拟任务，
实际渲染`KlineSyncConfig`、`RepairDailyPanel`和共享弹窗。
已核对验证页这三个组件及分钟周期工具与合入工作区内容一致。

三种周期依次点击「获取数据」，实际状态分别为`offline-5m`、`offline-10m`、
`offline-15m`；无上游能力时1分K按钮仍禁用。修正入口依次产生
`repair-5m-missing`、`repair-10m-full`、`repair-15m-missing`。
无横向溢出及Vite错误遮罩，文字和按钮可操作；控制台仅有React Router版本迁移提示。
截图已通过Browser显示。一次截图因标签未激活失败，激活后重试成功。
浏览器验证证明界面和请求参数，不证明真实行情回填或生产环境行为。

## 文档、协作与边界

同步需求、架构、测试、运维、发布支持、二开、接入、工程索引及操作说明书。
保留并行采集窗口修改，分钟刷新测试同步为当前11:45/16:00半开结束边界；
没有把采集窗口扩大成聚合交易时段。

未提交、推送、部署或重启生产；未递增版本、修改用户配置或执行真实行情回填。
既有行情分区保留。基础1分K缺失时须先补基础数据；有输出和任务100%不认证完整历史覆盖。
前一任务的rustdx逐月一致性研究及已知原始数据标签问题不属于本次修复。
