use super::{BasicQuote, PrecisionSnapshot, command_request::*};
use crate::tcp::{Tcp, reader::Reader};
use serde::Serialize;
use std::{collections::BTreeSet, io};

pub const CATEGORY_QUOTES_COMMAND: u16 = 0x054b;
#[derive(Debug, Clone, Copy, Serialize)]
pub enum QuoteCategory {
    AShares,
    Raw(u16),
}
impl QuoteCategory {
    pub fn raw(self) -> u16 {
        match self {
            Self::AShares => 6,
            Self::Raw(v) => v,
        }
    }
}
#[derive(Debug, Clone, Copy, Serialize)]
pub enum QuoteSortField {
    Code,
    Price,
    Amount,
    ChangePercent,
    OrderAmount,
    Raw(u16),
}
impl QuoteSortField {
    pub fn raw(self) -> u16 {
        match self {
            Self::Code => 0,
            Self::Price => 6,
            Self::Amount => 10,
            Self::ChangePercent => 14,
            Self::OrderAmount => 28,
            Self::Raw(v) => v,
        }
    }
}
#[derive(Debug, Clone, Copy)]
pub struct CategoryQuoteOptions {
    pub start: u16,
    pub count: u16,
    pub ascending: bool,
    pub sort_reverse_raw: Option<u16>,
    pub filter_raw: u16,
}
impl Default for CategoryQuoteOptions {
    fn default() -> Self {
        Self {
            start: 0,
            count: 80,
            ascending: false,
            sort_reverse_raw: None,
            filter_raw: 0,
        }
    }
}
#[derive(Debug, Clone, Copy, Serialize)]
pub struct CategoryQuotesRequest {
    category: u16,
    sort: u16,
    start: u16,
    count: u16,
    reverse: u16,
    filter: u16,
}
impl CategoryQuotesRequest {
    pub fn new(
        category: QuoteCategory,
        sort: QuoteSortField,
        options: CategoryQuoteOptions,
    ) -> io::Result<Self> {
        if options.count == 0 || options.count > 700 {
            return Err(input("分类报价count须为1..700（单帧客户端预算）"));
        }
        let reverse = options.sort_reverse_raw.unwrap_or(if sort.raw() == 0 {
            0
        } else if options.ascending {
            2
        } else {
            1
        });
        Ok(Self {
            category: category.raw(),
            sort: sort.raw(),
            start: options.start,
            count: options.count,
            reverse,
            filter: options.filter_raw,
        })
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let body: Vec<_> = [
            self.category,
            self.sort,
            self.start,
            self.count,
            self.reverse,
            5,
            self.filter,
            1,
            0,
        ]
        .into_iter()
        .flat_map(u16::to_le_bytes)
        .collect();
        crate::tcp::frame::request(CATEGORY_QUOTES_COMMAND, id, &body)
    }
    pub fn recv(
        &self,
        tcp: &mut Tcp,
        metadata: &PrecisionSnapshot,
    ) -> io::Result<CategoryQuotePage> {
        for market in [0, 1] {
            if !metadata.is_fresh(market, chrono::Utc::now()) {
                return Err(input("分类报价须预热沪深精度目录"));
            }
        }
        let bytes = tcp.exchange_checked(&self.encode(crate::tcp::frame::sequence())?)?;
        let mut result = decode_category_quotes(self, &bytes, metadata)?;
        for market in [0, 1] {
            if !metadata.is_fresh(market, chrono::Utc::now()) {
                return Err(invalid("分类报价请求跨日"));
            }
        }
        result.page.source.attach(tcp)?;
        Ok(result)
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct CategoryQuote {
    pub quote: BasicQuote,
    pub status_or_sort_raw: u16,
    pub rise_speed_raw: i16,
    pub rise_speed: f64,
    pub short_turnover_raw: i16,
    pub short_turnover: f64,
    pub two_minute_amount: f32,
    pub opening_rush_raw: i16,
    pub opening_rush: f64,
    pub extra_pair_raw: Vec<u8>,
    pub volume_rise_speed: f32,
    pub depth_raw: f32,
    pub extra_metadata_raw: Vec<u8>,
    pub active2_raw: u16,
}
#[derive(Debug, Clone, Serialize)]
pub struct CategoryQuotePage {
    pub request: CategoryQuotesRequest,
    pub header_raw: u16,
    pub page: CommandPage<CategoryQuote>,
}
pub type CategoryQuoteCollection = CommandCollection<CategoryQuote>;
pub fn decode_category_quotes(
    request: &CategoryQuotesRequest,
    bytes: &[u8],
    metadata: &PrecisionSnapshot,
) -> io::Result<CategoryQuotePage> {
    bounded(bytes)?;
    let mut r = Reader::new(bytes);
    let header_raw = r.u16()?;
    let n = count(&mut r, 86, usize::from(request.count))?;
    let mut records = Vec::with_capacity(n);
    let mut seen = BTreeSet::new();
    for _ in 0..n {
        let mut quote = super::extended_quote::core(&mut r, metadata, false, 1, true)?;
        if !seen.insert((quote.market_raw, quote.code.clone())) {
            return Err(invalid("分类报价重复证券"));
        }
        let tail = r.take(56)?;
        quote.tail_raw = tail.to_vec();
        let mut t = Reader::new(tail);
        let status_or_sort_raw = t.u16()?;
        let rise_speed_raw = t.u16()? as i16;
        let short_turnover_raw = t.u16()? as i16;
        let two_minute_amount = t.f32()?;
        let opening_rush_raw = t.u16()? as i16;
        let extra_pair_raw = t.take(10)?.to_vec();
        let volume_rise_speed = t.f32()?;
        let depth_raw = t.f32()?;
        let extra_metadata_raw = t.take(24)?.to_vec();
        let active2_raw = t.u16()?;
        records.push(CategoryQuote {
            quote,
            status_or_sort_raw,
            rise_speed_raw,
            rise_speed: f64::from(rise_speed_raw) / 100.0,
            short_turnover_raw,
            short_turnover: f64::from(short_turnover_raw) / 100.0,
            two_minute_amount,
            opening_rush_raw,
            opening_rush: f64::from(opening_rush_raw) / 100.0,
            extra_pair_raw,
            volume_rise_speed,
            depth_raw,
            extra_metadata_raw,
            active2_raw,
        });
    }
    exact(&r)?;
    let mut page = CommandPage::new(CATEGORY_QUOTES_COMMAND, records, bytes);
    page.source.metadata_generation = Some(metadata.generation());
    Ok(CategoryQuotePage {
        request: *request,
        header_raw,
        page,
    })
}
pub(crate) fn scan(
    tcp: &mut Tcp,
    mut request: CategoryQuotesRequest,
    metadata: &PrecisionSnapshot,
    options: CommandScanOptions,
) -> io::Result<CategoryQuoteCollection> {
    options.validate()?;
    tcp.with_timeout(options.timeout, |tcp| {
        let mut result = CommandCollection {
            pages: Vec::new(),
            record_count: 0,
            termination: CommandTermination::PageBudget,
            complete: false,
            atomic: false,
        };
        let mut seen = BTreeSet::new();
        let mut baseline = None;
        for _ in 0..options.max_pages {
            let page = request.recv(tcp, metadata)?.page;
            check_scan_source(&mut baseline, &page.source)?;
            let n = page.records.len();
            if n > options.max_records - result.record_count {
                result.termination = CommandTermination::RecordBudget;
                break;
            }
            for q in &page.records {
                if !seen.insert((q.quote.market_raw, q.quote.code.clone())) {
                    return Err(invalid("分类行情分页重叠，动态排名请重取"));
                }
            }
            result.record_count += n;
            result.pages.push(page);
            if n < usize::from(request.count) {
                result.complete = true;
                result.termination = if n == 0 {
                    CommandTermination::EmptyPage
                } else {
                    CommandTermination::ShortPage
                };
                break;
            }
            let Some(next) = request.start.checked_add(n as u16) else {
                result.termination = CommandTermination::OffsetLimit;
                break;
            };
            request.start = next;
        }
        Ok(result)
    })
}
