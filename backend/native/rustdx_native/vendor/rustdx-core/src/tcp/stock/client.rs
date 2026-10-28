//! 通达信行情高层客户端。
//!
//! 封装连接管理与各协议请求，
//! 返回数据与显式状态；K线代码借用输入字符串：
//!
//! ```ignore
//! use rustdx_complete::tcp::stock::Client;
//!
//! let mut client = Client::new()?; // 内置服务器故障转移
//! let quotes = client.quotes(&[(0, "000001"), (1, "600519")])?;
//! for q in quotes {
//!     println!("{}: {:.2} ({:+.2}%)", q.code, q.price, q.change_percent);
//! }
//! ```

use super::{
    BlockRecord, CompanyInfoCategoryItem, FinanceInfoData, FinanceSnapshot, HistoryMinuteTime,
    IndexKlineData, KlineData, KlineRequest, MarketPrecision, MinuteTimeData, QuoteData,
    QuoteMetadataStore, QuotePage, SecurityListData, TransactionData, Xdxr, XdxrData,
};
use crate::pool::{ConnectionPool, PoolConfig};
use crate::tcp::helper::DateTime;
use crate::tcp::{Tcp, TcpConfig, Tdx};
use chrono::Datelike;
use std::sync::{Arc, Mutex};

mod adjustment;
mod bar_range;
mod batch;
pub(crate) mod batch_budget;
mod extended_queries;
mod stream;
mod stream_queries;
mod transactions;
mod turnover;
mod version_330;
use adjustment::{adjusted_multipliers, checked_ex_right_reference};
pub use bar_range::{
    BarRangeCollection, BarRangeCoverage, BarRangeOptions, BarRangePage, BarRangeSource,
    BarRangeTermination,
};
pub use batch_budget::BatchResultBudget;
pub use stream::{BarBatchStreamOptions, BatchBarRange, BatchStreamOptions, BatchStreamSummary};

/// 高层客户端：持有连接，提供行情查询与复权计算 API。
#[derive(Debug)]
pub struct Client {
    /// 底层连接（需要更细粒度控制时可直接使用）。
    pub tcp: Tcp,
    batch_lock: batch::BatchGate,
    batch_pool: Mutex<Option<(usize, TcpConfig, ConnectionPool)>>,
    quote_metadata: Arc<QuoteMetadataStore>,
    batch_result_budget: BatchResultBudget,
}

impl Client {
    /// 连接服务器（内置故障转移），等价于 `Tcp::new()`。
    pub fn new() -> std::io::Result<Self> {
        Self::with_config(&TcpConfig::default())
    }

    /// 以指定配置连接服务器。
    pub fn with_config(config: &TcpConfig) -> std::io::Result<Self> {
        Self::with_config_until(
            config,
            crate::tcp::governance::deadline_after(crate::tcp::network_policy().connect_timeout)?,
        )
    }

    /// Capture the first handshake and all subsequent frames using the same
    /// bounded synchronous observer as the underlying managed connection.
    pub fn with_config_and_observer(
        config: &TcpConfig,
        observer: Arc<dyn crate::tcp::FrameObserver>,
    ) -> std::io::Result<Self> {
        Ok(Self {
            tcp: Tcp::with_config_and_observer(config, observer)?,
            batch_lock: batch::BatchGate::default(),
            batch_pool: Mutex::new(None),
            quote_metadata: Arc::new(QuoteMetadataStore::default()),
            batch_result_budget: BatchResultBudget::default(),
        })
    }

    pub(crate) fn with_config_until(
        config: &TcpConfig,
        deadline: std::time::Instant,
    ) -> std::io::Result<Self> {
        Ok(Self {
            tcp: Tcp::with_config_until(config, deadline)?,
            batch_lock: batch::BatchGate::default(),
            batch_pool: Mutex::new(None),
            quote_metadata: Arc::new(QuoteMetadataStore::default()),
            batch_result_budget: BatchResultBudget::default(),
        })
    }

    /// Applies to every Vec batch method. Errors keep input order; budget
    /// exhaustion returns an outer error and never a truncated successful Vec.
    pub fn set_batch_result_budget(&mut self, budget: BatchResultBudget) -> std::io::Result<()> {
        budget.validate()?;
        self.batch_result_budget = budget;
        Ok(())
    }

    pub fn batch_result_budget(&self) -> BatchResultBudget {
        self.batch_result_budget
    }

    /// Metadata sharing is explicit; default Client instances remain isolated.
    pub fn with_quote_metadata(
        config: &TcpConfig,
        metadata: Arc<QuoteMetadataStore>,
    ) -> std::io::Result<Self> {
        let mut client = Self::with_config(config)?;
        client.quote_metadata = metadata;
        Ok(client)
    }

    pub fn quote_metadata_store(&self) -> Arc<QuoteMetadataStore> {
        Arc::clone(&self.quote_metadata)
    }

    pub fn quote_metadata_status(&self) -> std::io::Result<Vec<MarketPrecision>> {
        Ok(self.quote_metadata.snapshot()?.status())
    }

    /// Explicit warmup shares the pagination deadline. No I/O occurs under a cache lock.
    pub fn refresh_quote_metadata(
        &mut self,
        markets: &[u16],
    ) -> std::io::Result<Vec<MarketPrecision>> {
        let store = &self.quote_metadata;
        self.tcp
            .with_timeout(crate::tcp::network_policy().pagination_timeout, |tcp| {
                let snapshot = super::quote_metadata::ensure_metadata(tcp, store, markets, true)?;
                Ok(snapshot.status())
            })
    }

    /// 重连（见 [`Tcp::reconnect`]）。
    pub fn reconnect(&mut self) -> std::io::Result<()> {
        self.tcp.reconnect()
    }

    /// 心跳（见 [`Tcp::heartbeat`]）。
    pub fn heartbeat(&mut self) -> std::io::Result<crate::tcp::HeartbeatAck> {
        self.tcp.heartbeat()
    }

    /// 请求失败时自动重连重试（见 [`Tcp::retry`]）。
    pub fn retry<T>(
        &mut self,
        f: impl FnMut(&mut Tcp) -> std::io::Result<T>,
        attempts: usize,
    ) -> std::io::Result<T> {
        self.tcp.retry(f, attempts)
    }

