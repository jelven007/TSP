# TSP 数据能力与 eltdx 3.3.0 接口对照

核对日期：2026-10-25。项目基线：`8b669fb5ff96606dce60349f088aea736dba6640`。

核对对象为当前项目的标准数据契约及默认 rustdx 实现，目标接口为 PyPI 发布的 **eltdx 3.3.0**，不是 eltdx 开发分支。本文是静态接口与语义对照，不表示项目已经接入 eltdx，也不表示两种客户端已经通过实际行情一致性或性能验收。

## 1. 接口总表

| 项目能力 | 当前 rustdx 取数入口 | eltdx 3.3.0 对应入口 | 协议与适配判断 |
| --- | --- | --- | --- |
| 实时行情 `realtime` | `get_realtime()` / `_quote_rows()` → `client.quotes()` | 直接对应：`client.quotes.legacy(codes)`；基础行情另可使用 `client.quotes.get_snapshots(codes)` | 当前报价协议为 `0x053e`；基础快照为 `0x054c`。价格、量额和一档盘口可映射，代码、百分比和时间依据需要转换 |
| 五档盘口 `depth5` | `get_depth_batch()` → `client.quotes()` | 直接对应：`client.quotes.legacy(codes)`；专门入口：`client.quotes.get_depth(codes)`；组合入口：`client.helpers.full_quotes(codes)` | `0x053e` 自带五档；`get_depth` 使用 `0x0547`；`full_quotes` 组合 `0x054c + 0x0547`。需检查完整五档是否实际返回 |
| 分钟K `minute` | `get_minute()` → `client.bars(frequency=...)` | `client.bars.get(code_or_codes, period="1m"/"5m"/"15m"/"30m"/"60m", adjust="none", ...)` | `0x052d`。按周期名、OHLCV和北京时间映射；分钟起点/终点标签须验证 |
| 日K `daily` | `get_daily()` / `iter_daily()` → `client.bars(frequency=9)` | `client.bars.get(code_or_codes, period="day", adjust="none", ...)` | `0x052d`。入库必须是不复权价格，日期和成交量单位需统一 |
| 除权因子 `adj_factor` | `get_adj_factors()` → `client.xdxr()` + 事件前日K → 本地计算 `ex_factor` | 保持当前契约：`client.corporate.capital_changes(codes)` + `client.bars.get(..., period="day", adjust="none")`；相关但非等价接口：`client.corporate.adjustment_factors(codes, ...)` | 输入为 `0x000f + 0x052d`。项目要求单事件比值，eltdx 系数接口返回累计仿射系数，不能直接替换 |
| 公司行为 `corporate_actions` | `get_corporate_actions()` → `client.xdxr()`，选取类别1事件 | `client.corporate.capital_changes(codes)`；登记日/方案资料可补查 `client.f10.dividend_financing(code)` | `0x000f` + 可选 F10 Entry。基础事件可映射；完整入账日期和证据仍须补齐 |
| 财务数据 `financial` | `get_financials()` → `financial_history()` → 下载 `gpcw` 专业财报 → 映射五张表 | `eltdx.financial.open_calc_client()` + `list_archive_files(calc)` + `download_archive(calc, filename)` / `download_records(calc, filename)` | 下载协议 `0x06b9`。这条路径最接近当前完整财务实现；`corporate.finance_batch()` 只是基础财务快照 |

