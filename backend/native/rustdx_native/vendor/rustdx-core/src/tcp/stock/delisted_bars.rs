//! 052b: unadjusted daily bars. Volume is a sign/magnitude integer in shares.
use super::command_request::*;
use crate::tcp::{Tcp, reader::Reader};
use chrono::{DateTime, FixedOffset, NaiveDate, TimeZone};
use serde::Serialize;
use std::{collections::BTreeMap, io, sync::Arc};

super::client::batch_budget::impl_heap!(DelistedBarsRequest; code);

pub const DELISTED_BARS_COMMAND: u16 = 0x052b;
pub const MAX_DELISTED_BARS_COUNT: u16 = 800;

#[derive(Debug, Clone, Serialize)]
pub struct DelistedBarsRequest {
    market: u16,
    code: String,
    start: u16,
    count: u16,
}
impl DelistedBarsRequest {
    pub fn new(market: u16, code: &str, start: u16, count: u16) -> io::Result<Self> {
        let code = super::company_info_request::resource_symbol(market, code)?;
        if count == 0 || count > MAX_DELISTED_BARS_COUNT {
            return Err(input("052b每页数量须为1..800"));
        }
        Ok(Self {
            market,
            code,
            start,
            count,
        })
    }
    pub fn market(&self) -> u16 {
        self.market
    }
    pub fn code(&self) -> &str {
        &self.code
    }
    pub fn start(&self) -> u16 {
        self.start
    }
    pub fn count(&self) -> u16 {
        self.count
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = [0; 12];
        body[..2].copy_from_slice(&self.market.to_le_bytes());
        body[2..8].copy_from_slice(self.code.as_bytes());
        body[8..10].copy_from_slice(&self.start.to_le_bytes());
        body[10..12].copy_from_slice(&self.count.to_le_bytes());
        crate::tcp::frame::request(DELISTED_BARS_COMMAND, id, &body)
    }
    pub fn recv(&self, tcp: &mut Tcp) -> io::Result<DelistedBarsPage> {
        let bytes = tcp.exchange_checked(&self.encode(crate::tcp::frame::sequence())?)?;
        let mut page = decode_delisted_bars(self, &bytes)?;
        page.source.attach(tcp)?;
        Ok(page)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct DelistedBar {
    pub date_raw: u32,
    pub date: NaiveDate,
    pub time: DateTime<FixedOffset>,
    pub open: f64,
    pub close: f64,
    pub high: f64,
    pub low: f64,
    pub open_price_milli: i64,
    pub close_price_milli: i64,
    pub high_price_milli: i64,
    pub low_price_milli: i64,
    /// None on the first record of each wire page; never inferred across pages.
    pub last_close_price_milli: Option<i64>,
    pub open_delta_raw: i64,
    pub close_delta_raw: i64,
    pub high_delta_raw: i64,
    pub low_delta_raw: i64,
    pub volume_raw: u32,
    pub volume_shares: u32,
    pub volume_lots: f64,
    pub amount_raw: u32,
    pub amount: f64,
    pub unknown_suffix_raw: [u8; 4],
    pub record_raw: Vec<u8>,
}
#[derive(Debug, Clone, Serialize)]
pub struct DelistedBarsPage {
    pub request: DelistedBarsRequest,
    /// Original wire order. Collection.records provides ascending unique dates.
    pub records: Vec<DelistedBar>,
    pub source: CommandSource,
    #[serde(skip)]
    pub raw_payload: Arc<[u8]>,
}

/// IEEE binary32 yuan, widened without rounding to binary64.
/// Use the checked page decoder for untrusted input.
pub fn decode_delisted_amount(raw: u32) -> f64 {
    f64::from(f32::from_bits(raw))
}

pub fn decode_delisted_bars(
    request: &DelistedBarsRequest,
    bytes: &[u8],
) -> io::Result<DelistedBarsPage> {
    bounded(bytes)?;
    let mut reader = Reader::new(bytes);
    let n = count(&mut reader, 17, usize::from(request.count))?;
    let mut records = Vec::with_capacity(n);
    let mut last_close = 0;
    let zone = FixedOffset::east_opt(8 * 3600).ok_or_else(|| invalid("052b日期时区"))?;
    for _ in 0..n {
        let record_start = reader.position();
        let date_raw = reader.u32()?;
        let date = date(date_raw).map_err(|e| invalid(&e.to_string()))?;
        let time = zone
            .from_local_datetime(&date.and_hms_opt(0, 0, 0).unwrap())
            .single()
            .ok_or_else(|| invalid("052b日期无效"))?;
        let open_delta_raw = reader.varint64()?;
        let close_delta_raw = reader.varint64()?;
        let high_delta_raw = reader.varint64()?;
        let low_delta_raw = reader.varint64()?;
        let open_price_milli = add(last_close, open_delta_raw)?;
        let close_price_milli = add(open_price_milli, close_delta_raw)?;
        let high_price_milli = add(open_price_milli, high_delta_raw)?;
        let low_price_milli = add(open_price_milli, low_delta_raw)?;
        if low_price_milli < 0
            || low_price_milli > open_price_milli.min(close_price_milli)
            || high_price_milli < open_price_milli.max(close_price_milli)
            || low_price_milli > high_price_milli
        {
            return Err(invalid("052b OHLC关系无效"));
        }
        let volume_raw =
            u32::try_from(reader.varint64()?).map_err(|_| invalid("052b成交量须为非负u32股数"))?;
        let amount_raw = reader.u32()?;
        let unknown_suffix_raw = reader.take(4)?.try_into().unwrap();
        records.push(DelistedBar {
            date_raw,
            date,
            time,
            open: open_price_milli as f64 / 1000.0,
            close: close_price_milli as f64 / 1000.0,
            high: high_price_milli as f64 / 1000.0,
            low: low_price_milli as f64 / 1000.0,
            open_price_milli,
            close_price_milli,
            high_price_milli,
            low_price_milli,
            last_close_price_milli: if records.is_empty() {
                None
            } else {
                Some(last_close)
            },
            open_delta_raw,
            close_delta_raw,
            high_delta_raw,
            low_delta_raw,
            volume_raw,
            volume_shares: volume_raw,
            volume_lots: f64::from(volume_raw) / 100.0,
            amount_raw,
            amount: super::decode_nonnegative_binary32(amount_raw)?,
            unknown_suffix_raw,
            record_raw: bytes[record_start..reader.position()].to_vec(),
        });
        last_close = close_price_milli;
    }
    exact(&reader)?;
    Ok(DelistedBarsPage {
        request: request.clone(),
        records,
        source: CommandSource::offline(DELISTED_BARS_COMMAND),
        raw_payload: bytes.into(),
    })
}

#[derive(Debug, Clone, Serialize)]
pub struct DelistedBarsCollection {
    pub pages: Vec<DelistedBarsPage>,
    /// Sorted by date, identical overlaps removed; conflicting overlaps fail.
    pub records: Vec<DelistedBar>,
    pub raw_record_count: usize,
    pub termination: CommandTermination,
    /// Exhausted this endpoint's 052b archive, not an exchange-history guarantee.
    pub complete: bool,
    pub atomic: bool,
}
fn same_bar(a: &DelistedBar, b: &DelistedBar) -> bool {
    a.open_price_milli == b.open_price_milli
        && a.close_price_milli == b.close_price_milli
        && a.high_price_milli == b.high_price_milli
        && a.low_price_milli == b.low_price_milli
        && a.volume_raw == b.volume_raw
        && a.amount_raw == b.amount_raw
        && a.unknown_suffix_raw == b.unknown_suffix_raw
}
// Wire deltas and last-close are page-relative; overlapping pages can encode
// the same daily bar differently. Compare absolute fields and the raw suffix.
impl PartialEq for DelistedBar {
    fn eq(&self, other: &Self) -> bool {
        self.date_raw == other.date_raw && same_bar(self, other)
    }
}

/// General 052b names. The legacy Delisted* names remain source-compatible;
/// this command also serves listed stocks on validated endpoints.
pub type DailyBar = DelistedBar;
pub type DailyBarsRequest = DelistedBarsRequest;
pub type DailyBarsPage = DelistedBarsPage;
pub use decode_delisted_bars as decode_daily_bars;

pub fn delisted_bars_all(
    tcp: &mut Tcp,
    mut request: DelistedBarsRequest,
    options: CommandScanOptions,
) -> io::Result<DelistedBarsCollection> {
    options.validate()?;
    tcp.with_timeout(options.timeout, |tcp| {
        let page_size = request.count;
        let mut result = DelistedBarsCollection {
            pages: Vec::new(),
            records: Vec::new(),
            raw_record_count: 0,
            termination: CommandTermination::PageBudget,
            complete: false,
            atomic: false,
        };
        let mut merged = BTreeMap::new();
        let mut baseline = None;
        for _ in 0..options.max_pages {
            let remaining = options.max_records - result.raw_record_count;
            if remaining == 0 {
                result.termination = CommandTermination::RecordBudget;
                break;
            }
            request.count = page_size.min(remaining.min(u16::MAX as usize) as u16);
            let page = request.recv(tcp)?;
            check_scan_source(&mut baseline, &page.source)?;
            let n = page.records.len();
            let before = merged.len();
            for bar in &page.records {
                if let Some(old) = merged.get(&bar.date) {
                    if !same_bar(old, bar) {
                        return Err(invalid("052b分页同日期数据冲突"));
                    }
                } else {
                    merged.insert(bar.date, bar.clone());
                }
            }
            if n != 0 && merged.len() == before {
                return Err(invalid("052b分页重复且没有新增日期"));
            }
            result.raw_record_count += n;
            result.pages.push(page);
            if n == 0 {
                result.termination = CommandTermination::EmptyPage;
                result.complete = true;
                break;
            }
            let Some(next) = request.start.checked_add(n as u16) else {
                result.termination = CommandTermination::OffsetLimit;
                break;
            };
            request.start = next;
        }
        if !result.complete && result.raw_record_count == options.max_records {
            result.termination = CommandTermination::RecordBudget;
        }
        result.records = merged.into_values().collect();
        Ok(result)
    })
}
