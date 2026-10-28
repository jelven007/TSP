//! Lossless trade events and checked collections for 0fc5 / 0fc6 / legacy 0fb5.
//! Classification is a versioned community protocol profile, not an exchange
//! event identifier or a proof of a complete trading day.
use std::{
    io::{self, ErrorKind},
    net::SocketAddr,
    num::NonZeroU64,
    ops::Range,
};

use chrono::{DateTime, Utc};

/// Version of the classification rules, independent of quote precision metadata.
pub const TRADE_SEMANTIC_SCHEMA_VERSION: u16 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[non_exhaustive]
pub enum TradeProtocol {
    Today0fc5,
    EnhancedHistory0fc6,
    /// No trade-count slot. Classification remains candidate semantics.
    LegacyHistory0fb5,
}

/// Immutable security/date/protocol context. The current-day command does not
/// carry a date; a local calendar observation must not become a verified date.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct TradeQuery {
    market: u16,
    code: String,
    date: Option<u32>,
    protocol: TradeProtocol,
}

impl TradeQuery {
    pub fn today(market: u16, code: &str) -> io::Result<Self> {
        super::TransactionRequest::new(market, code, 0, 1)?;
        Ok(Self {
            market,
            code: code.to_owned(),
            date: None,
            protocol: TradeProtocol::Today0fc5,
        })
    }

    /// Default enhanced history protocol (0fc6, 1–1800 records/page).
    pub fn history(market: u16, code: &str, date: u32) -> io::Result<Self> {
        super::EnhancedHistoryTransactionRequest::new(market, code, 0, 1, date)?;
        Ok(Self {
            market,
            code: code.to_owned(),
            date: Some(date),
            protocol: TradeProtocol::EnhancedHistory0fc6,
        })
    }

    pub fn history_legacy(market: u16, code: &str, date: u32) -> io::Result<Self> {
        super::HistoryTransactionRequest::new(market, code, 0, 1, date)?;
        Ok(Self {
            market,
            code: code.to_owned(),
            date: Some(date),
            protocol: TradeProtocol::LegacyHistory0fb5,
        })
    }

    pub fn market(&self) -> u16 {
        self.market
    }
    pub fn code(&self) -> &str {
        &self.code
    }
    /// Requested historical date; never a date asserted by the response.
    pub fn date(&self) -> Option<u32> {
        self.date
    }
    pub fn protocol(&self) -> TradeProtocol {
        self.protocol
    }

    pub fn encode(&self, start: u16, count: u16) -> io::Result<Vec<u8>> {
        if self.protocol == TradeProtocol::EnhancedHistory0fc6 {
            return super::EnhancedHistoryTransactionRequest::new(
                self.market,
                &self.code,
                start,
                count,
                self.date
                    .ok_or_else(|| io::Error::other("增强历史查询缺少日期"))?,
            )
            .map(|request| request.encode().to_vec());
        }
        match self.date {
            Some(date) => {
                super::HistoryTransactionRequest::new(self.market, &self.code, start, count, date)
                    .map(|request| request.encode().to_vec())
            }
            None => super::TransactionRequest::new(self.market, &self.code, start, count)
                .map(|request| request.encode().to_vec()),
        }
    }