    /// 实时行情快照。返回 owned 数据。
    ///
    /// [`TcpConfig::recheck_empty`](crate::tcp::TcpConfig::recheck_empty)
    /// 开启时空结果二次确认（等 300ms → 重连 → 重拉一次）——盘中高频轮询
    /// 场景下服务器会静默返回空帧，二次确认可消除抖动误判。
    ///
    /// 注意：非交易时段 quotes 本就为空，业务层应按时段判断后再调用，
    /// 否则每次都会多付一次重查代价。
    pub fn quotes(&mut self, stocks: &[(u16, &str)]) -> std::io::Result<Vec<QuoteData>> {
        if stocks.is_empty() {
            return Ok(Vec::new());
        }
        self.quotes_with_metadata(stocks).map(|page| page.records)
    }

    /// Cold metadata loading, quote query and optional empty recheck share one
    /// request deadline. Call refresh_quote_metadata before latency-sensitive queries.
    pub fn quotes_with_metadata(&mut self, stocks: &[(u16, &str)]) -> std::io::Result<QuotePage> {
        let request = super::QuotesRequest::new(stocks.to_vec())?;
        let store = &self.quote_metadata;
        let result = self
            .tcp
            .with_timeout(crate::tcp::network_policy().request_timeout, |tcp| {
                let markets: Vec<_> = stocks.iter().map(|item| item.0).collect();
                let snapshot = super::quote_metadata::ensure_metadata(tcp, store, &markets, false)
                    .map_err(|error| {
                        ctx_err(
                            error,
                            format_args!("报价目录加载失败；可先refresh_quote_metadata预热"),
                        )
                    })?;
                for &(market, code) in stocks {
                    snapshot.scale(market, code)?;
                }
                let first = request.recv_with_metadata(tcp, &snapshot)?;
                if !first.records.is_empty() || !tcp.config().recheck_empty {
                    return Ok(first);
                }
                tcp.delay(std::time::Duration::from_millis(300))?;
                tcp.reconnect()?;
                request.recv_with_metadata(tcp, &snapshot)
            });
        result.map_err(|e| ctx_err(e, format_args!("Client::quotes(n={})", stocks.len())))
    }

