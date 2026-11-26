# API、事件与 MCP 导航

本页维护当前接口入口和兼容规则；字段及权限以实际路由、类型与契约测试为准。
通用数据源和扩展协议见[接入参考](reference/integrations.md)，
策略结果含义见[S01](reference/s01.md)、[S02](reference/s02.md)。
历史查询只读持久化结果，不通过GET启动策略、行情加载或Compute。
普通自定义策略盘后“选”窗口为21:00～22:00，S03及S05为22:00～23:00；
状态维护通过已有池、选名单及日视图读取接口呈现。
S05采用[独立四维评分实现](reference/s05.md)，新归档来源字段
`score_provenance.scoring_implementation=s05_independent_four_dimensions_v1`。
评分/名次等既有字段保持，旧记录缺来源标识仍可读取，不新增接口或通过GET重算。
S05当日监控摘要的`branch_code/branch_source_date`只描述买入分支及来源日。
卖窗口按前一交易日买入归档匹配；买入分支4或缺当日分支不阻止已确认的卖窗口。
S05现金回测的`strategy_info.exit_params`按出场ID返回各保存参数，
`provenance.s05_selection_replay`记录每交易日分支、源日与评分来源；
仍通过现有策略回测接口，不新增提交或重算接口。
当前/下一交易日池可附带`status_refresh`，记录目标日、观察时钟、公告确认状态及剔除信息。
选名单读取同步应用持久化剔除，日视图缓存令牌随结果改变，原始归档明细保留。
实时状态更新不由GET触发；`strategy_results_updated`携带目标日和受影响策略ID刷新页面。
交易日08:30/09:00股票池消费各自当日扫描回执；第二轮不会将首轮回执视为新核验，
同样刷新池和自定义“选”状态投影。部分覆盖及错误随`status_refresh`返回，
原归档读取接口保留冻结记录；复牌/摘帽只按范围恢复入池，不自动新增“选”。

`GET /api/pipeline/jobs`沿原`suspension_sync`字段返回官方公告扫描覆盖、分页断点、
逐公告重试及公共发布身份。可选`evidence_policy=stock_status_v2`表示同时查证停复牌及
ST戴帽／摘帽；旧缺字段回执保留。相关维护20:30优先核验下一交易日状态，原自动总开关生效，
本项不新增HTTP写入口，不由读取请求触发采集。

登录租户的策略监控启停使用
`POST /api/monitor-rules/strategies/{strategy_id}/monitoring`，
接收`{"enabled":true|false}`，所有公开策略共用此接口，返回启停世代，不立即计算策略；
`GET /api/monitor-rules/strategies/monitoring`读取当日卖／选／买时段及保存状态。
账户权限和同源校验沿现有中间件，不因此授予Tier A访问。

新入池策略默认关闭；自定义／AI策略开启后，后台按交易日执行选、买、卖三个任务。
取消监控推进世代并取消未完成任务；S02沿同一策略级启停API管理。
完成阶段使用`monitor_contract=strategy_monitor_v2`及`monitor_tasks`保存回执，
单策略明细保留这些字段，成功与正常拒绝不会因行情序号或30秒期限变成等待。
`strategy_results_updated`仅失效事件携带策略ID的结果及监控状态；切卡片、日期或页签只读。
普通自定义日线策略也生成夜间选任务；日期归档按策略自身`stock/1d`或`stock/1m`
隔离，日视图读取兼容这两种周期，不把日线策略的结果强写为分钟身份。

`GET/PUT /api/strategies/pool`按租户＋用户维护名单及顺序，响应`ids/revision`；
PUT必须带非负整数revision，空ids明确清空，重复或不可用ID拒绝，冲突返回409。
旧非空选择迁移通过可选`migrate=true`，明确编辑后不被旧导入覆盖。
事务内发布私有策略池事件。

`GET /api/screener/daily-stock-pool/{strategy_id}`要求`as_of`，
返回pool、total和分页symbols，可选record_id读取快照；limit为1～1000、offset非负。
当前`strategy_scope_v4`只按自身完整范围及状态成池，数值过滤与行情缺失不缩池。
旧快照按原版本读取，不由GET重新标注或重算。

