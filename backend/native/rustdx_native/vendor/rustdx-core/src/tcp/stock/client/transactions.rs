//! Strict enumeration of 0fc5/0fc6 and explicit legacy 0fb5 events.
use std::{
    collections::{HashMap, hash_map::DefaultHasher},
    hash::{Hash, Hasher},
    io::{self, ErrorKind},
    net::SocketAddr,
};

use super::{Client, today_yyyymmdd};
use crate::tcp::{
    Tcp,
    stock::{
        TradePage, TradeProtocol, TradeQuery, TradeSource, TransactionCollection, TransactionData,
        TransactionPageInfo, TransactionPaginationError, TransactionPaginationFailure,
        TransactionPaginationOptions, TransactionPaginationTermination,
    },
};

impl Client {
    /// Enumerate raw current-day events (0x0fc5) from start=0 until an empty page.
    ///
    /// Short nonempty pages continue using their raw length. Earlier pages are
    /// placed first, preserving wire order and duplicate same-minute records.
    /// Limits, reconnects, date changes, ambiguous repeated pages and invalid
    /// order return errors, never successful partial data.
    ///
    /// `recheck_empty` confirms each empty page on the **same connection**, with
    /// a delay of at least 300ms. Intraday offsets can still move; successful
    /// enumeration does not verify a frozen snapshot or the source's day.
    ///
    /// ```no_run
    /// # fn main() -> std::io::Result<()> {
    /// use rustdx_complete::tcp::stock::{Client, TransactionPaginationOptions};
    /// let mut client = Client::new()?;
    /// let all = client.transaction_all(0, "000001", TransactionPaginationOptions::default())?;
    /// println!("{} raw events, {} pages", all.records.len(), all.pages.len());
    /// # Ok(()) }
    /// ```
    pub fn transaction_all(
        &mut self,
        market: u16,
        code: &str,
        options: TransactionPaginationOptions,
    ) -> io::Result<TransactionCollection> {
        collect(
            &mut self.tcp,
            Query {
                market,
                code,
                date: None,
                legacy: false,
            },
            options,
        )
    }

    /// Enumerate 0x0fc6 history for an explicit YYYYMMDD, preserving all events.
    ///
    /// Uses the same strict limits and ordering as [`Self::transaction_all`].
    /// Count/status/tail fields and raw pages remain in `event_pages`. No event
    /// filtering occurs. An empty result does not prove a no-trade day.
    ///
    /// ```no_run
    /// # fn main() -> std::io::Result<()> {
    /// use rustdx_complete::tcp::stock::{Client, TransactionPaginationOptions};
    /// let mut client = Client::new()?;
    /// let all = client.history_transaction_all(
    ///     0, "000001", 20261022, TransactionPaginationOptions::default(),
    /// )?;
    /// assert_eq!(all.requested_date, Some(20261022));
    /// # Ok(()) }
    /// ```
    pub fn history_transaction_all(
        &mut self,
        market: u16,
        code: &str,
        date: u32,
        options: TransactionPaginationOptions,
    ) -> io::Result<TransactionCollection> {
        collect(
            &mut self.tcp,
            Query {
                market,
                code,
                date: Some(date),
                legacy: false,
            },
            options,
        )
    }

    pub fn history_transaction_all_legacy(
        &mut self,
        market: u16,
        code: &str,
        date: u32,
        options: TransactionPaginationOptions,
    ) -> io::Result<TransactionCollection> {
        collect(
            &mut self.tcp,
            Query {
                market,
                code,
                date: Some(date),
                legacy: true,
            },
            options,
        )
    }

    /// Full 0fc6 enumeration; rich records and reference bytes are in `event_pages`.
    pub fn enhanced_history_transaction_all(
        &mut self,
        market: u16,
        code: &str,
        date: u32,
        options: TransactionPaginationOptions,
    ) -> io::Result<TransactionCollection> {
        self.history_transaction_all(market, code, date, options)
    }
}

#[derive(Clone, Copy)]
struct Query<'a> {
    market: u16,
    code: &'a str,
    date: Option<u32>,
    legacy: bool,
}

impl Query<'_> {
    fn trade_query(self) -> io::Result<TradeQuery> {
        match self.date {
            Some(date) if self.legacy => TradeQuery::history_legacy(self.market, self.code, date),
            Some(date) => TradeQuery::history(self.market, self.code, date),
            None => TradeQuery::today(self.market, self.code),
        }
    }

    fn request(self, start: u16, count: u16) -> io::Result<Vec<u8>> {
        self.trade_query()?.encode(start, count)
    }
}