    /// 股票K线。
    ///
    /// 单页请求（最多 `count` 根）；解析层保证按时间**升序**返回。
    /// 区间回补请用 [`Client::bars_range`]（自动翻页 + 窗口过滤）。
    pub fn bars<'a>(
        &mut self,
        market: u16,
        code: &'a str,
        category: u16,
        start: u16,
        count: u16,
    ) -> std::io::Result<Vec<super::KlineData<'a>>> {
        let request = KlineRequest::new(market, code, category, start, count)?;
        request.recv_stock(&mut self.tcp).map_err(|e| {
            ctx_err(
                e,
                format_args!("Client::bars(market={market}, code={code}, category={category})"),
            )
        })
    }

    /// 按日期区间拉取任意周期 K 线（自动翻页，时间升序，去重）。
    ///
    /// 与 [`Client::k`](Self::k)（仅日线）不同，本方法支持全部 K 线种类
    /// （`category`：0=5m、1=15m、2=30m、3=1h、…、9=日线），适用于
    /// 分钟线等短周期数据的**区间回补**。
    ///
    /// 内置防护（均来自全市场回补实测）：
    /// - 自动翻页直至覆盖 `begin` 或历史尽头（页序无关，末端统一升序）；
    /// - 翻页偏移可能重叠 → 按时间戳去重；
    /// - 非法日期、重复时间戳内容冲突或来源变化返回错误；
    /// - [`TcpConfig::recheck_empty`](crate::tcp::TcpConfig::recheck_empty)
    ///   开启时在同一连接确认空页。
    ///
    /// 兼容Vec入口仍在短页停止，不能证明完整覆盖；需要终止原因及来源时
    /// 使用 [`Self::bars_range_with_options`]。
    ///
    /// # 参数
    ///
    /// - `begin`/`end`: 日期区间（YYYYMMDD，闭区间），`None` 表示不设限。
    pub fn bars_range<'a>(
        &mut self,
        market: u16,
        code: &'a str,
        category: u16,
        begin: Option<u32>,
        end: Option<u32>,
    ) -> std::io::Result<Vec<super::KlineData<'a>>> {
        let ctx =
            format_args!("Client::bars_range(market={market}, code={code}, category={category})");
        fetch_bars_range(&mut self.tcp, market, code, category, begin, end)
            .map_err(|e| ctx_err(e, ctx))
    }

    /// 指数K线，含涨跌家数。
    ///
    /// 股票和指数使用相同命令0x052d，但指数响应包含涨跌家数，
    /// 需使用指数解析器；区间语义请用 [`Client::index_bars_range`]。
    pub fn index_bars<'a>(
        &mut self,
        market: u16,
        code: &'a str,
        category: u16,
        start: u16,
        count: u16,
    ) -> std::io::Result<Vec<super::IndexKlineData<'a>>> {
        let request = KlineRequest::new(market, code, category, start, count)?;
        request.recv_index(&mut self.tcp).map_err(|e| {
            ctx_err(
                e,
                format_args!("Client::index_bars(market={market}, code={code})"),
            )
        })
    }

    /// 指数K线区间回补：[`Client::index_bars`] 的自动翻页版，
    /// 与 [`Client::bars_range`](Self::bars_range) 对称，使用指数响应解析器。
    ///
    /// - 自动翻页直至覆盖 `begin` 或历史尽头（页序无关，末端统一升序）；
    /// - 翻页偏移可能重叠 → 按时间戳去重；
    /// - 最终按 `[begin, end]` 窗口过滤。
    ///
    /// # 参数
    ///
    /// - `begin`/`end`: 日期区间（YYYYMMDD，闭区间），`None` 表示不设限。
    pub fn index_bars_range<'a>(
        &mut self,
        market: u16,
        code: &'a str,
        category: u16,
        begin: Option<u32>,
        end: Option<u32>,
    ) -> std::io::Result<Vec<super::IndexKlineData<'a>>> {
        let ctx = format_args!(
            "Client::index_bars_range(market={market}, code={code}, category={category})"
        );
        fetch_index_bars_range(&mut self.tcp, market, code, category, begin, end)
            .map_err(|e| ctx_err(e, ctx))
    }

    /// 按日期区间拉取日K线。
    ///
    /// 自动翻页直到覆盖 `begin`，并过滤出 `[begin, end]`（格式 YYYYMMDD）的数据。
    /// `begin`/`end` 传 `None` 表示不设下/上限。
    ///
    /// [`TcpConfig::recheck_empty`](crate::tcp::TcpConfig::recheck_empty)
    /// 开启时空结果二次确认（防服务器静默限流误判为停牌真空）。
    pub fn k<'a>(
        &mut self,
        market: u16,
        code: &'a str,
        begin: Option<u32>,
        end: Option<u32>,
    ) -> std::io::Result<Vec<super::KlineData<'a>>> {
        let result = fetch_k(&mut self.tcp, market, code, begin, end);
        result.map_err(|e| ctx_err(e, format_args!("Client::k(market={market}, code={code})")))
    }

    /// 批量拉取多只股票日K（内部连接池并行，不占用当前连接）。
    ///
    /// # 参数
    ///
    /// - `stocks`: `(market, code)` 列表（market: 0=深市、1=沪市）；
    /// - `begin`/`end`: 日期区间（YYYYMMDD），`None` 不设限；
    /// - `max_parallel`: 并行连接数上限；传 `0` 按 CPU 核数自动。
    ///   受进程共享的 [`crate::tcp::NetworkPolicy`] 连接预算约束。
    ///   同一 Client 的批次排队执行，批次内并行，避免相互耗尽或关闭连接池。
    ///
    /// # 返回
    ///
    /// 按输入顺序返回；单只股票拉取失败不影响其他股票
    /// （失败信息在对应 [`BatchKline::result`] 中）。
    ///
    /// # 示例
    ///
    /// ```ignore
    /// use rustdx_complete::tcp::stock::Client;
    ///
    /// let client = Client::new()?;
    /// let rows = client.k_batch(&[(1, "600000"), (0, "000001"), (1, "600519")],
    ///                           Some(20260101), None, 4)?;
    /// for row in rows {
    ///     println!("{}: {} 根", row.code, row.result?.len());
    /// }
    /// ```
    pub fn k_batch<'a>(
        &self,
        stocks: &[(u16, &'a str)],
        begin: Option<u32>,
        end: Option<u32>,
        max_parallel: usize,
    ) -> std::io::Result<Vec<BatchKline<'a>>> {
        Ok(stocks
            .iter()
            .zip(batch::fetch(self, stocks, None, begin, end, max_parallel)?)
            .map(|(&(market, code), result)| BatchKline {
                market,
                code,
                result,
            })
            .collect())
    }

    /// 批量拉取多只股票任意周期 K 线区间（内部连接池并行，不占用当前连接）。
    ///
    /// 分钟线等短周期数据的**全市场回补**配套：每只股票走 [`Client::bars_range`]
    /// 同一套防护（自动翻页、冲突检查、来源连续性、窗口过滤、升序），多连接并行。
    ///
    /// # 参数
    ///
    /// - `stocks`: `(market, code)` 列表（market: 0=深市、1=沪市）；
    /// - `category`: K 线种类（0=5m、1=15m、…、9=日线）；
    /// - `begin`/`end`: 日期区间（YYYYMMDD，闭区间），`None` 不设限；
    /// - `max_parallel`: 并行连接数上限；传 `0` 按 CPU 核数自动。
    ///   受进程共享的 [`crate::tcp::NetworkPolicy`] 连接预算约束。
    ///   与同一 Client 的其他批次排队执行，批次内并行。
    ///
    /// 按输入顺序返回；单只失败不影响其他（失败信息在对应 [`BatchBars::result`] 中）。
    pub fn bars_batch<'a>(
        &self,
        stocks: &[(u16, &'a str)],
        category: u16,
        begin: Option<u32>,
        end: Option<u32>,
        max_parallel: usize,
    ) -> std::io::Result<Vec<BatchBars<'a>>> {
        Ok(stocks
            .iter()
            .zip(batch::fetch(
                self,
                stocks,
                Some(category),
                begin,
                end,
                max_parallel,
            )?)
            .map(|(&(market, code), result)| BatchBars {
                market,
                code,
                result,
            })
            .collect())
    }

    /// 按日期区间拉取**复权**日K线（前复权/后复权，本地计算）。
    ///
    /// 通达信服务器返回的 K 线均为不复权数据（实测请求中的复权字段实为
    /// 「取样步长」，见 `tests/adj_field_probe.rs`），因此本方法在本地用
    /// 除权除息事件（[`Client::xdxr`]）计算复权价：
    /// 除权参考价 `(preclose*10 - fh + pg*pgj) / (10 + pg + sg)`，
    /// 除权日价格按 `preclose_adj / preclose` 缩放，逐日累积。
    ///
    /// 复权只调整 OHLC 价格；`vol`/`amount` 保持原始值。
    ///
    /// # 参数
    ///
    /// - `adj`: 前复权（[`Adj::Qfq`]，最新价 = 实际价）或后复权
    ///   （[`Adj::Hfq`]，服务器可用历史的首日价 = 实际价）；
    /// - `begin`/`end`: 日期区间（YYYYMMDD），`None` 不设限。
    pub fn k_adjusted<'a>(
        &mut self,
        market: u16,
        code: &'a str,
        adj: Adj,
        begin: Option<u32>,
        end: Option<u32>,
    ) -> std::io::Result<Vec<super::KlineData<'a>>> {
        self.tcp
            .with_timeout(crate::tcp::network_policy().pagination_timeout, |tcp| {
                validate_request(market, code, 9)?;
                validate_range(begin, end)?;
                // 从服务器可用历史首日连续累积，再在本地过滤区间。
                let all = fetch_k_checked(tcp, market, code, None, None).map_err(|e| {
                    ctx_err(
                        e,
                        format_args!("Client::k_adjusted(market={market}, code={code})"),
                    )
                })?;
                if all.is_empty() {
                    return Ok(all);
                }
                let mut xdxrs = Xdxr::new(market, code);
                xdxrs.recv_parsed(tcp).map_err(|e| {
                    ctx_err(
                        e,
                        format_args!("Client::k_adjusted(xdxr, market={market}, code={code})"),
                    )
                })?;
                let multipliers = adjusted_multipliers(&all, xdxrs.result(), adj)?;

                all.into_iter()
                    .zip(multipliers)
                    .filter(|(bar, _)| {
                        let d = DateTime::to_u32(bar.dt);
                        begin.is_none_or(|b| d >= b) && end.is_none_or(|e| d <= e)
                    })
                    .map(|(mut bar, m)| {
                        bar.open *= m;
                        bar.high *= m;
                        bar.low *= m;
                        bar.close *= m;
                        if [bar.open, bar.high, bar.low, bar.close]
                            .iter()
                            .any(|p| !p.is_finite() || *p <= 0.0)
                        {
                            return Err(std::io::Error::new(
                                std::io::ErrorKind::InvalidData,
                                "复权价格溢出或非法",
                            ));
                        }
                        Ok(bar)
                    })
                    .collect()
            })
    }

    /// 当日0537分时的价格/数量兼容视图。完整字段见 [`Self::minute_series`]。
    pub fn minute(&mut self, market: u16, code: &str) -> std::io::Result<Vec<MinuteTimeData>> {
        Ok(self
            .minute_series(market, code)?
            .points
            .into_iter()
            .map(|point| MinuteTimeData {
                price: point.price,
                vol: point.volume_raw,
            })
            .collect())
    }

    /// 完整0537分时，保留均价、原始字段、合成会话时间和目录精度来源。
    /// 首次加载目录；目录与分时共用分页总体时限，之后命中当日缓存。
    pub fn minute_series(
        &mut self,
        market: u16,
        code: &str,
    ) -> std::io::Result<super::TodayMinuteSeries> {
        let request = super::TodayMinuteRequest::new(market, code)?;
        let store = &self.quote_metadata;
        self.tcp
            .with_timeout(crate::tcp::network_policy().pagination_timeout, |tcp| {
                let metadata =
                    super::quote_metadata::ensure_metadata(tcp, store, &[market], false)?;
                request.recv(tcp, &metadata)
            })
            .map_err(|e| ctx_err(e, format_args!("Client::minute_series({market}, {code})")))
    }

    /// 显式旧051d分时，保留原有今日0fb4回退行为。
    pub fn minute_legacy(
        &mut self,
        market: u16,
        code: &str,
    ) -> std::io::Result<Vec<MinuteTimeData>> {
        validate_request(market, code, 9)?;
        let ctx = format_args!("Client::minute(market={market}, code={code})");
        let mut mt = super::MinuteTime::new(market, code);
        match mt.recv_parsed(&mut self.tcp) {
            Ok(data) if !data.is_empty() => return Ok(data.to_vec()),
            Err(e)
                if !matches!(
                    e.kind(),
                    std::io::ErrorKind::InvalidData | std::io::ErrorKind::UnexpectedEof
                ) =>
            {
                return Err(ctx_err(e, ctx));
            }
            _ => {}
        }

        // 回退：当日分时接口不可用 → 今日历史分时接口（协议稳定）
        let mut hmt = HistoryMinuteTime::new(market, code, today_yyyymmdd());
        hmt.recv_parsed(&mut self.tcp)
            .map_err(|e| ctx_err(e, format_args!("{ctx}（回退到今日历史分时）")))?;
        Ok(hmt.result().to_vec())
    }

    /// 历史分时。
    pub fn history_minute(
        &mut self,
        market: u16,
        code: &str,
        date: u32,
    ) -> std::io::Result<Vec<MinuteTimeData>> {
        validate_request(market, code, 9)?;
        super::requests::checked_date(date, std::io::ErrorKind::InvalidInput)?;
        let mut mt = HistoryMinuteTime::new(market, code, date);
        mt.recv_parsed(&mut self.tcp).map_err(|e| {
            ctx_err(
                e,
                format_args!("Client::history_minute(market={market}, code={code}, date={date})"),
            )
        })?;
        Ok(mt.result().to_vec())
    }

    /// 当日事件单页，保留原始字段、分类和来源；不会自动翻页。
    pub fn trade_page(
        &mut self,
        market: u16,
        code: &str,
        start: u16,
        count: u16,
    ) -> std::io::Result<super::TradePage> {
        super::TradeQuery::today(market, code)?
            .recv_page(&mut self.tcp, start, count)
            .map_err(|e| {
                ctx_err(
                    e,
                    format_args!("Client::trade_page({market}, {code}, {start})"),
                )
            })
    }

    /// 0fc6历史事件单页，保留笔数槽、分类、参考字节和来源。
    pub fn history_trade_page(
        &mut self,
        market: u16,
        code: &str,
        date: u32,
        start: u16,
        count: u16,
    ) -> std::io::Result<super::TradePage> {
        super::TradeQuery::history(market, code, date)?
            .recv_page(&mut self.tcp, start, count)
            .map_err(|e| {
                ctx_err(
                    e,
                    format_args!("Client::history_trade_page({market}, {code}, {date}, {start})"),
                )
            })
    }

    /// 当日成交明细单页。全量枚举用 [`Self::transaction_all`]。
    pub fn transaction(
        &mut self,
        market: u16,
        code: &str,
        start: u16,
        count: u16,
    ) -> std::io::Result<Vec<TransactionData>> {
        super::TransactionRequest::new(market, code, start, count)?
            .recv(&mut self.tcp)
            .map_err(|e| {
                ctx_err(
                    e,
                    format_args!("Client::transaction(market={market}, code={code})"),
                )
            })
    }

    /// 增强历史成交明细单页（0x0fc6，保留笔数槽）。
    /// 全量枚举用 [`Self::history_transaction_all`]。
    pub fn history_transaction(
        &mut self,
        market: u16,
        code: &str,
        start: u16,
        count: u16,
        date: u32,
    ) -> std::io::Result<Vec<TransactionData>> {
        Ok(self
            .history_trade_page(market, code, date, start, count)?
            .records()
            .iter()
            .map(|record| record.to_legacy())
            .collect())
    }

    /// 显式旧0fb5单页，num=0表示旧协议缺失笔数槽。
    pub fn history_transaction_legacy(
        &mut self,
        market: u16,
        code: &str,
        start: u16,
        count: u16,
        date: u32,
    ) -> std::io::Result<Vec<TransactionData>> {
        super::HistoryTransactionRequest::new(market, code, start, count, date)?.recv(&mut self.tcp)
    }

    pub fn history_trade_page_legacy(
        &mut self,
        market: u16,
        code: &str,
        date: u32,
        start: u16,
        count: u16,
    ) -> std::io::Result<super::TradePage> {
        super::TradeQuery::history_legacy(market, code, date)?.recv_page(
            &mut self.tcp,
            start,
            count,
        )
    }

    /// 增强历史混合成交单页（0x0fc6，每页1–1800条）。
    ///
    /// 保留笔数槽、参考价和原始响应，
    /// 可通过页上的视图区分竞价快照、开盘撮合和盘后固定价成交。
    /// 日期来自请求，空页不证明全天无成交；不自动翻页或回退旧协议。
    ///
    /// ```no_run
    /// # fn main() -> std::io::Result<()> {
    /// use rustdx_complete::tcp::stock::Client;
    /// let mut client = Client::new()?;
    /// let page = client.enhanced_history_transaction(0, "000001", 0, 1800, 20261022)?;
    /// for record in page.actual_trades() {
    ///     println!("{} {} {:?}", record.time, record.price, record.side());
    /// }
    /// # Ok(())
    /// # }
    /// ```
    pub fn enhanced_history_transaction(
        &mut self,
        market: u16,
        code: &str,
        start: u16,
        count: u16,
        date: u32,
    ) -> std::io::Result<super::EnhancedHistoryTransactionPage> {
        super::EnhancedHistoryTransactionRequest::new(market, code, start, count, date)?
            .recv(&mut self.tcp)
            .map_err(|error| {
                ctx_err(
                    error,
                    format_args!(
                        "Client::enhanced_history_transaction(market={market}, code={code}, date={date}, start={start}, count={count})"
                    ),
                )
            })
    }

    /// Versioned finance semantics. Unknown/candidate fields remain explicit.
    pub fn finance(&mut self, market: u16, code: &str) -> std::io::Result<FinanceSnapshot> {
        let rows = super::FinanceRequest::new(market, code)?
            .recv(&mut self.tcp)
            .map_err(|e| {
                ctx_err(
                    e,
                    format_args!("Client::finance(market={market}, code={code})"),
                )
            })?;
        let mut snapshot = rows.into_iter().next().ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("Client::finance({market}, {code}): 无财务数据"),
            )
        })?;
        snapshot.fetched_at = Some(chrono::Utc::now());
        snapshot.source_endpoint = Some(self.tcp.get_ref().0.peer_addr()?);
        Ok(snapshot)
    }

    pub fn finance_snapshot(
        &mut self,
        market: u16,
        code: &str,
    ) -> std::io::Result<FinanceSnapshot> {
        self.finance(market, code)
    }

    /// Explicit historical pytdx compatibility, including its old units and names.
    pub fn finance_legacy(&mut self, market: u16, code: &str) -> std::io::Result<FinanceInfoData> {
        super::FinanceRequest::new(market, code)?
            .recv_legacy(&mut self.tcp)?
            .into_iter()
            .next()
            .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "无财务数据"))
    }

    /// 除权除息信息。
    pub fn xdxr(&mut self, market: u16, code: &str) -> std::io::Result<Vec<super::XdxrData>> {
        validate_request(market, code, 9)?;
        let mut xdxr = Xdxr::new(market, code);
        xdxr.recv_parsed(&mut self.tcp).map_err(|e| {
            ctx_err(
                e,
                format_args!("Client::xdxr(market={market}, code={code})"),
            )
        })?;
        Ok(xdxr.result().to_vec())
    }

    /// 板块文件，如 `"block_gn.dat"`；须通过完整MD5与成分格式校验。
    pub fn block(&mut self, block_file: &str) -> crate::Result<Vec<BlockRecord>> {
        Ok(self
            .block_file(block_file)?
            .decode_records()
            .map_err(|e| ctx_err(e, format_args!("Client::block({block_file})")))?)
    }

    /// 下载并校验完整板块文件，保留字节、元数据与每块来源。
    pub fn block_file(&mut self, block_file: &str) -> std::io::Result<super::BlockFile> {
        super::download_block_file(&mut self.tcp, block_file)
            .map_err(|e| ctx_err(e, format_args!("Client::block_file({block_file})")))
    }

    /// 读取通用06b9路径的单块；保留声明长度和未知尾部。
    pub fn file_content(
        &mut self,
        path: &str,
        offset: u32,
        size: u32,
    ) -> std::io::Result<super::FileContentChunk> {
        super::FileContentRequest::new(path, offset, size)?.recv(&mut self.tcp)
    }

    /// 有界下载通用资源；必须检查complete或调用require_complete。
    pub fn resource_file(
        &mut self,
        path: &str,
        options: super::ResourceReadOptions,
    ) -> std::io::Result<super::ResourceFile> {
        super::download_resource_file(&mut self.tcp, path, options)
            .map_err(|e| ctx_err(e, format_args!("Client::resource_file({path})")))
    }

    /// 完整下载已知统计ZIP并校验CFG；命名数值仍为候选语义。
    pub fn resource_stats(
        &mut self,
        path: &str,
        options: super::ResourceReadOptions,
    ) -> std::io::Result<super::TdxStatsResource> {
        super::read_tdx_stats(&mut self.tcp, path, options)
            .map_err(|e| ctx_err(e, format_args!("Client::resource_stats({path})")))
    }

    /// 证券数量。
    pub fn stock_count(&mut self, market: u16) -> std::io::Result<u16> {
        validate_request(market, "000001", 9)?;
        let mut sc = super::super::basic::SecurityCount::new(market);
        let c = sc
            .recv_parsed(&mut self.tcp)
            .map_err(|e| ctx_err(e, format_args!("Client::stock_count(market={market})")))?;
        Ok(*c)
    }

    pub fn security_directory(
        &mut self,
        market: u16,
    ) -> std::io::Result<Vec<super::SecurityDirectoryData>> {
        super::security_directory(&mut self.tcp, market)
    }

    pub fn security_directory_page(
        &mut self,
        market: u16,
        start: u32,
        count: u16,
    ) -> std::io::Result<super::SecurityDirectoryPage> {
        super::SecurityDirectoryRequest::new(market, start, count)?.recv(&mut self.tcp)
    }

    pub fn stocks_legacy(&mut self, market: u16) -> crate::Result<Vec<SecurityListData>> {
        super::stocks_legacy(&mut self.tcp, market)
    }

    /// 全部044d证券列表，自动分页聚合并更新目录精度缓存。
    pub fn stocks(&mut self, market: u16) -> crate::Result<Vec<SecurityListData>> {
        validate_request(market, "000001", 9)?;
        let started = chrono::Utc::now();
        let rows = super::stocks(&mut self.tcp, market).map_err(|e| match e {
            crate::Error::Io(error) => crate::Error::Io(ctx_err(
                error,
                format_args!("Client::stocks(market={market})"),
            )),
            other => crate::Error::Context {
                context: format!("Client::stocks(market={market})"),
                source: Box::new(other),
            },
        })?;
        let fetched_at = chrono::Utc::now();
        if super::quote_metadata::local_date(started)
            != super::quote_metadata::local_date(fetched_at)
        {
            return Err(crate::Error::Io(std::io::Error::other(
                "精度加载跨日，请重新预热",
            )));
        }
        self.quote_metadata.publish(
            vec![(
                market,
                rows.clone(),
                Some(self.tcp.get_ref().0.peer_addr()?),
            )],
            fetched_at,
        )?;
        Ok(rows)
    }

    /// F10 公司资料全部栏目，返回 `(栏目名, 内容)`。
    pub fn f10(&mut self, market: u16, code: &str) -> std::io::Result<Vec<(String, String)>> {
        self.f10_with_options(market, code, super::CompanyInfoReadOptions::default())
            .map_err(|e| ctx_err(e, format_args!("Client::f10(market={market}, code={code})")))
    }

    /// F10 公司资料栏目目录。
    pub fn f10_categories(
        &mut self,
        market: u16,
        code: &str,
    ) -> std::io::Result<Vec<CompanyInfoCategoryItem>> {
        self.company_info_directory(market, code)
            .map(|directory| {
                directory
                    .entries
                    .iter()
                    .map(super::CompanyInfoEntry::to_legacy)
                    .collect()
            })
            .map_err(|e| {
                ctx_err(
                    e,
                    format_args!("Client::f10_categories(market={market}, code={code})"),
                )
            })
    }

    fn batch_pool(&self, workers: usize) -> std::io::Result<ConnectionPool> {
        let config = self.tcp.config();
        let mut slot = self
            .batch_pool
            .lock()
            .map_err(|_| std::io::Error::other("批量连接池锁已损坏"))?;
        if let Some((size, saved, pool)) = slot.as_ref()
            && *size == workers
            && saved == config
        {
            return Ok(pool.clone());
        }
        if let Some((_, _, pool)) = slot.take() {
            pool.close();
        }
        let pool = ConnectionPool::with_tcp_config(
            PoolConfig {
                max_size: workers,
                // 批次完成释放全局许可，避免多个 Client 的空闲池相互饿死。
                max_idle: 0,
                ..Default::default()
            },
            config.clone(),
        )?;
        *slot = Some((workers, config.clone(), pool.clone()));
        Ok(pool)
    }
}