`POST /api/custom-signals`的早盘清仓模板为`morning_liquidation`，
kind为exit、timeframe为intraday，conditions为空。
七项`start_time/time/fallback_time/rise_pct/fall_pct/decline_3m_pct/volume_ratio_min`
允许显式null停用，time仍是止损键；关闭条件同时关闭其专用依赖。
全空不生成退出。现价相对精确前交易日原始昨收；分钟跌幅和均量比只读取已完成前缀。
旧振幅两字段、涨跌四字段冻结定义仍保留原解析，不读取即转换。

尾盘走强模板为`late_session_strength`，kind为entry、timeframe为intraday，
conditions为空。新十六字段依次为`time/evaluation_delay_minutes/pool_up_min_pct/`
`amount_min_100m/turnover_min_pct/amplitude_max_pct/change_min_pct/change_max_pct/`
`volume_ratio_min/ma5_1m_count/ma5_5m_count/end_time/return_5m_min_pct/`
`return_15m_min_pct/position_min_pct/gap_max_pct`，显式null停用且不补默认。
最近5/15分钟涨幅下限范围−10.0～100.0，日内位置下限和开盘缺口上限0.1～100.0；
均按百分数数值保存。六个当前入场信号共用模板，S05当前定义取消专用配置与规则，
旧冻结输入保持原解析，当前规则见[S05](reference/s05.md)。
确认延迟页面30～900秒、30秒步长，内部除以60保存；百分数按百分数数值保存，
成交额单位亿元，MA5数量为1～100整数。启动及结束用北京时间HH:mm:ss。
尾盘任务的启动、确认、重试与告警按保存定义及阶段回执处理。
S02买任务语义、异常及发布条件统一见[当前定义](reference/s02.md#买任务执行顺序)。
响应`monitor_tasks.buy.signals`含`breadth/stocks`阶段回执，阶段`rows/hits`
与任务最终`published/count`须分开读取。输入、计算、提交和独立观察证据见
[排障字段](reference/s02.md#页面与排障回执)；历史／盘后重载的诊断身份见
[输入与提交契约](reference/s02.md#输入修复冻结与提交)。
全部当日指标固定截至启动分钟。空模板截止不取消全局14:55实时新入场守卫。
旧六／七字段冻结定义保持原缺省及校验。

`POST /api/custom-signals/intraday/replay`只回放信号，响应execution为signal_only。
持仓出场须提供1～200条positions，各含symbol和已确认交易日entry_date；
数量不证明账户授权或实际成交。尾盘回放须指定strategy_id并读取其保存的当日完整池，
symbols只限定输出，不改变广度分母；诊断明确缺快照或数据日期。

## 1. 三种入口

| 入口 | 使用方 | 契约与权限 |
| --- | --- | --- |
| 登录会话接口 | 项目前端、管理员操作 | 账户权限、租户范围与同源检查；内部接口不会因存在路由而对 Token 开放 |
| Tier A API | 外部脚本、自建应用 | `Authorization: Bearer tsp_…`、scope、限流和稳定契约 |
| MCP | 支持 MCP 的客户端 | 独立 stdio 包，经 HTTP 调用同一 Tier A API；工具随配置 scope 启用 |

机器可读的开放契约为 `GET /api/openapi.json?tier=a`；原始 FastAPI
`/openapi.json` 包含内部路由，不代表外部系统拥有其权限。
稳定端点集合由[契约测试](../backend/tests/test_openapi_contract.py)冻结，
当前为 69 个方法/路径模板组合。

策略详情继续返回合并后的有效配置，可选`definition_defaults`返回原始可编辑配置。
内部配置POST为全量覆盖，PATCH只对`basic_filter`按字段合并；
null取消对应限制，缺省字段继承。数值范围及有效退出组合校验失败返回400，
不持久化。新增字段不改变现有授权范围或Tier A端点数量。

策略参数新增可选`display_scale/unit`，详情新增可选`minute_execution`及
`definition_defaults`；选股结果新增可选`sections/state/portfolio_scope/execution_error`。
旧结果缺字段继续可读。单策略省略周期时采用定义的首个周期，旧日线定义仍为1d；
显式周期继续校验，不将分钟策略自动降级到日线。开放端点和scope保持原集合，
OpenAPI指纹更新只确认上述请求／响应兼容变化。

## 2. Token 权限

| Scope | 能力 |
| --- | --- |
| `read:market` | 行情、指数、证券搜索与 K 线读取 |
| `read:analysis` | 策略、结果、回测报告、环境与监控事件读取 |
| `read:ext` | 扩展表结构、行和字段取值读取 |
| `write:ext` | 已有扩展表行数据写入 |
| `run:backtest` | 提交受并发控制的选股、回测和因子检验 |
| `paper:trade` | 模拟账户和订单等账务操作 |

`admin` 不向 API Token 签发。源重载、系统配置、行情同步、表结构管理、历史删除
及 AI 会话等管理或内部接口不因其他 scope 存在而开放。
有 Bearer 时按该 Token 校验，不借用浏览器 Cookie 扩大权限。

独立模式 Token 存储为哈希；完整多租户 Token 绑定控制库中的账户、租户、
授权版本及有效期。明文仅创建时返回一次。默认限流 120 请求/分钟，
触发时返回 429 和 `Retry-After`；重任务另受执行资源限制。
实现见[开放网关](../backend/app/services/api_gateway.py)、
[独立 Token](../backend/app/services/api_tokens.py)与
[多租户 Token](../backend/app/multitenancy/tokens.py)。

## 3. 按领域找接口

| 领域 | 入口示例 | 源码 |
| --- | --- | --- |
| 证券与股票 K 线 | `/api/kline/instruments/search`、`/api/kline/daily`、`/api/kline/minute`、`/api/kline/minute-range` | [kline.py](../backend/app/api/kline.py) |
| 指数 | `/api/index/daily`、`/api/index/minute`；内部页使用 `/catalog`、`/kline` | [indices.py](../backend/app/api/indices.py) |
| 市场快照与策略 | `/api/overview/market`、`/api/screener/market-snapshot`、`/api/strategies` | [overview.py](../backend/app/api/overview.py)、[strategy.py](../backend/app/api/strategy.py) |
| 策略运行与持久结果 | `/api/screener/run`、`/run_all`、`/cached`；归档分页为内部接口 | [screener.py](../backend/app/api/screener.py) |
| 回测与因子检验 | `/api/backtest/run`、`/strategy/run`、`/factor/run`、`/status` | [backtest.py](../backend/app/api/backtest.py) |
| 扩展数据 | `/api/ext-data/{config_id}/rows`、`/values`、`/ingest` | [ext_data.py](../backend/app/api/ext_data.py) |
| 环境与告警 | `/api/regime/latest`、`/history`、`/api/alerts` | [regime.py](../backend/app/api/regime.py)、[alerts.py](../backend/app/api/alerts.py) |
| 模拟交易 | `/api/paper/accounts`、`/orders`、`/trades`、`/corporate-actions` | [paper.py](../backend/app/api/paper.py) |
| 事件 | `/api/events/ticket`、`/api/events` | [events.py](../backend/app/api/events.py) |
| 健康与观测 | `/health`、`/health/live`、`/health/ready`、`/api/health` | [routes.py](../backend/app/api/routes.py) |

具体方法、参数、分页范围和响应组件以 Tier A 视图为准；上表中的内部页和
管理入口只用于定位源码。外部调用的例子见[Open API 样例](../examples/open-api/README.md)。

`POST /api/backtest/strategy/run` 接受可选的 `entry_participation_pct` 和
`exit_participation_pct`。两者都是大于 0 且不超过 1 的比例，缺省各为 `0.1`
（10%），分别限制回测买入和卖出的参与比例。

## 4. K 线与历史结果兼容

单策略运行接口`POST /api/screener/run_preset`和`POST /api/strategies/run`
省略`timeframe`或传null时，使用该策略声明的首个周期；
当前S02声明`1m`，参数及源码见[S02当前说明](reference/s02.md)；普通日线策略声明`1d`。
显式传入不支持的周期仍拒绝，不改用其他行情口径；结果归档使用实际解析周期。

股票 `daily` 省略 `freq` 时为 `1d`，`minute` 省略时为 `1m`。
当前个股六周期为 1m/5m/10m/15m/1d/1w；非默认周期按接口允许值使用。
5/10/15 分钟从本地 1 分钟计算，周 K 从本地日 K 计算；
无基础数据不得静默返回另一周期。

内部指数 `GET /api/index/kline` 使用 `period=1d|1w|1mo`。
省略 `start_date` 时优先使用本地可用起点，响应包含可选
`available_start_date`；显式范围优先。指数目录固定七只，
指数修正需要 `data:manage` 会话权限。

`GET /api/screener/cached` 可按 `as_of`、`asset_type` 和 `timeframe` 查询，
省略字段保持兼容语义。指定日期没有记录时返回空结果，不自动重算。
归档按稳定策略 ID、名称、逻辑版本、参数、标的池和日期隔离；
分页、明细与删除接口不因此加入开放面。

金额为元，分钟 `datetime` 为北京时间 naive 墙钟；比例和成交量不能统一猜测单位。
请按[金融契约](../CONTRIBUTING.md#3-不可混用的数据契约)和
[Provider 字段](reference/integrations.md#plugin-内部数据契约所有数据集必须遵守)处理空值、原价与复权价。

持仓提醒内部接口保持`GET /api/lots`的`{lots: [...]}`结构；
`POST /api/lots`和`DELETE /api/lots/{lot_id}`仍针对单只股票。
新增`GET /api/lots/batches`返回`batches`和`next_number`，
`POST /api/lots/batches`接收`name`、`request_id`及1～100项`stocks`。
每项沿用股票成本、数量与提醒字段，同批次不得重复代码。
名称最长80字符，留空自动命名；`request_id`支持字母、数字、下划线和短横线，
相同请求重复提交返回已保存批次，内容不同返回409。
批次号按北京时间`YYYYMMDD01`～`YYYYMMDD99`在租户内累计；达到99返回409。
序号、批次、股票和派生规则在同一SQL写事务保存；校验或写入失败不消耗序号。
股票可选`batch_id`表示所属批次，编辑保留旧分组，删除单只不影响同批次其他股票。
`DELETE /api/lots/batches/{batch_id}`接受10位数字批次号，返回
`{ok: true, deleted_lots: N}`；当前租户不存在该批次返回404，非法编号或存储成员ID返回400。
事务内删除全部成员、`{lot_id}_p/_d`规则及批次元数据，空批次返回N=0；
每日序号保留，删除后不复用编号。中途失败全部回滚，提交后同步引擎。
该写接口沿用登录租户及同源写入权限。
这些接口不加入Tier A开放集合。

## 5. SSE 连接与恢复

浏览器使用登录会话；外部 Token 客户端先用 `POST /api/events/ticket`
换取 60 秒一次性票据，再建立事件流。
完整多租户 Gateway 使用短期授权与持久事件游标，按租户和 scope 过滤私有事件。

票据到期需重新换票，并从最后确认游标重连。`events_reset` 表示相关 REST
读模型需要重新同步，不应清空所有查询或刷新整页。慢消费者、断线、撤权和
广播中断的行为见[网关协议](reference/integrations.md#gateway)。
代理配置必须保持长连接并关闭不适当的缓冲。

## 6. MCP

[mcp-server](../mcp-server/README.md) 为独立 Python 包，
当前实现 12 个按 scope 启用的工具，包括证券搜索、股票与指数 K 线、
市场概览、环境、策略详情、告警、扩展表读写和回测。
配置实际实例 URL 和 Token 后按其 README 启动。
MCP 不绕过服务权限，也不会额外开放 AI 或系统管理接口。

## 7. 修改接口时验证

同步后端参数/响应、前端 [api.ts](../frontend/src/lib/api.ts)、调用方、
集中[查询键](../frontend/src/lib/queryKeys.ts)及相邻测试。
开放集合或 schema 变化必须有兼容说明，并更新真实契约快照；
不要在文档中独立维护一个易过期的指纹。

按[测试指南](testing.md)运行 API、Provider、Token/租户隔离、
OpenAPI 和 SSE 定向回归。接口测试通过不替代部署、真实数据覆盖或负载验收。
