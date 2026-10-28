//! Range provenance and bounded, source-continuous stock/index pagination.
use chrono::Datelike;
use std::{collections::BTreeMap, io, net::SocketAddr, time::Duration};

use super::{Client, KlineRequest, today_yyyymmdd, validate_bar_dates, validate_range};
use crate::tcp::{
    Tcp,
    helper::DateTime,
    stock::{
        DailyBar, DailyBarsRequest, IndexKlineData, KlineData, decode_daily_bars,
        decode_index_bars, decode_stock_bars,
    },
};

#[derive(Clone, Copy)]
struct RangeRequest<'a> {
    parameters: KlineRequest<'a>,
    command: u16,
}
impl<'a> From<KlineRequest<'a>> for RangeRequest<'a> {
    fn from(parameters: KlineRequest<'a>) -> Self {
        Self {
            parameters,
            command: 0x052d,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct BarRangeOptions {
    pub page_size: u16,
    pub max_pages: usize,
    /// Raw rows, including duplicate boundaries and rows outside the window.
    pub max_rows: usize,
    /// Decompressed response bytes, including empty confirmations.
    pub max_bytes: usize,
    pub timeout: Duration,
    /// A short page is an unverified stopping point, never a complete range.
    pub stop_on_short_page: bool,
}

impl Default for BarRangeOptions {
    fn default() -> Self {
        Self {
            page_size: 800,
            max_pages: 1024,
            max_rows: 65_536,
            max_bytes: 64 * 1024 * 1024,
            timeout: Duration::from_secs(120),
            stop_on_short_page: false,
        }
    }
}

impl BarRangeOptions {
    pub(crate) fn validate(self) -> io::Result<()> {
        if !(1..=800).contains(&self.page_size)
            || self.max_pages == 0
            || self.max_rows == 0
            || self.max_bytes == 0
            || self.timeout.is_zero()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "K线区间预算非法",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub enum BarRangeTermination {
    LowerBoundReached,
    EmptyPageObserved,
    ShortPage,
    PageLimit,
    RowLimit,
    ByteLimit,
    OffsetLimit,
}

/// These are source enumeration facts, not proof of exchange history,
/// retention, suspension, closed bars or a frozen intraday snapshot.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub enum BarRangeCoverage {
    Unverified,
    SourceLowerBoundReached,
    SourceEnumerated,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub struct BarRangeSource {
    pub endpoint: SocketAddr,
    pub connection_id: u64,
    pub connection_generation: u64,
    pub observed_date: u32,
}

impl BarRangeSource {
    fn capture(tcp: &Tcp) -> io::Result<Self> {
        Ok(Self {
            endpoint: tcp.get_ref().0.peer_addr()?,
            connection_id: tcp.connection_id().get(),
            connection_generation: tcp.connection_generation(),
            observed_date: today_yyyymmdd(),
        })
    }

    fn check(self, tcp: &Tcp) -> io::Result<()> {
        crate::operation::check()?;
        if self != Self::capture(tcp)? {
            return Err(io::Error::new(
                io::ErrorKind::ConnectionAborted,
                "K线区间连接、节点或观察日期变化，须从头重采",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct BarRangePage {
    pub start: u16,
    pub requested_count: u16,
    pub raw_count: usize,
    pub payload_bytes: usize,
    pub requests: usize,
    pub oldest: Option<DateTime>,
    pub newest: Option<DateTime>,
    pub duplicate_rows: usize,
    /// False for a received page rejected by the byte budget.
    pub accepted: bool,
}

#[derive(Debug, serde::Serialize)]
pub struct BarRangeCollection<T> {
    pub market: u16,
    pub code: String,
    pub command: u16,
    pub category: u16,
    pub requested_begin: Option<u32>,
    pub requested_end: Option<u32>,
    /// Actual bounds of the filtered records, not inferred coverage.
    pub actual_begin: Option<DateTime>,
    pub actual_end: Option<DateTime>,
    pub source: BarRangeSource,
    pub started_at: chrono::DateTime<chrono::Utc>,
    pub fetched_at: chrono::DateTime<chrono::Utc>,
    pub pages: Vec<BarRangePage>,
    pub raw_rows: usize,
    pub payload_bytes: usize,
    pub records: Vec<T>,
    pub complete: bool,
    pub termination: BarRangeTermination,
    pub coverage: BarRangeCoverage,
}

impl Client {
    /// Unadjusted 052b daily bars with exact u32 share counts. Endpoint coverage
    /// still needs independent evidence; completion only describes enumeration.
    pub fn daily_bars_range_with_options(
        &mut self,
        market: u16,
        code: &str,
        begin: Option<u32>,
        end: Option<u32>,
        options: BarRangeOptions,
    ) -> io::Result<BarRangeCollection<DailyBar>> {
        DailyBarsRequest::new(market, code, 0, options.page_size)?;
        query(
            &mut self.tcp,
            RangeRequest {
                parameters: KlineRequest::new(market, code, 9, 0, options.page_size)?,
                command: 0x052b,
            },
            begin,
            end,
            options,
            |p, bytes| {
                Ok(decode_daily_bars(
                    &DailyBarsRequest::new(p.market(), p.code(), p.start(), p.count())?,
                    bytes,
                )?
                .records)
            },
            |bar| DateTime {
                year: bar.date.year() as u16,
                month: bar.date.month() as u16,
                day: bar.date.day() as u16,
                hour: 0,
                minute: 0,
            },
        )
    }

    /// Short pages advance by raw count until an empty page or the lower bound.
    /// Inspect complete/coverage before treating the result as an archive.
    pub fn bars_range_with_options<'a>(
        &mut self,
        market: u16,
        code: &'a str,
        category: u16,
        begin: Option<u32>,
        end: Option<u32>,
        options: BarRangeOptions,
    ) -> io::Result<BarRangeCollection<KlineData<'a>>> {
        stock(&mut self.tcp, market, code, category, begin, end, options)
    }

    pub fn index_bars_range_with_options<'a>(
        &mut self,
        market: u16,
        code: &'a str,
        category: u16,
        begin: Option<u32>,
        end: Option<u32>,
        options: BarRangeOptions,
    ) -> io::Result<BarRangeCollection<IndexKlineData<'a>>> {
        index(&mut self.tcp, market, code, category, begin, end, options)
    }
}

pub(super) fn stock<'a>(
    tcp: &mut Tcp,
    market: u16,
    code: &'a str,
    category: u16,
    begin: Option<u32>,
    end: Option<u32>,
    options: BarRangeOptions,
) -> io::Result<BarRangeCollection<KlineData<'a>>> {
    query(
        tcp,
        KlineRequest::new(market, code, category, 0, options.page_size)?.into(),
        begin,
        end,
        options,
        decode_stock_bars,
        |bar| bar.dt,
    )
}

pub(super) fn index<'a>(
    tcp: &mut Tcp,
    market: u16,
    code: &'a str,
    category: u16,
    begin: Option<u32>,
    end: Option<u32>,
    options: BarRangeOptions,
) -> io::Result<BarRangeCollection<IndexKlineData<'a>>> {
    query(
        tcp,
        KlineRequest::new(market, code, category, 0, options.page_size)?.into(),
        begin,
        end,
        options,
        decode_index_bars,
        |bar| bar.dt,
    )
}

pub(super) fn legacy_records<T>(result: BarRangeCollection<T>) -> io::Result<Vec<T>> {
    match result.termination {
        BarRangeTermination::PageLimit
        | BarRangeTermination::RowLimit
        | BarRangeTermination::ByteLimit
        | BarRangeTermination::OffsetLimit => Err(io::Error::other(format!(
            "K线区间预算耗尽: {:?}",
            result.termination
        ))),
        _ => Ok(result.records),
    }
}

fn query<'a, T: PartialEq>(
    tcp: &mut Tcp,
    request: RangeRequest<'a>,
    begin: Option<u32>,
    end: Option<u32>,
    options: BarRangeOptions,
    decode: impl Fn(&KlineRequest<'a>, &[u8]) -> io::Result<Vec<T>>,
    datetime: impl Fn(&T) -> DateTime,
) -> io::Result<BarRangeCollection<T>> {
    options.validate()?;
    validate_range(begin, end)?;
    tcp.with_timeout(
        options
            .timeout
            .min(crate::tcp::network_policy().pagination_timeout),
        |tcp| {
            let source = BarRangeSource::capture(tcp)?;
            collect(
                request,
                begin,
                end,
                source,
                options,
                |start, count| {
                    let page = KlineRequest::new(
                        request.parameters.market(),
                        request.parameters.code(),
                        request.parameters.category(),
                        start,
                        count,
                    )?;
                    let read = |tcp: &mut Tcp| -> io::Result<(Vec<T>, usize)> {
                        source.check(tcp)?;
                        let encoded = if request.command == 0x052b {
                            DailyBarsRequest::new(page.market(), page.code(), start, count)?
                                .encode(crate::tcp::frame::sequence())?
                        } else {
                            page.encode().to_vec()
                        };
                        let bytes = crate::tcp::send_recv_decompress(tcp, &encoded, "K线区间")?;
                        source.check(tcp)?;
                        Ok((decode(&page, &bytes)?, bytes.len()))
                    };
                    let (mut records, mut bytes) = read(tcp)?;
                    let mut requests = 1;
                    if records.is_empty() && tcp.config().recheck_empty {
                        tcp.delay(Duration::from_millis(300))?;
                        let confirmed = read(tcp)?;
                        records = confirmed.0;
                        bytes += confirmed.1;
                        requests += 1;
                    }
                    Ok((records, bytes, requests))
                },
                datetime,
            )
        },
    )
}

fn collect<T: PartialEq>(
    request: RangeRequest<'_>,
    begin: Option<u32>,
    end: Option<u32>,
    source: BarRangeSource,
    options: BarRangeOptions,
    mut fetch: impl FnMut(u16, u16) -> io::Result<(Vec<T>, usize, usize)>,
    datetime: impl Fn(&T) -> DateTime,
) -> io::Result<BarRangeCollection<T>> {
    let started_at = chrono::Utc::now();
    let mut all = BTreeMap::new();
    let mut pages = Vec::new();
    let mut raw_rows = 0;
    let mut payload_bytes = 0usize;
    let mut start = 0u32;
    let mut previous_oldest = None;
    let termination = loop {
        crate::operation::check()?;
        if pages.len() >= options.max_pages {
            break BarRangeTermination::PageLimit;
        }
        let Ok(offset) = u16::try_from(start) else {
            break BarRangeTermination::OffsetLimit;
        };
        let remaining = options.max_rows - raw_rows;
        if remaining == 0 {
            break BarRangeTermination::RowLimit;
        }
        let count = remaining.min(usize::from(options.page_size)) as u16;
        let (records, bytes, requests) = fetch(offset, count)?;
        validate_bar_dates(records.iter().map(&datetime))?;
        if records.len() > usize::from(count) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "K线页超过请求数量",
            ));
        }
        let total_bytes = payload_bytes
            .checked_add(bytes)
            .ok_or_else(|| io::Error::other("K线字节计量溢出"))?;
        payload_bytes = total_bytes;
        let raw_count = records.len();
        let oldest = records.iter().map(&datetime).min();
        let newest = records.iter().map(&datetime).max();
        if total_bytes > options.max_bytes {
            raw_rows += raw_count;
            pages.push(BarRangePage {
                start: offset,
                requested_count: count,
                raw_count,
                payload_bytes: bytes,
                requests,
                oldest,
                newest,
                duplicate_rows: 0,
                accepted: false,
            });
            break BarRangeTermination::ByteLimit;
        }
        if !records.is_empty() && previous_oldest.is_some_and(|prev| oldest >= Some(prev)) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "K线分页未向历史方向推进",
            ));
        }
        let mut duplicate_rows = 0;
        for record in records {
            let dt = datetime(&record);
            if let Some(previous) = all.get(&dt) {
                if previous != &record {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "K线重复时间戳的内容冲突",
                    ));
                }
                duplicate_rows += 1;
            } else {
                all.insert(dt, record);
            }
        }
        raw_rows += raw_count;
        pages.push(BarRangePage {
            start: offset,
            requested_count: count,
            raw_count,
            payload_bytes: bytes,
            requests,
            oldest,
            newest,
            duplicate_rows,
            accepted: true,
        });
        if raw_count == 0 {
            break BarRangeTermination::EmptyPageObserved;
        }
        if oldest.is_some_and(|dt| begin.is_some_and(|b| dt.to_u32() < b)) {
            break BarRangeTermination::LowerBoundReached;
        }
        if options.stop_on_short_page && raw_count < usize::from(count) {
            break BarRangeTermination::ShortPage;
        }
        previous_oldest = oldest;
        start += raw_count as u32;
    };
    let coverage = match termination {
        BarRangeTermination::LowerBoundReached => BarRangeCoverage::SourceLowerBoundReached,
        BarRangeTermination::EmptyPageObserved => BarRangeCoverage::SourceEnumerated,
        _ => BarRangeCoverage::Unverified,
    };
    let records: Vec<T> = all
        .into_iter()
        .filter(|(dt, _)| {
            begin.is_none_or(|b| dt.to_u32() >= b) && end.is_none_or(|e| dt.to_u32() <= e)
        })
        .map(|(_, record)| record)
        .collect();
    Ok(BarRangeCollection {
        market: request.parameters.market(),
        code: request.parameters.code().to_owned(),
        command: request.command,
        category: request.parameters.category(),
        requested_begin: begin,
        requested_end: end,
        actual_begin: records.first().map(&datetime),
        actual_end: records.last().map(&datetime),
        source,
        started_at,
        fetched_at: chrono::Utc::now(),
        pages,
        raw_rows,
        payload_bytes,
        records,
        complete: coverage != BarRangeCoverage::Unverified,
        termination,
        coverage,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dt(day: u16) -> DateTime {
        DateTime {
            year: 2025,
            month: 1,
            day,
            hour: 15,
            minute: 0,
        }
    }
    fn source() -> BarRangeSource {
        BarRangeSource {
            endpoint: "127.0.0.1:7709".parse().unwrap(),
            connection_id: 1,
            connection_generation: 1,
            observed_date: 20250110,
        }
    }
    fn request() -> RangeRequest<'static> {
        KlineRequest::new(0, "000001", 9, 0, 800).unwrap().into()
    }

    #[test]
    fn short_pages_continue_with_raw_offsets_and_record_duplicate_boundaries() {
        let mut offsets = Vec::new();
        let result = collect(
            request(),
            None,
            None,
            source(),
            BarRangeOptions::default(),
            |start, _| {
                offsets.push(start);
                Ok((
                    match start {
                        0 => vec![dt(8), dt(9)],
                        2 => vec![dt(7), dt(8)],
                        _ => vec![],
                    },
                    4,
                    1,
                ))
            },
            |dt| *dt,
        )
        .unwrap();
        assert_eq!(offsets, [0, 2, 4]);
        assert_eq!(result.records, [dt(7), dt(8), dt(9)]);
        assert_eq!(result.pages[1].duplicate_rows, 1);
        assert!(result.complete);
        assert_eq!(result.coverage, BarRangeCoverage::SourceEnumerated);
        assert_eq!(result.actual_begin, Some(dt(7)));
        assert_eq!(result.payload_bytes, 12);
    }

    #[test]
    fn limits_and_legacy_short_page_are_explicitly_incomplete() {
        for (options, expected) in [
            (
                BarRangeOptions {
                    max_pages: 1,
                    ..Default::default()
                },
                BarRangeTermination::PageLimit,
            ),
            (
                BarRangeOptions {
                    max_rows: 1,
                    ..Default::default()
                },
                BarRangeTermination::RowLimit,
            ),
            (
                BarRangeOptions {
                    max_bytes: 1,
                    ..Default::default()
                },
                BarRangeTermination::ByteLimit,
            ),
            (
                BarRangeOptions {
                    stop_on_short_page: true,
                    ..Default::default()
                },
                BarRangeTermination::ShortPage,
            ),
        ] {
            let result = collect(
                request(),
                None,
                None,
                source(),
                options,
                |_, _| Ok((vec![dt(9)], 2, 1)),
                |dt| *dt,
            )
            .unwrap();
            assert!(!result.complete);
            assert_eq!(result.termination, expected);
        }
        let result = collect(
            request(),
            Some(20250108),
            Some(20250109),
            source(),
            BarRangeOptions::default(),
            |_, _| Ok((vec![dt(7), dt(8), dt(9)], 8, 1)),
            |dt| *dt,
        )
        .unwrap();
        assert!(result.complete);
        assert_eq!(result.termination, BarRangeTermination::LowerBoundReached);
        assert_eq!(result.records, [dt(8), dt(9)]);
    }

    #[test]
    fn conflicting_duplicate_and_nonprogress_are_rejected() {
        let mut call = 0;
        let error = collect(
            request(),
            None,
            None,
            source(),
            BarRangeOptions::default(),
            |_, _| {
                call += 1;
                Ok((
                    if call == 1 {
                        vec![(dt(8), 1)]
                    } else {
                        vec![(dt(7), 1), (dt(8), 2)]
                    },
                    4,
                    1,
                ))
            },
            |row| row.0,
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(
            collect(
                request(),
                None,
                None,
                source(),
                BarRangeOptions::default(),
                |_, _| Ok((vec![dt(9)], 2, 1)),
                |dt| *dt
            )
            .is_err()
        );
    }

    #[test]
    fn reconnect_invalidates_the_source_even_when_endpoint_is_unchanged() {
        let (config, server) = crate::tcp::tests::mock_server(2, |_| vec![0, 0]);
        let mut tcp = Tcp::with_config(&config).unwrap();
        let source = BarRangeSource::capture(&tcp).unwrap();
        tcp.reconnect().unwrap();
        assert_eq!(
            source.check(&tcp).unwrap_err().kind(),
            io::ErrorKind::ConnectionAborted
        );
        drop(tcp);
        server.join().unwrap();
    }

    #[test]
    fn network_short_page_and_empty_confirmation_keep_one_session() {
        use std::sync::{Arc, Mutex};
        let offsets = Arc::new(Mutex::new(Vec::new()));
        let observed = offsets.clone();
        let hex = include_str!("../../../../tests/fixtures/kline/stock-day.response.hex");
        let payload: Vec<u8> = hex
            .trim()
            .as_bytes()
            .chunks_exact(2)
            .map(|p| u8::from_str_radix(std::str::from_utf8(p).unwrap(), 16).unwrap())
            .collect();
        let (mut config, server) = crate::tcp::tests::mock_server(1, move |request| {
            if request.get(10..12) != Some(&[0x2d, 5]) {
                return vec![0, 0];
            }
            let start = u16::from_le_bytes(request[24..26].try_into().unwrap());
            observed.lock().unwrap().push(start);
            if start == 0 {
                payload.clone()
            } else {
                vec![0, 0]
            }
        });
        config.recheck_empty = true;
        let mut client = Client::with_config(&config).unwrap();
        let result = client
            .bars_range_with_options(0, "000001", 9, None, None, BarRangeOptions::default())
            .unwrap();
        assert!(result.complete);
        assert_eq!(result.records.len(), 3);
        assert_eq!(
            result.source.connection_id,
            client.tcp.connection_id().get()
        );
        assert_eq!(result.pages[1].requests, 2);
        assert_eq!(&*offsets.lock().unwrap(), &[0, 3, 3]);
        drop(client);
        server.join().unwrap();
    }
}
