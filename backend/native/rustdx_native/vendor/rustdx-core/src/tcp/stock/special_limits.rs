//! 0452 is a table of exceptional limits, not a complete exchange rule book.
use super::command_request::*;
use crate::tcp::{Tcp, reader::Reader};
use serde::Serialize;
use std::{collections::BTreeSet, io};

pub const SPECIAL_LIMITS_COMMAND: u16 = 0x0452;
pub type SpecialLimitPage = CommandPage<SpecialLimitRecord>;
pub type SpecialLimitCollection = CommandCollection<SpecialLimitRecord>;

#[derive(Debug, Clone, Copy)]
pub struct SpecialLimitsRequest {
    start: u16,
}
impl SpecialLimitsRequest {
    pub fn new(start: u16) -> Self {
        Self { start }
    }
    pub fn start(&self) -> u16 {
        self.start
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = [0; 14];
        body[..2].copy_from_slice(&self.start.to_le_bytes());
        crate::tcp::frame::request(SPECIAL_LIMITS_COMMAND, id, &body)
    }
    pub fn recv(&self, tcp: &mut Tcp) -> io::Result<SpecialLimitPage> {
        let bytes = tcp.exchange_checked(&self.encode(crate::tcp::frame::sequence())?)?;
        let mut page = decode_special_limits(&bytes)?;
        page.source.attach(tcp)?;
        Ok(page)
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct SpecialLimitRecord {
    pub market_raw: u8,
    pub code_number_raw: u32,
    pub code: String,
    pub upper_price: f32,
    pub lower_price: f32,
    pub upper_bits: u32,
    pub lower_bits: u32,
}
pub fn decode_special_limits(bytes: &[u8]) -> io::Result<SpecialLimitPage> {
    bounded(bytes)?;
    let mut r = Reader::new(bytes);
    let n = count(&mut r, 13, 5041)?;
    let mut records = Vec::with_capacity(n);
    let mut seen = BTreeSet::new();
    for _ in 0..n {
        let market_raw = r.u8()?;
        let code_number_raw = r.u32()?;
        let upper_price = r.f32()?;
        let lower_price = r.f32()?;
        if code_number_raw > 999_999
            || !seen.insert((market_raw, code_number_raw))
            || upper_price < 0.0
            || lower_price < 0.0
            || upper_price < lower_price
        {
            return Err(invalid("0452代码、重复记录或上下限异常"));
        }
        records.push(SpecialLimitRecord {
            market_raw,
            code_number_raw,
            code: format!("{code_number_raw:06}"),
            upper_price,
            lower_price,
            upper_bits: upper_price.to_bits(),
            lower_bits: lower_price.to_bits(),
        });
    }
    exact(&r)?;
    Ok(CommandPage::new(SPECIAL_LIMITS_COMMAND, records, bytes))
}

pub(crate) fn scan(
    tcp: &mut Tcp,
    options: CommandScanOptions,
) -> io::Result<SpecialLimitCollection> {
    options.validate()?;
    tcp.with_timeout(options.timeout, |tcp| {
        let mut report = CommandCollection {
            pages: Vec::new(),
            record_count: 0,
            termination: CommandTermination::PageBudget,
            complete: false,
            atomic: false,
        };
        let mut start = 0u16;
        let mut seen = BTreeSet::new();
        let mut baseline = None;
        for _ in 0..options.max_pages {
            let page = SpecialLimitsRequest::new(start).recv(tcp)?;
            check_scan_source(&mut baseline, &page.source)?;
            let n = page.records.len();
            if n > options.max_records - report.record_count {
                report.termination = CommandTermination::RecordBudget;
                break;
            }
            for row in &page.records {
                if !seen.insert((row.market_raw, row.code_number_raw)) {
                    return Err(invalid("0452分页重复记录"));
                }
            }
            report.record_count += n;
            report.pages.push(page);
            if n == 0 {
                report.termination = CommandTermination::EmptyPage;
                report.complete = true;
                break;
            }
            let Some(next) = start.checked_add(n as u16) else {
                report.termination = CommandTermination::OffsetLimit;
                break;
            };
            start = next;
        }
        Ok(report)
    })
}