#[derive(Default)]
struct Pages {
    pages: Vec<(TransactionPageInfo, Vec<TransactionData>)>,
    event_pages: Vec<TradePage>,
    pending_page: Option<TradePage>,
    fingerprints: HashMap<u64, Vec<usize>>,
    records: usize,
    bytes: usize,
    requests: usize,
    session: Option<(u64, SocketAddr)>,
    observed_date: u32,
}

impl Pages {
    fn error(&self, reason: TransactionPaginationFailure, start: u32) -> io::Error {
        use TransactionPaginationFailure::*;
        let kind = match reason {
            ConnectionChanged | DateChanged => ErrorKind::ConnectionAborted,
            RepeatedPage | InvalidOrder => ErrorKind::InvalidData,
            PageLimit | RecordLimit | ByteLimit | OffsetLimit => ErrorKind::Other,
        };
        io::Error::new(
            kind,
            TransactionPaginationError {
                reason,
                start,
                pages: self.pages.len(),
                records: self.records,
            },
        )
    }

    fn check_context(&self, tcp: &Tcp, query: Query<'_>, start: u16) -> io::Result<()> {
        crate::operation::check()?;
        let session = (tcp.connection_generation(), tcp.get_ref().0.peer_addr()?);
        if self.session.is_some_and(|expected| expected != session) {
            return Err(self.error(
                TransactionPaginationFailure::ConnectionChanged,
                start.into(),
            ));
        }
        if query.date.is_none() && today_yyyymmdd() != self.observed_date {
            return Err(self.error(TransactionPaginationFailure::DateChanged, start.into()));
        }
        Ok(())
    }

    fn read(
        &mut self,
        tcp: &mut Tcp,
        query: Query<'_>,
        start: u16,
        count: u16,
        max_bytes: usize,
    ) -> io::Result<Vec<TransactionData>> {
        self.check_context(tcp, query, start)?;
        self.requests += 1;
        let bytes =
            crate::tcp::send_recv_decompress(tcp, &query.request(start, count)?, "全量成交明细")
                .map_err(|e| {
                    super::ctx_err(
                        e,
                        format_args!(
                            "成交明细全量(market={}, code={}, date={:?}, start={start})",
                            query.market, query.code, query.date,
                        ),
                    )
                })?;
        self.check_context(tcp, query, start)?;
        self.session
            .get_or_insert((tcp.connection_generation(), tcp.get_ref().0.peer_addr()?));
        self.bytes = self
            .bytes
            .checked_add(bytes.len())
            .filter(|total| *total <= max_bytes)
            .ok_or_else(|| self.error(TransactionPaginationFailure::ByteLimit, start.into()))?;
        let mut page =
            crate::tcp::stock::decode_trade_page(&query.trade_query()?, start, count, &bytes)
                .map_err(|e| {
                    super::ctx_err(
                        e,
                        format_args!("成交明细全量解码(code={}, start={start})", query.code,),
                    )
                })?;
        page.source = Some(TradeSource::capture(tcp)?);
        let rows = page.records().iter().map(|row| row.to_legacy()).collect();
        self.pending_page = Some(page);
        Ok(rows)
    }

    fn push(&mut self, info: TransactionPageInfo, rows: Vec<TransactionData>) -> io::Result<()> {
        if rows.windows(2).any(|pair| pair[0].time > pair[1].time)
            || self.pages.last().is_some_and(|(_, newer)| {
                rows.last()
                    .zip(newer.first())
                    .is_some_and(|(old, new)| old.time > new.time)
            })
        {
            return Err(self.error(
                TransactionPaginationFailure::InvalidOrder,
                info.start.into(),
            ));
        }
        if !rows.is_empty() {
            let mut hash = DefaultHasher::new();
            let current = self
                .pending_page
                .as_ref()
                .ok_or_else(|| io::Error::other("缺少成交事件页"))?;
            current.payload().hash(&mut hash);
            let indexes = self.fingerprints.entry(hash.finish()).or_default();
            if indexes
                .iter()
                .any(|&index| self.event_pages[index].payload() == current.payload())
            {
                return Err(self.error(
                    TransactionPaginationFailure::RepeatedPage,
                    info.start.into(),
                ));
            }
            indexes.push(self.pages.len());
        }
        self.records += rows.len();
        self.event_pages.push(
            self.pending_page
                .take()
                .ok_or_else(|| io::Error::other("缺少成交事件页"))?,
        );
        self.pages.push((info, rows));
        Ok(())
    }
}