fn validate_request(market: u16, code: &str, category: u16) -> std::io::Result<()> {
    if market > 1
        || code.len() != 6
        || !code.bytes().all(|b| b.is_ascii_alphanumeric())
        || category > 11
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "市场、六位ASCII代码或K线周期非法",
        ));
    }
    Ok(())
}

fn validate_range(begin: Option<u32>, end: Option<u32>) -> std::io::Result<()> {
    for date in [begin, end].into_iter().flatten() {
        if chrono::NaiveDate::from_ymd_opt((date / 10000) as i32, date / 100 % 100, date % 100)
            .is_none()
        {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "日期必须是合法YYYYMMDD",
            ));
        }
    }
    if begin.zip(end).is_some_and(|(b, e)| b > e) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "开始日期晚于结束日期",
        ));
    }
    Ok(())
}

fn fetch_k_checked<'a>(
    tcp: &mut Tcp,
    market: u16,
    code: &'a str,
    begin: Option<u32>,
    end: Option<u32>,
) -> std::io::Result<Vec<KlineData<'a>>> {
    fetch_k(tcp, market, code, begin, end)
}

/// 今日日期，格式 YYYYMMDD（用于当日分时回退到今日历史分时）。
fn today_yyyymmdd() -> u32 {
    let today = chrono::Utc::now()
        .with_timezone(&chrono::FixedOffset::east_opt(8 * 3600).unwrap())
        .date_naive();
    (today.year() * 10000 + today.month() as i32 * 100 + today.day() as i32) as u32
}

