# API、事件与 MCP 导航

`GET /api/screener/market-snapshot?realtime=true&symbols=600001.SH`仅返回当前日共享缓存
中的指定股票（每批1～1000只），返回`raw_close/change_pct/turnover_rate/vol_ratio_5d`
及既有技术信号、报价时间字段。缺当前日缓存返回当日日期和空rows；非法范围422。
不带参数继续返回原全市场快照，单位及验证见[本次记录](../reports/engineering/s02-live-results-20261106.md)。
两个新增查询参数均为可选，缺省为`realtime=false`、`symbols=null`；
Tier A端点集合及响应组件保持，契约快照与默认请求回归同步验证。

模拟账户新增 `PUT/DELETE /api/paper/account?account=<id>`（本地源码候选，未部署）。
修改字段为 `name/initial_cash/commission_pct/stamp_tax_pct/slippage_bps/queue_limit_orders`，
账号ID固定；已有业务记录禁止改本金，未知字段422、非法值400、不存在404。
删除清理账号专属业务及提醒/通知，文件失败409并保留数据；SQL需要schema 8。
旧创建、费用和冻结入口继续兼容，详细口径见[账号生命周期](reference/trading-ai.md#paper-6-api-设计apipaper-薄胶水--域模块)。
新增 `paper_changed` 事件只刷新相关账号、对比与提醒，不重载页面。

`POST /api/custom-signals`早盘清仓定义使用`template=morning_liquidation`、
`timeframe=intraday`、`kind=exit`、空`conditions`，`params`为
`{"time":"10:30:00","fallback_time":"10:45:00","rise_pct":6,"fall_pct":6}`。
时间09:30:00～11:30:00，支持秒且兜底晚于止损；涨跌幅须有限正数且≤100，百分数按6传入。
现价相对精确前交易日原始昨收计算；跌幅填写正数，未知参数或混用旧振幅拒绝。
仅`time/amplitude_pct`旧定义保持原振幅及单截止，编辑保存后明确转换。
`POST /api/custom-signals/intraday/replay`使用原日期/证券字段，另须提供1～200条
`positions`，每条含所选`symbol`与已确认交易日`entry_date`，可附`account_id/cycle_id`；
回放只判断日期与信号，输入数量不代表成交或账户授权。
返回`triggers`的`reason`（rise/fall/stop_time/fallback/resume，旧定义amplitude/deadline）、`trigger_time`、
新阈值的`change_pct`为小数（0.06代表6%），
`source`（completed_minute/clock）、`diagnostics`和`execution=signal_only`。
普通无持仓信号监控拒绝此模板，通用分钟策略监控使用租户实际持仓。

内部`POST /api/custom-signals`新增可选`template`及`params`。
尾盘走强使用`template=late_session_strength`、`timeframe=intraday`、`kind=entry`、
空`conditions`；参数为`time`、`change_min_pct`、`change_max_pct`、
`pool_up_min_pct`、`ma5_1m_count`、`ma5_5m_count`，百分数按0.4/4/60传入。
默认14:30:00、0.4～4、60、7、4；时间为午后整分钟，数量为整数且至少2。
`POST /api/custom-signals/intraday/replay`回放模板必须指定`strategy_id`，
使用该策略已保存的当日股票池，响应`diagnostics`列出缺快照或数据日期，
`symbols`仅限定输出、不能改变上涨占比分母。现有普通信号请求兼容，无新增Tier A授权。

内部会话接口`GET /api/screener/daily-stock-pool/{strategy_id}`以必填`as_of`
查询当日基础池，返回`pool`（尚未生成时null），可选`record_id`选择快照；
`limit`为1～1000、`offset`非负，完整数量在`total`，`symbols`为当前页。
名单与策略最终结果独立；此接口遵循现有租户会话权限，不新增Tier A开放端点。
新生成名单只满足股票范围和流通市值约束，`basic_filter`保存入池所用配置，
`definition_version`为`scope_float_cap_v2`；历史快照缺版本字段时保留原口径，
不因读取新接口而重算或重新标注。

核对日期：2026-11-01。接口行为以运行实例生成的 OpenAPI 和对应源码为准。
本文提供调用入口和授权边界；完整字段、单位、Provider 和事件协议见
[接入参考](reference/integrations.md)。

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

## 4. K 线与历史结果兼容

单策略运行接口`POST /api/screener/run_preset`和`POST /api/strategies/run`
省略`timeframe`或传null时，使用该策略声明的首个周期；
s02自动采用`1m`，普通日线策略采用`1d`。
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