    /// One request only. Source metadata identifies the connection that returned
    /// the successful response, including any transport-level automatic retry.
    pub fn recv_page(
        &self,
        tcp: &mut crate::tcp::Tcp,
        start: u16,
        count: u16,
    ) -> io::Result<TradePage> {
        let bytes =
            crate::tcp::send_recv_decompress(tcp, &self.encode(start, count)?, "成交事件页")?;
        let mut page = super::decode_trade_page(self, start, count, &bytes)?;
        page.source = Some(TradeSource {
            endpoint: tcp.get_ref().0.peer_addr()?,
            connection_id: tcp.connection_id(),
            fetched_at: Utc::now(),
        });
        Ok(page)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[non_exhaustive]
pub enum TradeEventKind {
    AuctionSnapshot,
    OpeningMatch,
    Trade,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[non_exhaustive]
pub enum TradePhase {
    AuctionProcess,
    Opening,
    ClosingMatch,
    AfterHoursFixedPrice,
    Regular,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub enum TradeDirection {
    Buy,
    Sell,
    Neutral,
    Unknown,
}

/// Only `ConfirmedByProfile` is included in the default executed-trade view.
/// It means supported by this protocol profile, not independently verified.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub enum TradeQualification {
    ConfirmedByProfile,
    Candidate,
    ExcludedByProfile,
    Unknown,
}

/// A decoded record keeps its page-relative price delta and raw field slots.
/// Construction/deserialization is restricted so derived classification cannot
/// drift from the raw status and protocol.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct TradeRecord {
    time_minutes: u16,
    price_delta_raw: i32,
    price_raw: i64,
    volume_raw: i32,
    trade_count_raw: Option<i32>,
    status_raw: i32,
    tail_raw: i32,
    byte_range: Range<usize>,
    protocol: TradeProtocol,
    event_kind: TradeEventKind,
    phase: TradePhase,
    direction: TradeDirection,
    qualification: TradeQualification,
}

impl TradeRecord {
    pub(super) fn from_raw(
        raw: super::trade_decode::RawTradeRecord,
        protocol: TradeProtocol,
    ) -> Self {
        let super::trade_decode::RawTradeRecord {
            time_minutes,
            price_delta_raw,
            price_raw,
            volume_raw,
            trade_count_raw,
            status_raw,
            tail_raw,
            byte_range,
        } = raw;
        let (event_kind, phase, direction, qualification) =
            classify(time_minutes, status_raw, protocol);
        Self {
            time_minutes,
            price_delta_raw,
            price_raw,
            volume_raw,
            trade_count_raw,
            status_raw,
            tail_raw,
            byte_range,
            protocol,
            event_kind,
            phase,
            direction,
            qualification,
        }
    }

    pub fn time_minutes(&self) -> u16 {
        self.time_minutes
    }
    pub fn price_delta_raw(&self) -> i32 {
        self.price_delta_raw
    }
    pub fn price_raw(&self) -> i64 {
        self.price_raw
    }
    /// Compatibility /100 scale for these trade protocols; does not use 053e
    /// quote precision metadata or establish precision for other instruments.
    pub fn price(&self) -> f64 {
        self.price_raw as f64 / 100.0
    }
    /// Unconverted protocol quantity. No implicit multiplication by 100.
    pub fn volume_raw(&self) -> i32 {
        self.volume_raw
    }
    /// Raw count-like slot, not a stable transaction ID. Legacy 0fb5 has None.
    pub fn trade_count_raw(&self) -> Option<i32> {
        self.trade_count_raw
    }
    pub fn status_raw(&self) -> i32 {
        self.status_raw
    }
    pub fn tail_raw(&self) -> i32 {
        self.tail_raw
    }
    pub fn byte_range(&self) -> Range<usize> {
        self.byte_range.clone()
    }
    pub fn protocol(&self) -> TradeProtocol {
        self.protocol
    }
    pub fn event_kind(&self) -> TradeEventKind {
        self.event_kind
    }
    pub fn phase(&self) -> TradePhase {
        self.phase
    }
    pub fn direction(&self) -> TradeDirection {
        self.direction
    }
    pub fn qualification(&self) -> TradeQualification {
        self.qualification
    }
    pub fn is_confirmed_trade(&self) -> bool {
        self.qualification == TradeQualification::ConfirmedByProfile
    }

    pub fn to_legacy(&self) -> super::TransactionData {
        super::TransactionData {
            time: format!(
                "{:02}:{:02}",
                self.time_minutes / 60,
                self.time_minutes % 60
            ),
            price: self.price(),
            vol: self.volume_raw,
            num: self.trade_count_raw.unwrap_or(0),
            buyorsell: self.status_raw,
        }
    }
}

fn classify(
    time: u16,
    status: i32,
    protocol: TradeProtocol,
) -> (
    TradeEventKind,
    TradePhase,
    TradeDirection,
    TradeQualification,
) {
    use TradeQualification as Q;
    // Status takes precedence over the 09:25 / 15:00 time boundary.
    if status == 8 {
        return (
            TradeEventKind::AuctionSnapshot,
            TradePhase::AuctionProcess,
            TradeDirection::Unknown,
            Q::ExcludedByProfile,
        );
    }
    let direction = match status {
        0 => TradeDirection::Buy,
        1 => TradeDirection::Sell,
        2 => TradeDirection::Neutral,
        _ => TradeDirection::Unknown,
    };
    let opening = time == 9 * 60 + 25;
    let closing = time == 15 * 60;
    let after_hours = status == 5 && (15 * 60 + 5..=15 * 60 + 30).contains(&time);
    let regular =
        (9 * 60 + 30..=11 * 60 + 30).contains(&time) || (13 * 60..15 * 60).contains(&time);
    let kind = if opening {
        TradeEventKind::OpeningMatch
    } else if matches!(status, 0..=2) || closing || after_hours {
        TradeEventKind::Trade
    } else {
        TradeEventKind::Unknown
    };
    let phase = if opening {
        TradePhase::Opening
    } else if closing {
        TradePhase::ClosingMatch
    } else if after_hours {
        TradePhase::AfterHoursFixedPrice
    } else if regular && matches!(status, 0..=2) {
        TradePhase::Regular
    } else {
        TradePhase::Unknown
    };
    let supported = (matches!(status, 0..=2) && (opening || closing || regular)) || after_hours;
    let qualification = if supported && protocol != TradeProtocol::LegacyHistory0fb5 {
        Q::ConfirmedByProfile
    } else if supported || matches!(status, 0..=2 | 5) {
        Q::Candidate
    } else {
        Q::Unknown
    };
    (kind, phase, direction, qualification)
}

/// Captured by managed live I/O. IDs are unique within the current process;
/// include an archive/run ID when persisting across processes.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct TradeSource {
    endpoint: SocketAddr,
    connection_id: NonZeroU64,
    fetched_at: DateTime<Utc>,
}

impl TradeSource {
    pub(super) fn capture(tcp: &crate::tcp::Tcp) -> io::Result<Self> {
        Ok(Self {
            endpoint: tcp.get_ref().0.peer_addr()?,
            connection_id: tcp.connection_id(),
            fetched_at: Utc::now(),
        })
    }