/// 给 IO 错误附加接口上下文（保留错误 kind，便于调用方按 kind 分支处理）。
fn ctx_err(e: std::io::Error, ctx: std::fmt::Arguments<'_>) -> std::io::Error {
    std::io::Error::new(e.kind(), format!("{ctx}: {e}"))
}

/// 从给定连接按日期区间拉取任意周期 K 线：自动翻页、去重、窗口过滤、升序。
///
/// 与 `Client::bars_range` 共用逻辑（`Client::k` 的日K专用版见 [`fetch_k`]）。
fn fetch_bars_range<'a>(
    tcp: &mut Tcp,
    market: u16,
    code: &'a str,
    category: u16,
    begin: Option<u32>,
    end: Option<u32>,
) -> std::io::Result<Vec<KlineData<'a>>> {
    bar_range::legacy_records(bar_range::stock(
        tcp,
        market,
        code,
        category,
        begin,
        end,
        BarRangeOptions {
            stop_on_short_page: true,
            ..Default::default()
        },
    )?)
}

/// 从给定连接按日期区间拉取指数K线：自动翻页、去重、窗口过滤、升序。
///
/// 与 [`fetch_bars_range`] 对称，使用指数响应解析器；
/// 翻页判停、冲突检查、来源连续性和窗口过滤采用同一套约定。
fn fetch_index_bars_range<'a>(
    tcp: &mut Tcp,
    market: u16,
    code: &'a str,
    category: u16,
    begin: Option<u32>,
    end: Option<u32>,
) -> std::io::Result<Vec<IndexKlineData<'a>>> {
    bar_range::legacy_records(bar_range::index(
        tcp,
        market,
        code,
        category,
        begin,
        end,
        BarRangeOptions {
            stop_on_short_page: true,
            ..Default::default()
        },
    )?)
}

