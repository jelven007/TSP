//! Bounded synchronous date-window retrieval, with no estimate fallback.
use super::Client;
use crate::tcp::stock::{
    KlineRequest, ReportedTurnover, TurnoverOptions, TurnoverOutcome, TurnoverQuery, TurnoverScope,
    TurnoverSource, decode_reported_turnover,
};
use std::io::{self, ErrorKind};

impl Client {
    /// Fetch vendor K-line turnover for one explicit date and scope.
    ///
    /// Units/session evidence must be supplied in `options.profile`. Without
    /// it the raw bars are retained but the business total is unavailable.
    /// Reconnects, byte/record/page limits and deadlines return errors.
    pub fn reported_turnover(
        &mut self,
        market: u16,
        code: &str,
        date: u32,
        scope: TurnoverScope,
        options: TurnoverOptions,
    ) -> io::Result<TurnoverOutcome> {
        let category = match scope {
            TurnoverScope::Minute1 => 8,
            TurnoverScope::Minute5 => 0,
            _ => options.daily_category,
        };
        let query = TurnoverQuery::new(market, code, date, scope, category)?;
        KlineRequest::new(market, code, category, 0, options.page_size)?;
        if options.max_pages == 0
            || options.max_records == 0
            || options.max_bytes == 0
            || options.timeout.is_zero()
        {
            return Err(io::Error::new(
                ErrorKind::InvalidInput,
                "源成交额查询预算必须大于0",
            ));
        }
        if scope == TurnoverScope::AfterHours {
            return ReportedTurnover::from_pages(query, vec![], options.profile.as_ref());
        }
        self.tcp.with_timeout(
            options
                .timeout
                .min(crate::tcp::network_policy().pagination_timeout),
            |tcp| {
                let connection = tcp.connection_id();
                let endpoint = tcp.get_ref().0.peer_addr()?;
                let (mut offset, mut bytes, mut records) = (0u32, 0usize, 0usize);
                let mut pages = Vec::new();
                let mut oldest = None;
                loop {
                    crate::operation::check()?;
                    if pages.len() >= options.max_pages || records >= options.max_records {
                        return Err(io::Error::other("源成交额查询页数或条数预算耗尽"));
                    }
                    let start = u16::try_from(offset)
                        .map_err(|_| io::Error::other("源成交额查询偏移耗尽"))?;
                    let count =
                        usize::from(options.page_size).min(options.max_records - records) as u16;
                    if !pages.is_empty() {
                        tcp.delay(options.page_delay)?;
                    }
                    if tcp.connection_id() != connection || tcp.get_ref().0.peer_addr()? != endpoint
                    {
                        return Err(io::Error::new(
                            ErrorKind::ConnectionAborted,
                            "源成交额查询连接变化",
                        ));
                    }
                    let request = KlineRequest::new(market, code, category, start, count)?;
                    let payload =
                        crate::tcp::send_recv_decompress(tcp, &request.encode(), "源成交额K线")?;
                    if tcp.connection_id() != connection || tcp.get_ref().0.peer_addr()? != endpoint
                    {
                        return Err(io::Error::new(
                            ErrorKind::ConnectionAborted,
                            "源成交额查询重连；结果未拼接",
                        ));
                    }
                    bytes = bytes
                        .checked_add(payload.len())
                        .filter(|n| *n <= options.max_bytes)
                        .ok_or_else(|| io::Error::other("源成交额查询字节预算耗尽"))?;
                    let page = decode_reported_turnover(
                        &request,
                        &payload,
                        Some(TurnoverSource::capture(tcp)?),
                    )?;
                    let timestamps: Vec<_> = page
                        .bars()
                        .iter()
                        .map(|b| (b.date(), b.time_minutes()))
                        .collect();
                    if timestamps.windows(2).any(|p| p[0] >= p[1])
                        || oldest.is_some_and(|previous| {
                            timestamps.last().is_some_and(|stamp| *stamp >= previous)
                        })
                    {
                        return Err(io::Error::new(
                            ErrorKind::InvalidData,
                            "源成交额分页时间重复或倒序",
                        ));
                    }
                    oldest = timestamps.first().copied().or(oldest);
                    records += page.bars().len();
                    offset += page.bars().len() as u32;
                    let reached_older = page.bars().iter().any(|b| b.date() < date);
                    let reached_end = page.bars().is_empty();
                    let target_records = pages
                        .iter()
                        .map(|p: &crate::tcp::stock::ReportedTurnoverSeries| {
                            p.bars().iter().filter(|b| b.date() == date).count()
                        })
                        .sum::<usize>()
                        + page.bars().iter().filter(|b| b.date() == date).count();
                    let expected = match category {
                        8 => 240,
                        0 => 48,
                        _ => 1,
                    };
                    pages.push(page);
                    if reached_end || reached_older || target_records >= expected {
                        return ReportedTurnover::from_pages(
                            query,
                            pages,
                            options.profile.as_ref(),
                        );
                    }
                }
            },
        )
    }
}