    pub fn endpoint(&self) -> SocketAddr {
        self.endpoint
    }
    pub fn connection_id(&self) -> NonZeroU64 {
        self.connection_id
    }
    pub fn fetched_at(&self) -> DateTime<Utc> {
        self.fetched_at
    }
    /// Shanghai calendar date of fetching, not the response's trading date.
    pub fn observed_date(&self) -> chrono::NaiveDate {
        (self.fetched_at + chrono::Duration::hours(8)).date_naive()
    }
}

/// Raw payload is always retained; record order and duplicates are preserved.
#[derive(Debug, Clone, serde::Serialize)]
pub struct TradePage {
    pub(super) query: TradeQuery,
    pub(super) start: u16,
    pub(super) request_count: u16,
    pub(super) records: Vec<TradeRecord>,
    pub(super) historical_prefix_raw: Option<[u8; 4]>,
    pub(super) payload: Vec<u8>,
    pub(super) source: Option<TradeSource>,
}

impl TradePage {
    pub fn query(&self) -> &TradeQuery {
        &self.query
    }
    pub fn start(&self) -> u16 {
        self.start
    }
    pub fn request_count(&self) -> u16 {
        self.request_count
    }
    /// Raw response count, including auction snapshots and unknown states.
    pub fn raw_count(&self) -> usize {
        self.records.len()
    }
    pub fn records(&self) -> &[TradeRecord] {
        &self.records
    }
    pub fn historical_prefix_raw(&self) -> Option<[u8; 4]> {
        self.historical_prefix_raw
    }
    pub fn payload(&self) -> &[u8] {
        &self.payload
    }
    pub fn source(&self) -> Option<&TradeSource> {
        self.source.as_ref()
    }
    pub fn record_bytes(&self, index: usize) -> Option<&[u8]> {
        self.records
            .get(index)
            .and_then(|record| self.payload.get(record.byte_range()))
    }
    /// Wide offset; callers must checked-convert before the next request.
    /// An empty page keeps the offset unchanged and should terminate enumeration.
    pub fn next_offset(&self) -> u32 {
        u32::from(self.start) + self.records.len() as u32
    }
    pub fn confirmed_trades(&self) -> impl Iterator<Item = &TradeRecord> {
        self.records
            .iter()
            .filter(|record| record.is_confirmed_trade())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
#[non_exhaustive]
pub enum TradeTermination {
    EmptyPageObserved,
    OffsetLimit,
    PageLimit,
    RecordLimit,
    ByteLimit,
    Deadline,
    Cancelled,
    ConnectionChanged,
    DateChanged,
    DecodeError { message: String },
    TransportError { message: String },
    RepeatedPage,
    CallerStopped,
}

/// Coverage is derived by construction; no public setter can promote it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub enum TradeCoverage {
    /// Historical request enumerated from zero to an actual empty page on one
    /// known connection. Does not prove source retention or exchange coverage.
    SourceEnumerated,
    Partial,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[non_exhaustive]
pub enum TradeWarning {
    MissingSource,
    NonzeroStart,
    CurrentDayMayShift,
    TradingDateUnverified,
    LegacyClassificationCandidate,
    UnqualifiedEvents,
}

/// Counts are record counts, never sums of the count-like protocol slot.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub struct TradeCounts {
    pub raw: usize,
    pub auction_snapshots: usize,
    pub opening_matches: usize,
    pub trades: usize,
    pub unknown_events: usize,
    pub confirmed: usize,
    pub candidates: usize,
    pub excluded: usize,
    pub unknown_qualification: usize,
    pub confirmed_unknown_direction: usize,
}

/// Checked, immutable aggregation of already fetched pages. This model does
/// not fetch pages, deduplicate events, or assert independently checked coverage.
///
/// ```
/// use rustdx_complete::tcp::stock::{
///     TradeQuery, TradeCollection, TradeTermination, TradeCoverage, decode_trade_page,
/// };
/// let query = TradeQuery::history(0, "000001", 20261022)?;
/// let empty = decode_trade_page(&query, 0, 1800, &[0; 6])?;
/// let result = TradeCollection::new(query, vec![empty], TradeTermination::EmptyPageObserved)?;
/// // Offline bytes carry no source provenance, even with an empty terminal page.
/// assert_eq!(result.coverage(), TradeCoverage::Unknown);
/// # Ok::<(), std::io::Error>(())
/// ```
#[derive(Debug, Clone, serde::Serialize)]
pub struct TradeCollection {
    query: TradeQuery,
    pages: Vec<TradePage>,
    termination: TradeTermination,
    coverage: TradeCoverage,
    counts: TradeCounts,
    warnings: Vec<TradeWarning>,
    time_range: Option<(u16, u16)>,
    payload_bytes: usize,
}

impl TradeCollection {
    /// Rejects mixed queries/page sizes, skipped/overlapping offsets, pages after
    /// an empty page, changed known connections, and contradictory empty/offset
    /// termination. Missing provenance is allowed but lowers coverage.
    pub fn new(
        query: TradeQuery,
        pages: Vec<TradePage>,
        termination: TradeTermination,
    ) -> io::Result<Self> {
        let invalid = |message| io::Error::new(ErrorKind::InvalidInput, message);
        let mut known_source: Option<&TradeSource> = None;
        let mut missing_source = pages.is_empty();
        let mut next = pages.first().map_or(0, |page| u32::from(page.start));
        let mut empty_seen = false;
        let mut payload_bytes = 0usize;
        for page in &pages {
            if page.query != query {
                return Err(invalid("成交集合包含不同证券、日期或协议"));
            }
            if pages
                .first()
                .is_some_and(|first| first.request_count != page.request_count)
            {
                return Err(invalid("成交集合分页大小不一致"));
            }
            if empty_seen || u32::from(page.start) != next {
                return Err(invalid("成交集合偏移不连续或空页后仍追加"));
            }
            next = page.next_offset();
            empty_seen = page.raw_count() == 0;
            payload_bytes = payload_bytes
                .checked_add(page.payload.len())
                .ok_or_else(|| invalid("成交集合字节数溢出"))?;
            if let Some(source) = &page.source {
                if let Some(previous) = known_source {
                    if source.connection_id != previous.connection_id
                        || source.endpoint != previous.endpoint
                    {
                        return Err(invalid("成交集合跨连接；请终止旧集合并从零重启"));
                    }
                    if query.protocol == TradeProtocol::Today0fc5
                        && source.observed_date() != previous.observed_date()
                    {
                        return Err(invalid("当日成交集合跨本地自然日"));
                    }
                }
                known_source = Some(source);
            } else {
                missing_source = true;
            }
        }
        if (termination == TradeTermination::EmptyPageObserved) != empty_seen {
            return Err(invalid("空页终止原因须与实际末页一致"));
        }
        if termination == TradeTermination::OffsetLimit && next <= u32::from(u16::MAX) {
            return Err(invalid("OffsetLimit须有不可表示的下一页偏移"));
        }
        let mut warnings = Vec::new();
        if missing_source {
            warnings.push(TradeWarning::MissingSource);
        }
        let nonzero_start = pages.first().is_some_and(|page| page.start != 0);
        if nonzero_start {
            warnings.push(TradeWarning::NonzeroStart);
        }
        warnings.push(TradeWarning::TradingDateUnverified);
        if query.protocol == TradeProtocol::Today0fc5 {
            warnings.push(TradeWarning::CurrentDayMayShift);
        } else if query.protocol == TradeProtocol::LegacyHistory0fb5 {
            warnings.push(TradeWarning::LegacyClassificationCandidate);
        }
        let coverage = if termination != TradeTermination::EmptyPageObserved || nonzero_start {
            TradeCoverage::Partial
        } else if missing_source || query.date.is_none() {
            TradeCoverage::Unknown
        } else {
            TradeCoverage::SourceEnumerated
        };
        let mut counts = TradeCounts::default();
        let mut time_range: Option<(u16, u16)> = None;
        for record in pages.iter().flat_map(|page| &page.records) {
            counts.raw += 1;
            match record.event_kind {
                TradeEventKind::AuctionSnapshot => counts.auction_snapshots += 1,
                TradeEventKind::OpeningMatch => counts.opening_matches += 1,
                TradeEventKind::Trade => counts.trades += 1,
                TradeEventKind::Unknown => counts.unknown_events += 1,
            }
            match record.qualification {
                TradeQualification::ConfirmedByProfile => {
                    counts.confirmed += 1;
                    if record.direction == TradeDirection::Unknown {
                        counts.confirmed_unknown_direction += 1;
                    }
                }
                TradeQualification::Candidate => counts.candidates += 1,
                TradeQualification::ExcludedByProfile => counts.excluded += 1,
                TradeQualification::Unknown => counts.unknown_qualification += 1,
            }
            time_range = Some(
                time_range.map_or((record.time_minutes, record.time_minutes), |(min, max)| {
                    (min.min(record.time_minutes), max.max(record.time_minutes))
                }),
            );
        }
        if counts.candidates > 0 || counts.unknown_qualification > 0 {
            warnings.push(TradeWarning::UnqualifiedEvents);
        }
        Ok(Self {
            query,
            pages,
            termination,
            coverage,
            counts,
            warnings,
            time_range,
            payload_bytes,
        })
    }