项目实现依据：[数据源 Provider](../backend/app/plugins/rustdx/provider.py)、[财报解析器](../backend/app/plugins/rustdx/financial.py)。目标接口依据：[eltdx 3.3.0 发布包](https://pypi.org/project/eltdx/3.3.0/)、[版本固定源码](https://github.com/electkismet/eltdx/tree/v3.3.0)。

## 2. 逐项字段与语义

### 2.1 实时行情

当前 rustdx 的 `quotes()` 使用 `0x053e`，返回基础行情及完整五档。项目实时记录保留基础行情和买一、卖一；五档能力由独立路由和服务消费。`quotes.legacy()` 是 eltdx 中的直接协议对应入口。

`quotes.get_snapshots()` 使用 `0x054c`，足以提供核心价格、成交量额和一档盘口。它是另一条行情快照路径，需要单独验证与旧协议的字段、证券覆盖及时间语义，不能仅按方法名认定数值完全一致。

| eltdx 字段 | 项目字段 | 转换 |
| --- | --- | --- |
| `exchange` + `code` / `full_code` | `symbol` | `sh600519` → `600519.SH`；保留交易所身份，不只靠六位代码判断 |
| `last_price` | `last_price` | 元，不复权 |
| `pre_close_price` | `prev_close` | 元 |
| `open_price` / `high_price` / `low_price` | `open` / `high` / `low` | 当日原始价 |
| `total_hand` | `volume` | 手，直接映射，不再除100 |
| `amount` | `amount` | 元 |
| `change` | `change_amount` | 元 |
| `change_pct` | `change_pct` | eltdx 是百分数值；项目实时入口是小数制，需 `/100` |
| `buy_levels[0]` / `sell_levels[0]` | `bid1` / `ask1` 及对应挂单量 | 价格为元、数量为手；缺档保留未知 |
| `server_time_raw`（旧快照）/ `time_raw`（基础快照） | 时间原始信息 | 不能未经日期、时区与语义认证直接当作 Unix 毫秒时间戳 |

名称由证券目录关联，不需要每轮刷新目录。当前 rustdx 把本地接收时间写入 `timestamp`，同时声明 `timestamp_basis="received"`，`source_timestamp` 留空。接入 eltdx 后仍应明确区分接收时间、源端事件时间和源端快照时间；更换协议不能自动证明报价日期或收盘定版状态。

振幅和换手率不是快照接口必备输出：当前 Provider 先留空，下游用价格、成交量和股本计算。财务股本为低频输入，不影响换手率随实时成交量变化。

依据：[项目快照字段](../backend/app/plugins/rustdx/provider.py)、[行情时间契约](../backend/app/services/quote_time.py)、[eltdx 报价模型](https://github.com/electkismet/eltdx/blob/v3.3.0/src/eltdx/models/quote.py)、[基础快照文档](https://github.com/electkismet/eltdx/blob/v3.3.0/docs/methods/7709-批量快照.md)。

### 2.2 五档盘口

三种入口的返回与请求语义不同：

| 入口 | 返回 | 特点 |
| --- | --- | --- |
| `client.quotes.legacy(codes)` | `list[LegacyQuote]` | `0x053e` 单次完整快照，包含五档；大量代码由调用方分批 |
| `client.quotes.get_depth(codes)` | `QuoteRefreshPage`，数据在 `.records` | `0x0547`；以空游标初始化五档，单次最多100个代码 |
| `client.helpers.full_quotes(codes)` | `list[QuoteSnapshot]` | 每批最多80个代码，组合基础快照与五档刷新 |

`get_depth()` 实质调用 `refresh(codes, cursors={})`。持续游标刷新和推送分别使用 `quotes.refresh()` 与 `quotes.poll_push()`，需要维护其状态，不能把所有调用都当作无状态的完整快照。

项目字段映射：

| eltdx 字段 | 项目字段 |
| --- | --- |
| `buy_levels[i].price` | `bid_prices[i]` |
| `buy_levels[i].volume` | `bid_volumes[i]` |
| `sell_levels[i].price` | `ask_prices[i]` |
| `sell_levels[i].volume` | `ask_volumes[i]` |

价格单位为元、盘口数量单位为手。`full_quotes()` 在五档刷新没有可合并记录时会保留基础快照，因此“Helper 调用成功”不能证明已经具备完整五档。Provider 必须检查档位和覆盖率，再按项目现有空值契约输出。

依据：[项目五档映射](../backend/app/plugins/rustdx/provider.py)、[3.3.0 五档与推送](https://github.com/electkismet/eltdx/blob/v3.3.0/docs/methods/7709-增量刷新推送队列.md)、[完整行情 Helper](https://github.com/electkismet/eltdx/blob/v3.3.0/docs/helpers/完整行情.md)、[报价 API 实现](https://github.com/electkismet/eltdx/blob/v3.3.0/src/eltdx/api/quotes.py)。

### 2.3 分钟K

接口形状：

```python
series = client.bars.get(
    "sz000001",
    period="1m",
    adjust="none",
    count=800,
)
```

这是调用示例，本文未执行行情请求。其他当前项目周期为 `5m`、`15m`、`30m`、`60m`。列表输入返回 `dict[str, KlineSeries]`，单代码输入返回 `KlineSeries`，数据在 `.bars`。

| eltdx K线字段 | 项目字段 | 转换 |
| --- | --- | --- |
| `bar.time` | `datetime` | 转成北京时间，再去掉时区，保存北京时间墙钟 |
| `bar.open/high/low/close` | 同名字段 | 不复权价格 |
| `bar.volume_lots` | `volume` | 已是手，直接使用 |
| `bar.amount` | `amount` | 元 |

当前 rustdx 的分钟原始 `vol` 以股计，Provider 已 `/100`；eltdx 的 `volume_lots` 已完成换算，不能再复制这一步转换。

eltdx `bar_time="start"` 是默认值，`"end"` 会按分钟周期移动时间标签。当前 rustdx 保留源标签，且明确兼容实时开端标签与历史结束标签，故接入时必须用同标的、同周期、同交易时段样本确认对应关系。不能直接把全部数据平移一分钟或五分钟。

`all_pages=True` 支持自动翻页；业务日期窗口仍由适配层筛选。单次协议请求是一只证券、最多800根，多标的列表通过连接池并发调度。

当日分时 `client.minutes.today()` 返回价格、成交量、均价序列，不等同于含完整 OHLCV 的分钟K。当前项目分钟K应对应 `bars.get()`。

依据：[项目分钟标准化](../backend/app/plugins/rustdx/provider.py)、[3.3.0 K线文档](https://github.com/electkismet/eltdx/blob/v3.3.0/docs/methods/7709-K线周期线.md)、[K线 API](https://github.com/electkismet/eltdx/blob/v3.3.0/src/eltdx/api/bars.py)。

### 2.4 日K

对应调用为 `client.bars.get(code_or_codes, period="day", adjust="none", ...)`。

项目标准行是 `symbol/date/open/high/low/close/volume/amount`，日期从北京时间的 `bar.time` 提取，量使用 `volume_lots`。指数可用 SDK 的自动识别，也可显式传 `kind="index"`；ETF按证券身份路由，不能只凭裸代码判断。

虽然 eltdx 支持 `adjust="qfq"`、`"hfq"` 和定点复权，但当前项目原始日K必须入库为不复权价格。前复权由项目的 `adj_factor` 和 enriched 流水线处理。直接把服务端前复权价格当原始日K写入会破坏原始价与复权价的边界。

盘中项目也会用实时快照更新当日 OHLCV 展示与缓存，盘后用日K接口恢复权威历史值。历史日K接口与盘中快照入口应分别映射。

依据：[项目日K取数](../backend/app/plugins/rustdx/provider.py)、[复权消费者](../backend/app/indicators/pipeline.py)、[3.3.0 K线文档](https://github.com/electkismet/eltdx/blob/v3.3.0/docs/methods/7709-K线周期线.md)。

### 2.5 除权因子

项目要求：

```text
symbol / trade_date / ex_factor
ex_factor = 单次事件的除权前价格 / 除权参考价格
```

当前 rustdx 从 `xdxr()` 选取类别1事件，获取事件之前最近交易日的原始收盘价，然后本地计算。对应的 eltdx 输入组合是：

```text
client.corporate.capital_changes(codes)
+ client.bars.get(..., period="day", adjust="none")
+ 项目原有事件聚合、参考价舍入及因子计算
```

类别1字段的映射：

| eltdx 字段 | 当前项目计算输入 |
| --- | --- |
| `date` | 除权事件日 |
| `c1_value / 10` | 每股现金分红 |
| `c2_value` | 配股价，元 |
| `c3_value / 10` | 每股送转数量 |
| `c4_value / 10` | 每股配股数量 |

使用每股口径时，当前项目计算为：

```text
参考价 = (前收盘价 - 每股现金分红 + 每股配股数量 × 配股价)
         / (1 + 每股送转数量 + 每股配股数量)

参考价按现有规则四舍五入：股票2位，ETF3位
ex_factor = 前收盘价 / 舍入后的参考价
```

还要保留同日多事件聚合、缺配股价和缺前收盘价的处理，以及现有除权后收益自检。

eltdx `client.corporate.adjustment_factors()` 是相关能力，但输出为事件日期上的累计 `qfq_scale/qfq_offset/hfq_scale/hfq_offset`：

```text
adjusted_price = raw_price × scale + offset
```

这是仿射变换。它的系数粒度、累计方式和数值语义均不同于项目的单事件 `ex_factor`；取 `qfq_scale`、取其倒数、或把累计系数直接改名都不满足当前契约。保持现有消费者时，应复用原有单事件因子算法。

依据：[当前因子生成](../backend/app/plugins/rustdx/provider.py)、[参考价舍入](../backend/app/plugins/rustdx/provider.py)、[项目累计应用规则](../backend/app/indicators/pipeline.py)、[3.3.0 本地复权系数](https://github.com/electkismet/eltdx/blob/v3.3.0/docs/methods/7709-本地复权系数.md)。

### 2.6 公司行为

基础事件对应 `client.corporate.capital_changes(codes)` 的类别1记录，与除权因子共用权息数据，但公司行为输出是现金和股份权益事件，不是价格系数。

| eltdx 类别1字段 | 项目公司行为字段 |
| --- | --- |
| `date` | `ex_date` |
| `c1_value / 10` | `cash_per_share` |
| `c3_value / 10` | `bonus_per_share` |
| `c4_value / 10` | `rights_per_share` |
| `c2_value` | `rights_price` |

项目还要求稳定事件 `id`、`symbol`、`source`、`asset_type`。类别2–15描述其他类型股本和权息变化，不能把每条记录都按类别1解释。

完整模拟交易入账还依赖：

- `record_date`：股权登记日。
- `cash_pay_date`：现金到账日。
- `bonus_listing_date`：送转股份上市日。
- `rights_start_date`、`rights_end_date`、`rights_listing_date`：配股缴款和上市日期。
- `dates_verified`、`evidence`：日期认证和真实来源证据。

当前 rustdx 仅生成事件金额、股份和除权日，未提供完整日期证据。eltdx 的 `capital_changes()` 也不能单凭类别1事件补出上述全部日期。

`client.f10.dividend_financing(code, section="fh")` 可提供方案资料，其中 `T021` 为股权登记日、`T023` 为除权派息日。它可用于补充与交叉核对，但 `T023` 不能自动视为现金到账日，也不能证明送转上市、配股缴款及上市日期全部齐备。证据不齐时须继续保留未认证状态。

依据：[项目公司行为转换](../backend/app/plugins/rustdx/provider.py)、[公司行为模型与证据要求](../backend/app/data_providers/corporate_actions.py)、[3.3.0 股本变迁](https://github.com/electkismet/eltdx/blob/v3.3.0/docs/methods/7709-股本变迁GBBQ.md)、[分红融资资料](https://github.com/electkismet/eltdx/blob/v3.3.0/docs/methods/F10-分红融资.md)。

### 2.7 财务数据

当前项目 `financial` 包含五张表，不能只用一份最新基础财务快照代表它：

| 项目财务表 | 当前 gpcw 映射示例 |
| --- | --- |
| `metrics` | `col1` → `eps_basic`；`col4` → `bps`；`col281` → `roe`；另含毛利率、净利率、增长率、资产负债率 |
| `income` | `col74` → `revenue`；`col95` → `net_income`；`col96` → `net_income_attributable` |
| `balance_sheet` | `col40` → `total_assets`；`col63` → `total_liabilities`；`col72` → `total_equity` |
| `cash_flow` | `col107` → `net_operating_cash_flow`；`col119` → 投资现金流；`col128` → 筹资现金流 |
| `shares` | `col238` → `total_shares`；`col239` → `float_shares` |

所有表还需要 `symbol/period_end/announce_date`。当前公告日期来自 `col314`，缺失或无效公告日期的记录不会作为有效历史财务输出。当前 Provider 最新查询先取4期再选每股最新一行，历史查询取8期；存储层会保留及合并已有历史。

**与当前实现最接近的 eltdx 路径是专业财报模块：**

```python
from eltdx.financial import (
    open_calc_client,
    list_archive_files,
    download_archive,
    download_records,
)
```

| 步骤 | 入口 | 返回 |
| --- | --- | --- |
| 连接财报计算服务器 | `open_calc_client()` | 指向计算服务器的 `TdxClient` |
| 查询可用财报文件 | `list_archive_files(calc)` | `FinancialFileInfo` 列表，包含文件名、摘要、大小 |
| 下载原始财报 | `download_archive(calc, filename)` | ZIP 字节 |
| 下载并解析财报 | `download_records(calc, filename)` | `FinancialRecord` 列表，含代码、市场、报告期和 `fields` |

底层均复用 `calc.resources.download_file(path)` 的 `0x06b9` 分块下载。清单路径是 `tdxfin/gpcw.txt`，数据文件是 `tdxfin/gpcwYYYYMMDD.zip`；下载时应核对清单文件名是否需要补 `tdxfin/` 前缀。

`FinancialRecord.fields` 是原始财务字段序列，不是已经映射好的三大报表。与当前项目的编号对应关系为 `colN = fields[N-1]`，接入时仍须保留 `_FINANCIAL_MAPS`、`col314` 日期解析、空值和有效日期检查。

eltdx 解析器提供 ZIP 与 DAT 解析和大小限制，但 `download_records()` 本身并未执行项目当前的清单 MD5/大小一致性检查、报告头日期与目标文件日期一致性检查、原始文件版本归档和公告日期认证。保留当前质量契约的做法是通过下载入口获取字节，再复用项目校验、解析、字段映射和归档链，或对新解析输出执行同等验证。

其他财务入口的定位：

| 入口 | 对当前项目的用途 |
| --- | --- |
| `client.corporate.finance_batch(codes)`，`0x0010` | 最新流通/总股本、上市日期和基础财务快照；对应当前 `get_instrument_metadata()` 使用的 `finance()`，适合维表补充 |
| `client.helpers.stock_profile_table(codes)` | 合并实时行情、名称和基础财务；不是上述五张历史财务表的替代 |
| `client.f10.finance_report(code, report_type=...)` | F10多期报表资料，可作补充；其 `Txxx` 字段、报告期和公告日期须另行核对 |
| `client.f10.finance_diagnosis(code, section=...)` | 财务能力分析和评分资料，不等同于项目 `metrics` 的原始指标字段 |

3.3.0 的 F10 财务文档确认 `zcfzb` 为资产负债表，并给出 `lrb` 调用示例；其他报表类型及完整历史覆盖以实际返回为准。不能凭报表接口名称宣称已覆盖当前财务消费需求。

依据：[当前财务五表映射](../backend/app/plugins/rustdx/provider.py)、[报告和公告日期校验](../backend/app/plugins/rustdx/provider.py)、[当前财报校验与归档](../backend/app/plugins/rustdx/financial.py)、[3.3.0 专业财报文档](https://github.com/electkismet/eltdx/blob/v3.3.0/docs/FINANCIAL.md)、[专业财报实现](https://github.com/electkismet/eltdx/blob/v3.3.0/src/eltdx/financial.py)、[基础财务快照](https://github.com/electkismet/eltdx/blob/v3.3.0/docs/methods/7709-财务基础信息.md)。

## 3. 公共输入、消费者与兼容边界

### 3.1 公共输入和全量分钟

证券目录用 `client.codes.all("sh")`、`client.codes.all("sz")` 获取代码、名称及分类，再按项目沪深市场边界筛选股票、ETF或指数。eltdx 的跨市场便捷方法默认包含北交所，不能直接当作当前项目沪深股票池。

项目第八类能力 `full_minute` 仍对应 `client.bars.get(..., period="1m", adjust="none")`，没有必要寻找一个独立的“全市场分钟”原生接口。冷启动抓取当日窗口，增量轮抓取最近若干根，适配层负责标的池、分页、落盘和完整性。它也不承诺一次网络请求获取所有证券的所有分钟。

依据：[项目全量分钟方法](../backend/app/plugins/rustdx/provider.py)、[增量分钟方法](../backend/app/plugins/rustdx/provider.py)、[3.3.0 代码表](https://github.com/electkismet/eltdx/blob/v3.3.0/docs/methods/7709-代码表.md)。

### 3.2 当前仓库内消费者

| 契约 | 当前主要消费者 |
| --- | --- |
| `realtime` | `services/quote_service.py`，行情展示、实时规则、策略监控及当日 enriched 更新 |
| `depth5` | `services/depth_service.py`，盘口展示、封单和相关监控 |
| `minute` / `full_minute` | `services/kline_sync.py`、`services/minute_refresh.py`，分时图、分钟回测与板块分钟计算 |
| `daily` | `services/kline_sync.py`、仓库和指标流水线，策略、分析与回测 |
| `adj_factor` | `indicators/pipeline.py`、`services/minute_adjust.py`，日线及分钟的复权计算 |
| `corporate_actions` | `services/corporate_actions.py`、`strategy/paper.py`，事件持久化与模拟账户权益 |
| `financial` | `services/financial_sync.py`、财务分析及历史股本补全 |

本文覆盖仓库内主要消费者，未盘点外部 Open API 调用方。供应商接口应在 Provider 边界适配，不能直接把 eltdx 返回模型传给现有消费方。

### 3.3 兼容性矩阵

| 变化 | 判断 | 必须保留的契约 |
| --- | --- | --- |
| 更换客户端、保持标准字段及数值语义 | 条件兼容 | 代码、单位、日期、空值、证券覆盖和失败行为均相同 |
| eltdx `change_pct` 未转换就下发 | 不兼容 | 百分数值转小数制 |
| 已是手的 `volume_lots` 再 `/100` | 不兼容 | 日K、分钟K、实时及盘口数量最终均使用手 |
| 入库改为服务端前复权日K | 不兼容 | 原始价和复权价分开，继续由现有因子消费者计算 |
| 直接用仿射系数替换 `ex_factor` | 不兼容 | 单事件 pre/post 比值及现有累计规则 |
| 仅有除权日即宣称公司行为可入账 | 不兼容 | 登记、到账、缴款和上市日期及证据 |
| 用最新财务快照替代历史五张表 | 不兼容 | 报告期、公告日、历史字段和版本留存 |
| 更换报价为刷新/推送协议 | 条件兼容 | 初始化、游标、缓存合并、掉线恢复及未返回字段的含义 |
| 更换分钟起点/终点标签 | 条件兼容 | 对齐已有分钟消费者的时间边界，不能产生跨分钟交易时点变化 |

当前没有实施任何契约变更或弃用。后续若只增加 Provider，优先保持消费方契约；若改变复权算法、分钟时间标签或账务证据语义，应作为独立的语义变更评估历史数据和回测影响。

## 4. 核对结果与后续接入验证

本次已完成：

- 读取当前项目 Provider、财报解析器、公司行为模型、复权消费者和相邻契约测试。
- 对照 PyPI eltdx 3.3.0 发布包中的文档与 Python 源码，而非使用旧版扁平 API 名称。
- 用 AST 静态检查确认表中涉及的报价、K线、权息、基础财务、专业财报、目录及 Helper 入口真实存在。
- 核对报价、K线、权息和财报下载的底层命令对应关系。

本次未执行 eltdx 安装、真实行情取数、数据源替换、数值一致性测试或性能压测。文档中的调用示例仅用于说明接口形状。

如果后续实施接入，验证至少应覆盖：

| 验证范围 | 验证重点 | 可复用的当前测试入口 |
| --- | --- | --- |
| Provider字段 | 沪深同码、股票/ETF/指数、比例、成交量单位、缺字段和空响应 | `test_rustdx_contracts.py`、`test_rustdx_provider.py` |
| 分钟与日线 | 同标的同窗口 OHLCV、开收盘标签、时区、分页、未完成分钟 | `test_minute_timezone_contract.py`、`test_rustdx_sync.py` |
| 因子 | 现金分红、送转、配股、同日事件、ETF舍入、缺前收盘价 | `test_rustdx_contracts.py`、`test_etf_adj_factor.py` |
| 公司行为 | 日期证据缺失不入账、权益阶段、重复执行和修订冲突 | `test_paper_corporate_actions.py` |
| 财务 | 文件校验、报告日期、`colN` 映射、公告日期、历史合并和版本留存 | `test_rustdx_financial.py`、`test_rustdx_contracts.py` |
| 实时运行 | 整轮完成间隔、报价覆盖、盘口缺档、重连和前端可读延迟 | 原生池与报价压测入口；真实交易时段另行观测 |

这是一份接口与契约对照，不是“可直接替换”的验收结论。