/// 除权参考价（`category == 1` 除权除息事件）：
/// `(前收盘 × 10 − 每10股派现 + 配股价 × 每10股配股) ÷ (10 + 每10股送转 + 每10股配股)`，
/// **四舍五入到分**（与交易所/行情软件的除权日昨收一致）。
///
/// 输入或计算结果非法时原样返回 `preclose`（兼容接口）；
/// [`Client::k_adjusted`] 会将这类错误返回给调用方。
/// 除权日 `prev_close` 应传本函数结果，随后即可做涨跌停判定
/// （见 [`crate::limit`]）与日线 `prev_close` 链构建。
pub fn ex_right_reference(preclose: f64, ev: &XdxrData) -> f64 {
    checked_ex_right_reference(preclose, ev).unwrap_or(preclose)
}

/// 从给定连接拉取单只股票日K：自动翻页、日期区间过滤、按日期升序排序。
///
/// 与 `Client::k` 共用同一套逻辑，供 `k_batch` 在连接池连接上复用。
fn fetch_k<'a>(
    tcp: &mut Tcp,
    market: u16,
    code: &'a str,
    begin: Option<u32>,
    end: Option<u32>,
) -> std::io::Result<Vec<KlineData<'a>>> {
    fetch_bars_range(tcp, market, code, 9, begin, end)
}