fn collect(
    tcp: &mut Tcp,
    query: Query<'_>,
    options: TransactionPaginationOptions,
) -> io::Result<TransactionCollection> {
    if options.max_pages == 0
        || options.max_records == 0
        || options.max_bytes == 0
        || options.timeout.is_zero()
    {
        return Err(io::Error::new(
            ErrorKind::InvalidInput,
            "成交明细分页预算必须大于0",
        ));
    }
    // Validate symbol, page size and history date before any request.
    query.request(0, options.page_size)?;
    tcp.with_timeout(
        options
            .timeout
            .min(crate::tcp::network_policy().pagination_timeout),
        |tcp| {
            let started_at = chrono::Utc::now();
            let observed_date = today_yyyymmdd();
            let mut state = Pages {
                observed_date,
                ..Pages::default()
            };
            let mut start = 0u32;
            loop {
                let offset = u16::try_from(start)
                    .map_err(|_| state.error(TransactionPaginationFailure::OffsetLimit, start))?;
                if state.pages.len() == options.max_pages {
                    return Err(state.error(TransactionPaginationFailure::PageLimit, start));
                }
                if state.requests > 0 {
                    tcp.delay(options.page_delay)?;
                }
                // An empty probe is still allowed at an exact record budget.
                let remaining = options.max_records - state.records;
                let count = usize::from(options.page_size).min(remaining.max(1)) as u16;
                let before_bytes = state.bytes;
                let before_requests = state.requests;
                let mut rows = state.read(tcp, query, offset, count, options.max_bytes)?;
                if rows.is_empty() && tcp.config().recheck_empty {
                    tcp.delay(
                        options
                            .page_delay
                            .max(std::time::Duration::from_millis(300)),
                    )?;
                    rows = state.read(tcp, query, offset, count, options.max_bytes)?;
                }
                if rows.len() > remaining {
                    return Err(state.error(TransactionPaginationFailure::RecordLimit, start));
                }
                let raw_count = rows.len();
                state.push(
                    TransactionPageInfo {
                        start: offset,
                        request_count: count,
                        raw_count,
                        record_range: 0..0,
                        payload_bytes: state.bytes - before_bytes,
                        requests: state.requests - before_requests,
                    },
                    rows,
                )?;
                if raw_count == 0 {
                    let (connection_generation, source_endpoint) = state
                        .session
                        .ok_or_else(|| io::Error::other("成交明细枚举缺少连接来源"))?;
                    let mut records = Vec::with_capacity(state.records);
                    for (info, rows) in state.pages.iter_mut().rev() {
                        info.record_range = records.len()..records.len() + rows.len();
                        records.append(rows);
                    }
                    return Ok(TransactionCollection {
                        market: query.market,
                        code: query.code.to_owned(),
                        command: match query.trade_query()?.protocol() {
                            TradeProtocol::Today0fc5 => 0x0fc5,
                            TradeProtocol::EnhancedHistory0fc6 => 0x0fc6,
                            TradeProtocol::LegacyHistory0fb5 => 0x0fb5,
                        },
                        requested_date: query.date,
                        observed_date,
                        source_endpoint,
                        connection_generation,
                        started_at,
                        fetched_at: chrono::Utc::now(),
                        records,
                        pages: state.pages.into_iter().map(|(info, _)| info).collect(),
                        event_pages: state.event_pages,
                        requests: state.requests,
                        payload_bytes: state.bytes,
                        termination: TransactionPaginationTermination::EmptyPageObserved,
                    });
                }
                // Raw count, never filtered count or the requested page size.
                start += raw_count as u32;
            }
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn current_day_date_change_is_rejected_but_history_keeps_its_explicit_date() {
        let (config, server) = crate::tcp::tests::mock_server(1, |_| vec![0, 0]);
        let tcp = Tcp::with_config(&config).unwrap();
        let pages = Pages {
            observed_date: 20000101,
            ..Pages::default()
        };
        let query = Query {
            market: 0,
            code: "000001",
            date: None,
            legacy: false,
        };
        let error = pages.check_context(&tcp, query, 3).unwrap_err();
        let failure = error
            .get_ref()
            .unwrap()
            .downcast_ref::<TransactionPaginationError>()
            .unwrap();
        assert_eq!(failure.reason, TransactionPaginationFailure::DateChanged);
        assert!(
            pages
                .check_context(
                    &tcp,
                    Query {
                        date: Some(20261022),
                        ..query
                    },
                    3
                )
                .is_ok()
        );
        drop(tcp);
        server.join().unwrap();
    }
}
