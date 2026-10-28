//! 056a records are auction process snapshots, including points at 09:25.
use super::command_request::*;
use crate::tcp::{Tcp, reader::Reader};
use serde::Serialize;
use std::{collections::BTreeSet, io};

super::client::batch_budget::impl_heap!(AuctionSeriesRequest; symbol);

pub const AUCTION_SERIES_COMMAND: u16 = 0x056a;
#[derive(Debug, Clone, Copy)]
pub struct AuctionOptions {
    pub mode: u32,
    pub start: u32,
    pub count: u32,
}
impl Default for AuctionOptions {
    fn default() -> Self {
        Self {
            mode: 3,
            start: 0,
            count: 500,
        }
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct AuctionSeriesRequest {
    symbol: CommandSymbol,
    date_raw: u32,
    mode: u32,
    start: u32,
    count: u32,
}
impl AuctionSeriesRequest {
    pub fn new(
        market: u16,
        code: &str,
        trading_date: Option<u32>,
        options: AuctionOptions,
    ) -> io::Result<Self> {
        if let Some(day) = trading_date {
            date(day)?;
        }
        // Response length is a u16; this is a local safety bound, not a server cap.
        if options.count == 0
            || options.count > 4095
            || options.start.checked_add(options.count).is_none()
        {
            return Err(input("竞价count须为1..4095且偏移不得溢出"));
        }
        Ok(Self {
            symbol: CommandSymbol::new(market, code)?,
            date_raw: trading_date.unwrap_or(0),
            mode: options.mode,
            start: options.start,
            count: options.count,
        })
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = Vec::with_capacity(28);
        self.symbol.encode(&mut body, true);
        for value in [self.date_raw, self.mode, 0, self.start, self.count] {
            body.extend_from_slice(&value.to_le_bytes());
        }
        crate::tcp::frame::request(AUCTION_SERIES_COMMAND, id, &body)
    }
    pub fn recv(&self, tcp: &mut Tcp) -> io::Result<AuctionSeries> {
        let bytes = tcp.exchange_checked(&self.encode(crate::tcp::frame::sequence())?)?;
        let mut result = decode_auction_series(self, &bytes)?;
        result.page.source.attach(tcp)?;
        Ok(result)
    }
}
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct AuctionPoint {
    pub index: u32,
    pub minute_raw: u16,
    pub second_raw: u8,
    pub time_label: String,
    pub price: f32,
    pub price_bits: u32,
    pub matched_volume_raw: u32,
    pub unmatched_signed_raw: i32,
    pub unmatched_volume_raw: u32,
    /// Sign only; zero is neutral. The side mapping is not exchange-confirmed.
    pub unmatched_sign: i8,
    pub reserved_raw: u8,
}
#[derive(Debug, Clone, Serialize)]
pub struct AuctionSeries {
    pub request: AuctionSeriesRequest,
    pub requested_date: Option<u32>,
    pub server_trading_date: Option<u32>,
    pub process_snapshots: bool,
    pub page: CommandPage<AuctionPoint>,
}
pub type AuctionCollection = CommandCollection<AuctionPoint>;
pub fn decode_auction_series(
    request: &AuctionSeriesRequest,
    bytes: &[u8],
) -> io::Result<AuctionSeries> {
    bounded(bytes)?;
    let mut r = Reader::new(bytes);
    let n = count(&mut r, 16, request.count as usize)?;
    let mut points = Vec::with_capacity(n);
    for index in 0..n {
        let minute_raw = r.u16()?;
        let price = r.f32()?;
        let matched_volume_raw = r.u32()?;
        let unmatched_signed_raw = r.u32()? as i32;
        let reserved_raw = r.u8()?;
        let second_raw = r.u8()?;
        if minute_raw >= 1440 || second_raw >= 60 || price < 0.0 {
            return Err(invalid("056a时间/价格非法"));
        }
        points.push(AuctionPoint {
            index: request.start + index as u32,
            minute_raw,
            second_raw,
            time_label: format!(
                "{:02}:{:02}:{:02}",
                minute_raw / 60,
                minute_raw % 60,
                second_raw
            ),
            price,
            price_bits: price.to_bits(),
            matched_volume_raw,
            unmatched_signed_raw,
            unmatched_volume_raw: unmatched_signed_raw.unsigned_abs(),
            unmatched_sign: unmatched_signed_raw.signum() as i8,
            reserved_raw,
        });
    }
    exact(&r)?;
    Ok(AuctionSeries {
        request: request.clone(),
        requested_date: (request.date_raw != 0).then_some(request.date_raw),
        server_trading_date: None,
        process_snapshots: true,
        page: CommandPage::new(AUCTION_SERIES_COMMAND, points, bytes),
    })
}
pub(crate) fn scan(
    tcp: &mut Tcp,
    mut request: AuctionSeriesRequest,
    options: CommandScanOptions,
) -> io::Result<AuctionCollection> {
    options.validate()?;
    tcp.with_timeout(options.timeout, |tcp| {
        let mut result = CommandCollection {
            pages: Vec::new(),
            record_count: 0,
            termination: CommandTermination::PageBudget,
            complete: false,
            atomic: false,
        };
        let mut fingerprints = BTreeSet::new();
        let mut baseline = None;
        for _ in 0..options.max_pages {
            let page = request.recv(tcp)?.page;
            check_scan_source(&mut baseline, &page.source)?;
            let n = page.records.len();
            if n > options.max_records - result.record_count {
                result.termination = CommandTermination::RecordBudget;
                break;
            }
            if n > 0 && !fingerprints.insert(page.raw_payload.to_vec()) {
                return Err(invalid("056a分页未推进"));
            }
            result.record_count += n;
            result.pages.push(page);
            if n == 0 || n < request.count as usize {
                result.termination = if n == 0 {
                    CommandTermination::EmptyPage
                } else {
                    CommandTermination::ShortPage
                };
                result.complete = true;
                break;
            }
            let Some(next) = request.start.checked_add(n as u32) else {
                result.termination = CommandTermination::OffsetLimit;
                break;
            };
            request.start = next;
        }
        Ok(result)
    })
}