fn validate_bar_dates(dates: impl Iterator<Item = DateTime>) -> std::io::Result<()> {
    let today = chrono::Utc::now()
        .with_timezone(&chrono::FixedOffset::east_opt(8 * 3600).unwrap())
        .date_naive();
    for dt in dates {
        let date = chrono::NaiveDate::from_ymd_opt(dt.year as i32, dt.month as u32, dt.day as u32);
        if date.is_none_or(|date| date.year() < 1990 || date > today)
            || dt.hour > 23
            || dt.minute > 59
        {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("K线日期非法: {dt:?}"),
            ));
        }
    }
    Ok(())
}

/// 批量 K 线单项结果。
///
/// `result` 为该股票独立的结果：`Ok` 含按日期升序的日 K 数据，
/// `Err` 为该股票拉取失败（不影响其他股票）。
#[derive(Debug)]
pub struct BatchKline<'a> {
    /// 市场代码（0=深市、1=沪市）
    pub market: u16,
    /// 6 位股票代码
    pub code: &'a str,
    /// 该股票拉取结果
    pub result: std::io::Result<Vec<KlineData<'a>>>,
}

/// [`Client::bars_batch`] 单项结果（结构同 [`BatchKline`]，任意周期）。
#[derive(Debug)]
pub struct BatchBars<'a> {
    /// 市场代码（0=深市、1=沪市）
    pub market: u16,
    /// 6 位股票代码
    pub code: &'a str,
    /// 该股票拉取结果（按时间升序）
    pub result: std::io::Result<Vec<KlineData<'a>>>,
}

/// 复权方式。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Adj {
    /// 前复权：以最新价为基准，历史价按比例缩放（最新价 = 实际价）。
    Qfq,
    /// 后复权：以服务器可用历史首日为基准（首日价 = 实际价）。
    Hfq,
}

#[cfg(test)]
mod tests {

    #[test]
    fn overlapping_batches_wait_and_preserve_results_when_pool_size_changes() {
        use super::Client;
        use std::sync::{
            Mutex,
            atomic::{AtomicBool, Ordering},
            mpsc,
        };
        use std::time::Duration;

        for second_workers in [1, 2] {
            let (started_tx, started_rx) = mpsc::channel();
            let (release_tx, release_rx) = mpsc::channel();
            let release_rx = Mutex::new(release_rx);
            let first = AtomicBool::new(true);
            let (config, server) = crate::tcp::tests::mock_server(
                if second_workers == 1 { 3 } else { 4 },
                move |request| {
                    if request.get(14..20) == Some(b"600000".as_slice())
                        && first.swap(false, Ordering::SeqCst)
                    {
                        started_tx.send(()).unwrap();
                        release_rx.lock().unwrap().recv().unwrap();
                    }
                    vec![0, 0]
                },
            );
            let client = Client::with_config(&config).unwrap();
            std::thread::scope(|scope| {
                let first = scope.spawn(|| client.k_batch(&[(1, "600000")], None, None, 1));
                started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
                let (done_tx, done_rx) = mpsc::channel();
                let client = &client;
                let second = scope.spawn(move || {
                    let result = client.bars_batch(
                        &[(0, "000001"), (1, "600519")],
                        9,
                        None,
                        None,
                        second_workers,
                    );
                    done_tx.send(()).unwrap();
                    result
                });
                let pending = done_rx.recv_timeout(Duration::from_millis(100));
                release_tx.send(()).unwrap();
                assert!(first.join().unwrap().unwrap()[0].result.is_ok());
                assert!(
                    second
                        .join()
                        .unwrap()
                        .unwrap()
                        .iter()
                        .all(|row| row.result.is_ok())
                );
                assert!(matches!(pending, Err(mpsc::RecvTimeoutError::Timeout)));
            });
            drop(client);
            server.join().unwrap();
        }
    }

    #[test]
    fn k_range_retain_logic() {
        // 直接测试日期过滤逻辑所依赖的 to_u32 排序
        use crate::tcp::helper::DateTime;
        let d = DateTime {
            year: 2026,
            month: 9,
            day: 1,
            hour: 15,
            minute: 0,
        };
        assert_eq!(d.to_u32(), 20260901);
    }

    #[test]
    fn adjusted_multipliers_no_xdxr() {
        use super::Adj;
        use super::adjusted_multipliers;
        // 无除权：scale 恒 1 → 前/后复权乘数均为 1
        let days = vec![kd(20260101, 10.0), kd(20260102, 11.0), kd(20260105, 12.0)];
        assert_eq!(
            adjusted_multipliers(&days, &[], Adj::Qfq).unwrap(),
            vec![1.0, 1.0, 1.0]
        );
        assert_eq!(
            adjusted_multipliers(&days, &[], Adj::Hfq).unwrap(),
            vec![1.0, 1.0, 1.0]
        );
    }