    pub fn query(&self) -> &TradeQuery {
        &self.query
    }
    /// Position in this slice is the collection's page index.
    pub fn pages(&self) -> &[TradePage] {
        &self.pages
    }
    pub fn termination(&self) -> &TradeTermination {
        &self.termination
    }
    pub fn coverage(&self) -> TradeCoverage {
        self.coverage
    }
    pub fn counts(&self) -> TradeCounts {
        self.counts
    }
    pub fn warnings(&self) -> &[TradeWarning] {
        &self.warnings
    }
    pub fn time_range(&self) -> Option<(u16, u16)> {
        self.time_range
    }
    pub fn payload_bytes(&self) -> usize {
        self.payload_bytes
    }
    pub fn records(&self) -> impl Iterator<Item = &TradeRecord> {
        self.pages.iter().flat_map(|page| &page.records)
    }
    pub fn confirmed_trades(&self) -> impl Iterator<Item = &TradeRecord> {
        self.records().filter(|record| record.is_confirmed_trade())
    }
    /// Wide position for diagnostics; even 65536 must never wrap to zero.
    pub fn next_offset(&self) -> u32 {
        self.pages.last().map_or(0, TradePage::next_offset)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source(date: &str, endpoint: &str) -> TradeSource {
        TradeSource {
            endpoint: endpoint.parse().unwrap(),
            connection_id: NonZeroU64::new(1).unwrap(),
            fetched_at: date.parse().unwrap(),
        }
    }

    #[test]
    fn source_checks_do_not_merge_endpoints_or_local_calendar_days() {
        let query = TradeQuery::today(0, "000001").unwrap();
        let mut first =
            super::super::decode_trade_page(&query, 0, 1, &[1, 0, 0x58, 2, 1, 1, 1, 0, 0]).unwrap();
        first.source = Some(source("2026-10-22T15:59:59Z", "127.0.0.1:7709"));
        let mut empty = super::super::decode_trade_page(&query, 1, 1, &[0, 0]).unwrap();
        // Same UTC day, next Shanghai calendar day.
        empty.source = Some(source("2026-10-22T16:00:00Z", "127.0.0.1:7709"));
        assert!(
            TradeCollection::new(
                query.clone(),
                vec![first.clone(), empty.clone()],
                TradeTermination::EmptyPageObserved
            )
            .is_err()
        );
        empty.source = Some(source("2026-10-22T15:59:59Z", "127.0.0.2:7709"));
        assert!(
            TradeCollection::new(
                query,
                vec![first, empty],
                TradeTermination::EmptyPageObserved
            )
            .is_err()
        );
    }
}
