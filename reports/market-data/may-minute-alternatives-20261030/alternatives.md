# 2026年5月1分钟K线替代数据源

核查日期：2026-10-30。范围沿用TSP现有沪深股票池、上市日期和北京时间分钟键口径。下表确认的是供应商官方声明的历史范围和接口字段；尚未使用这些供应商的授权账号采集、验收5月全市场数据，因此不能宣称任一家已实测达到99%。

## 可覆盖目标日期的主要选择

| 方案 | 官方历史范围 | 目标字段 | 获取条件 | 对当前项目的意义 |
| --- | --- | --- | --- | --- |
| 通达信官方云端 `tdxaidata/tqserver` | 1m从2024年9月至今，包含2026年5月。[1](#s1) | Open/High/Low/Close/Volume/Amount。[1](#s1) | 通达信数据服务Key及积分；官方SDK支持Windows/macOS/Linux。[2](#s2) | 保留通达信来源的优先候选；新增SDK/provider接入，不能直接作为rustdx的7709备选IP |
| TickFlow Pro，或具备对应权限的更高套餐 | 官网Pro说明为“一年分钟级历史”，包含当前日期回溯的2026年5月。[3](#s3) | OHLC、volume、amount；`klines.get/batch`支持指定起止时间。[4](#s4) | 完整服务API Key及历史分钟权限；免费服务没有分钟K。[5](#s5) | 项目已有历史分钟采集链，预计接入工作最少；仍须验证实际账号权限和5月覆盖 |
| Tushare `stk_mins` | 超过10年历史；权限页标明历史分钟始于2009年。[6](#s6)[7](#s7) | OHLC、vol为股、amount为元。[6](#s6) | 单独开通“A股历史分钟”；个人公开价2000元/年，机构另计。[7](#s7) | 费用公开，Python SDK或HTTP API可接入，适合有明确预算的历史补齐 |
| 聚宽JQData `get_price` | 正式账号2005年至今；试用开放距今15个月至3个月，2026年5月在当前试用窗口内。[8](#s8)[9](#s9) | OHLC、volume、money。[8](#s8) | 申请账号；官方试用3个月、每日100万条、1个连接，中国大陆可用。[9](#s9) | 免费验证的优先候选；全市场需分日处理并核实实际扣量/权限 |
| 米筐RQData `get_price` | 官方研究平台说明A股分钟数据从2005年至今。[10](#s10) | OHLC、volume、total_turnover。[11](#s11) | 商业数据账号及相应流量/历史权限，按授权报价 | 适合长期维护、较长历史及量额交叉核对 |

上述字段均来自各家真实K线接口，区别于把分时价格或压缩成交记录估算成K线。历史范围包含目标日期，并不自动证明所有目标证券、所有交易分钟均无缺口。

## 推荐顺序

**希望继续采用通达信来源：先验证官方云端 `tdxaidata`。** 官方文档直接覆盖目标日期，支持Linux和macOS，后台模式不要求启动通达信客户端。需要在`TdxAiData.ini`配置通达信商城“积分和Key管理”创建的数据服务Key。积分成本按实际查询与账号政策核实，本次没有查到可直接套用的全市场5月固定总价。[1](#s1)[2](#s2)

**希望尽快接入现有TSP：先验证TickFlow历史分钟权限。** 现有代码保留`TickFlowProvider.get_klines(freq='1m')`和`kline_sync`中的历史分钟批量路径。`get_minute`本身是占位方法，不能只调用它测试就认定供应商无数据；需走现有同步调用链。设置页目前没有TickFlow可选入口，旧部署仍有配置/API兼容路径，因此不将其描述为“网页上一键切换”。[12](#s12)

**希望先免费取得样本：申请JQData试用。** 当前日期下5月处于试用历史窗口，每日配额支持分日验证和下载。不能把每日100万条直接当作一次全市场采集成功的保证，实际配额扣量、试用模块权限及到期时间以账号查询为准。[9](#s9)

**希望购买费用透明的独立历史接口：Tushare `stk_mins`。** 官方单次上限8000行，每分钟500次，允许按代码/时间循环取数。5月每只全月上市股票按现有240分钟口径约4320根，适合按股票分块，保留额外竞价记录后再做口径对齐。2000元/年是个人历史分钟权限价格，不能当作机构报价。[6](#s6)[7](#s7)

RQData可作为长期主源或第二来源。这里未对供应商之间的数据质量、实际速度或完整率作未经实测的排名。

## 新节点与其他路径

本次没有找到经过5月完整性验证、可直接加入`RUSTDX_FALLBACK_SERVERS`的新7709节点IP。此前41个既有节点的实测结论仍有效；“另一个IP能连通”不能替代完整5月档案认证。[13](#s13)

通达信官方云后台是一条新的数据服务接入路径。`tdxaidata`通过官方动态库和账号Key获取云端数据，需新adapter或SDK桥接。若要求采集程序仍使用Rust，可以由Rust调用一个包装官方SDK的本地HTTP服务，或在核实ABI后做动态库接口适配；这些适配本次未实现。

通达信官方文档中的`http://127.0.0.1:17709/`是**本地客户端模式**，要求启动支持TQ的客户端，不能把该本地HTTP地址当作跨平台云端公开API或新的7709历史站。[14](#s14)

QMT/XtQuant也是候选：官方支持真实存储的1m周期，使用`download_history_data`下载后读取。可获得的数据与MiniQMT使用的服务器一致；没有现成账号/服务器历史范围的实测，暂不将其列为已确认可以覆盖5月的方案。[15](#s15)

已下载且确实覆盖5月的真实分钟档案也可以用于离线导入。当前未取得新的完整档案，不能通过改变文件名、填零或把5分钟K拆成1分钟K恢复真实历史。

## 99%验收与接入要点

先对此前59只整月缺口证券取完整5月，核查日期边界、OHLC、量额、重复键和源返回缺口；随后扩大到现有完整股票池。这是筛选新源的顺序，不是已经完成的采集结果。[13](#s13)

沿当前基线，应有22,406,400键，99%要求至少22,182,336个有效唯一键。存在未上市/停牌等状态时，必须按项目已经采用的规则处理并披露缺口；新增历史退市证券或北交所范围时，需单独扩池、重算分母，不能直接沿用该数字。[13](#s13)

接入时有四项直接影响真实性：

1. 返回源分钟OHLC和源成交额，不能用价乘量估算成交额。
2. 关闭自动填充。通达信`fill_data=False`；RQData注意默认停牌补齐，使用适当的`skip_suspended`；聚宽注意`skip_paused/fill_paused`。零成交的源记录与客户端合成补齐记录必须区分。[1](#s1)[8](#s8)[11](#s11)
3. 明确不复权及单位。通达信`dividend_type='none'`，其`Amount`文档单位为万元，进入TSP前须乘10000转元；Tushare `vol`为股、`amount`为元。保留TSP存量价格/量基准，不直接混写。[1](#s1)[6](#s6)
4. 对齐北京时间与竞价归属。Tushare示例含09:30额外竞价记录，不能简单删除后遗漏开盘量额；依据供应商规则与源日K/分钟K对账。日期查询须覆盖5月29日收盘，且不因默认午夜结束参数漏掉末日。[6](#s6)[8](#s8)

TSP已有自定义`minute`插件/HTTP数据契约，可承接新源，标准字段为`symbol, datetime, open, high, low, close, volume, amount`。历史与盘中分钟落盘当前共用`minute`路由；只有历史权限的源，不能直接当作已支持盘中分钟采集的完整替代品。[16](#s16)

## 官方资料与本地事实依据

<a id="s1"></a>
1. [通达信：获取K线行情get_market_data](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1ctuhthaq5qmg/mindoc-1h10g60jt68sc.html)。历史1m范围、字段、价格/量额单位、填充及分页规则。

<a id="s2"></a>
2. [通达信：TdxAiData简介与跨平台安装/Key](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1hjbgqpdhv114.html)。

<a id="s3"></a>
3. [TickFlow官网：Pro一年分钟历史说明](https://tickflow.org/)。

<a id="s4"></a>
4. [TickFlow Python SDK：历史K线、字段、起止时间与批量](https://docs.tickflow.org/zh-Hans/sdk/python-quickstart)。

<a id="s5"></a>
5. [TickFlow完整/免费服务及地址](https://docs.tickflow.org/zh-Hans/quickstart)。

<a id="s6"></a>
6. [Tushare：stk_mins历史分钟接口](https://tushare.pro/wctapi/documents/370.md)。

<a id="s7"></a>
7. [Tushare：独立历史分钟权限、价格及频次](https://tushare.pro/document/2?doc_id=290)。

<a id="s8"></a>
8. [聚宽：get_price范围、分钟字段、复权与停牌规则](https://www.joinquant.com/help/api/doc?id=9990&name=JQDatadoc)。

<a id="s9"></a>
9. [聚宽：JQData试用窗口、期限、每日配额](https://www.joinquant.com/help/api/doc?name=logon&id=9830)。

<a id="s10"></a>
10. [米筐：A股2005年以来分钟历史](https://www.ricequant.com/doc/quant/research)。

<a id="s11"></a>
11. [RQData：get_price参数与量额字段](https://www.ricequant.com/doc/rqdata/python/generic-api.html#rqdata-API-get_price-genericapi)；另核对了[官方Markdown原文](https://www.ricequant.com/doc/sources/rqdata/python/generic-api.md)。

<a id="s12"></a>
12. [现有TickFlow适配源码](../../../backend/app/data_providers/tickflow_provider.py)、[现有历史分钟同步调用链](../../../backend/app/services/kline_sync.py)、[配置兼容说明](../../../docs/reference/integrations.md#rustdx)。

<a id="s13"></a>
13. [本轮rustdx覆盖率实测](../rustdx-may-99-20261030/report.md)、[有效键与缺口基线](../rustdx-may-99-20261030/fresh-verification.json)、59证券缺口见证（附件保存在受控归档）。

<a id="s14"></a>
14. [通达信：本地HTTP方式调用及客户端条件](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1hdhbmi50d038.html)。

<a id="s15"></a>
15. [迅投：XtData运行方式、历史下载与1m周期](https://dict.thinktrader.net/nativeApi/xtdata.html)。

<a id="s16"></a>
16. [TSP自定义数据源与分钟时间/字段契约](../../../docs/reference/integrations.md#plugin-get_minute-的-datetime-时区契约)。

公开范围/价格资料均于2026-10-30检索。各供应商具体账号权限与5月全市场覆盖均标记为“未实测”；本次没有购买服务、创建账号或变更生产行情数据。