    #[test]
    fn adjusted_multipliers_split() {
        use super::Adj;
        use super::adjusted_multipliers;
        // 10 送 10：除权参考价 = 前收/2 → scale 减半
        let days = vec![
            kd(20260105, 20.0),
            kd(20260106, 10.0), // 除权日，前收 20 → 参考价 10（10送10）
            kd(20260107, 10.5),
        ];
        let xdxrs = vec![xdxr(20260106, 0.0, 10.0, 0.0, 0.0)]; // 每 10 股送 10
        // 后复权：除权后价格放大回除权前量纲
        let m = adjusted_multipliers(&days, &xdxrs, Adj::Hfq).unwrap();
        assert!((m[0] - 1.0).abs() < 1e-9);
        assert!((m[1] - 2.0).abs() < 1e-9, "除权日乘数应为 2，实际 {m:?}");
        assert!((m[2] - 2.0).abs() < 1e-9);
        // 前复权：最新价不变，历史价按比例缩小
        let m = adjusted_multipliers(&days, &xdxrs, Adj::Qfq).unwrap();
        assert!((m[2] - 1.0).abs() < 1e-9);
        assert!((m[0] - 0.5).abs() < 1e-9, "除权前乘数应为 0.5，实际 {m:?}");
    }

    #[test]
    fn adjusted_multipliers_cash_and_rights() {
        use super::Adj;
        use super::adjusted_multipliers;
        // 每 10 股派 5 元 + 每 10 股配 3 股（配股价 2 元）：
        // 参考价 = (20*10 - 5 + 3*2) / (10 + 3) = 201/13 ≈ 15.4615 → 四舍五入 15.46
        let days = vec![kd(20260105, 20.0), kd(20260106, 15.0), kd(20260107, 15.5)];
        let xdxrs = vec![xdxr(20260106, 5.0, 0.0, 3.0, 2.0)];
        let m = adjusted_multipliers(&days, &xdxrs, Adj::Hfq).unwrap();
        assert!((m[1] - (20.0 / 15.46)).abs() < 1e-9, "实际 {m:?}");
    }

    /// 除权参考价与交易所口径一致（四舍五入到分）。
    #[test]
    fn ex_right_reference_rounds_to_fen() {
        use super::ex_right_reference;
        // (20*10 - 5 + 3*2) / 13 = 201/13 = 15.4615… → 15.46
        let ev = xdxr(20260106, 5.0, 0.0, 3.0, 2.0);
        assert_eq!(ex_right_reference(20.0, &ev), 15.46);
        // 无变动 → 原样返回
        let none = xdxr(20260106, 0.0, 0.0, 0.0, 0.0);
        assert_eq!(ex_right_reference(20.0, &none), 20.0);
    }

    #[test]
    fn adjusted_multipliers_suspended_xdxr_day() {
        use super::Adj;
        use super::adjusted_multipliers;
        // 除权日为停牌日（无 K 线）：事件顺延到下一交易日处理，
        // 仍用除权前最后一个交易日收盘作为 preclose
        let days = vec![kd(20260102, 20.0), kd(20260106, 10.0)];
        let xdxrs = vec![xdxr(20260105, 0.0, 10.0, 0.0, 0.0)]; // 01-05 除权，01-06 才恢复交易
        let m = adjusted_multipliers(&days, &xdxrs, Adj::Hfq).unwrap();
        assert!((m[0] - 1.0).abs() < 1e-9);
        assert!(
            (m[1] - 2.0).abs() < 1e-9,
            "停牌顺延后乘数应为 2，实际 {m:?}"
        );
    }

    #[test]
    fn adjustment_anchors_first_bar_and_chains_suspension_events() {
        use super::{Adj, adjusted_multipliers};
        let days = [kd(20260102, 10.0), kd(20260106, 8.0)];
        let events = [
            xdxr(20260101, 0.0, 10.0, 0.0, 0.0),
            xdxr(20260102, 0.0, 10.0, 0.0, 0.0),
            xdxr(20260103, 10.0, 0.0, 0.0, 0.0),
            xdxr(20260105, 10.0, 0.0, 0.0, 0.0),
        ];
        assert_eq!(
            adjusted_multipliers(&days, &events, Adj::Hfq).unwrap(),
            [1.0, 1.25]
        );
        let qfq = adjusted_multipliers(&days, &events, Adj::Qfq).unwrap();
        assert!((qfq[0] - 0.8).abs() < 1e-12);
        assert_eq!(qfq[1], 1.0);
        for cash in [f32::NAN, -1.0, 1000.0] {
            assert!(
                adjusted_multipliers(&days, &[xdxr(20260105, cash, 0.0, 0.0, 0.0)], Adj::Qfq)
                    .is_err()
            );
        }
        assert!(adjusted_multipliers(&[kd(20260102, f64::NAN)], &[], Adj::Hfq).is_err());
    }

    fn kd(date: u32, close: f64) -> super::KlineData<'static> {
        let (y, m, d) = (date / 10000, date / 100 % 100, date % 100);
        super::KlineData {
            dt: crate::tcp::helper::DateTime {
                year: y as u16,
                month: m as u16,
                day: d as u16,
                hour: 15,
                minute: 0,
            },
            code: "",
            open: close,
            close,
            high: close,
            low: close,
            vol: 0.0,
            amount: 0.0,
        }
    }

    fn xdxr(date: u32, fh: f32, sg: f32, pg: f32, pgj: f32) -> super::XdxrData {
        super::XdxrData {
            market: 1,
            code: "600000".into(),
            date,
            category: 1,
            fh_qltp: fh,
            pgj_qzgb: pgj,
            sg_hltp: sg,
            pg_hzgb: pg,
        }
    }

    /// 回退日期使用上海时区，不受部署机器本地时区影响。
    #[test]
    fn today_yyyymmdd_format() {
        use super::today_yyyymmdd;
        let v = today_yyyymmdd();
        assert!((20000101..=20991231).contains(&v), "非法日期: {v}");
        let today = chrono::Utc::now()
            .with_timezone(&chrono::FixedOffset::east_opt(8 * 3600).unwrap())
            .date_naive();
        let expected = {
            use chrono::Datelike;
            (today.year() * 10000 + today.month() as i32 * 100 + today.day() as i32) as u32
        };
        assert_eq!(v, expected);
    }
}
