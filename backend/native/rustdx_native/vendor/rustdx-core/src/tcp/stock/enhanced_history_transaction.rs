//! Enhanced historical mixed events (0x0fc6), separate from legacy 0x0fb5.
use chrono::{DateTime, NaiveDateTime, Utc};
use serde::Serialize;
use std::{
    io::{self, ErrorKind},
    net::SocketAddr,
};

use crate::tcp::{Tcp, reader::Reader};

/// eltdx 3.2.2's enhanced historical trade command.
pub const ENHANCED_HISTORY_TRANSACTION_COMMAND: u16 = 0x0fc6;
/// The pinned eltdx 3.2.2 profile supports 1–1800 mixed events per page.
pub const MAX_ENHANCED_HISTORY_TRANSACTION_COUNT: u16 = 1800;

/// Immutable, checked single-page request for 0x0fc6.
///
/// `date` is a calendar date in YYYYMMDD form; validation does not establish
/// that it was a trading day or that the server retains data for that day.
/// `start=0` addresses the latest end; larger offsets address earlier events.
/// The offset counts all raw events, including auction snapshots.
///
/// ```
/// use rustdx_complete::tcp::stock::EnhancedHistoryTransactionRequest;
/// let request = EnhancedHistoryTransactionRequest::new(0, "000001", 0, 1800, 20261022)?;
/// assert_eq!(&request.encode()[10..12], &[0xc6, 0x0f]);
/// assert!(EnhancedHistoryTransactionRequest::new(0, "000001", 0, 1801, 20261022).is_err());
/// # Ok::<(), std::io::Error>(())
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EnhancedHistoryTransactionRequest<'a> {
    market: u16,
    code: &'a str,
    start: u16,
    count: u16,
    date: u32,
}

impl<'a> EnhancedHistoryTransactionRequest<'a> {
    /// Markets 0/1 and six ASCII alphanumeric code bytes, without inference.
    pub fn new(market: u16, code: &'a str, start: u16, count: u16, date: u32) -> io::Result<Self> {
        super::requests::symbol(market, code)?;
        super::requests::checked_date(date, ErrorKind::InvalidInput)?;
        if !(1..=MAX_ENHANCED_HISTORY_TRANSACTION_COUNT).contains(&count) {
            return Err(io::Error::new(
                ErrorKind::InvalidInput,
                "增强历史成交数量须为1–1800",
            ));
        }
        Ok(Self {
            market,
            code,
            start,
            count,
            date,
        })
    }

    pub fn market(self) -> u16 {
        self.market
    }
    pub fn code(self) -> &'a str {
        self.code
    }
    pub fn start(self) -> u16 {
        self.start
    }
    pub fn count(self) -> u16 {
        self.count
    }
    pub fn date(self) -> u32 {
        self.date
    }

    /// Header (message id 1) + date(u32)/market(u16)/code(6)/start(u16)/count(u16).
    pub fn encode(self) -> [u8; 28] {
        let mut bytes = [0; 28];
        bytes[..10].copy_from_slice(&[0x0c, 1, 0, 0, 0, 1, 0x12, 0, 0x12, 0]);
        bytes[10..12].copy_from_slice(&ENHANCED_HISTORY_TRANSACTION_COMMAND.to_le_bytes());
        bytes[12..16].copy_from_slice(&self.date.to_le_bytes());
        bytes[16..18].copy_from_slice(&self.market.to_le_bytes());
        bytes[18..24].copy_from_slice(self.code.as_bytes());
        bytes[24..26].copy_from_slice(&self.start.to_le_bytes());
        bytes[26..28].copy_from_slice(&self.count.to_le_bytes());
        bytes
    }

    /// One synchronous page, using the existing deadline/retry/observer policy.
    /// An empty page is returned as observed; there is no legacy fallback.
    pub fn recv(self, tcp: &mut Tcp) -> io::Result<EnhancedHistoryTransactionPage> {
        tcp.with_timeout(crate::tcp::network_policy().request_timeout, |tcp| {
            let bytes = crate::tcp::send_recv_decompress(tcp, &self.encode(), "增强历史成交0fc6")?;
            let mut page = decode_enhanced_history_transactions(&self, &bytes)?;
            page.source_endpoint = Some(tcp.get_ref().0.peer_addr()?);
            page.fetched_at = Some(Utc::now());
            Ok(page)
        })
    }
}

/// Direction names in the pinned SDK profile. Other statuses remain explicit.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum EnhancedTradeSide {
    Buy,
    Sell,
    Neutral,
    Status(i32),
}

/// eltdx 3.2.2's mixed-event classification, not exchange-level verification.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum EnhancedTradeEventKind {
    Trade,
    OpeningMatch,
    AuctionSnapshot,
}

/// One enhanced historical event. No field is a stable exchange trade ID.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct EnhancedHistoryTransactionData {
    /// Wire order within this page; not a trade identifier.
    pub index: u16,
    /// Request offset + page index; not stable across requests or servers.
    pub absolute_index: u32,
    /// Minute of day, 0–1439.
    pub time_minutes: u16,
    /// HH:MM, without seconds.
    pub time: String,
    /// Constructed from the request date, without a wire date or timezone.
    pub trade_datetime: NaiveDateTime,
    /// Page-local accumulated price / 100, following the pinned SDK profile.
    pub price: f64,
    pub price_delta_raw: i32,
    pub price_acc_raw: i64,
    /// Raw quantity; instrument-specific unit must be established separately.
    pub volume: i32,
    /// Raw count slot, also named `order_count` by eltdx; not a serial number.
    pub order_count: i32,
    /// Direction/status wire value, including 5, 8 and unknown values.
    pub status_raw: i32,
    /// The final varint, usually zero; unknown nonzero values are preserved.
    pub reserved_raw: i32,
}

impl EnhancedHistoryTransactionData {
    pub fn side(&self) -> EnhancedTradeSide {
        match self.status_raw {
            0 => EnhancedTradeSide::Buy,
            1 => EnhancedTradeSide::Sell,
            2 => EnhancedTradeSide::Neutral,
            value => EnhancedTradeSide::Status(value),
        }
    }

    /// Status 8 takes priority even at 09:25.
    pub fn event_kind(&self) -> EnhancedTradeEventKind {
        if self.status_raw == 8 {
            EnhancedTradeEventKind::AuctionSnapshot
        } else if self.time_minutes == 9 * 60 + 25 {
            EnhancedTradeEventKind::OpeningMatch
        } else {
            EnhancedTradeEventKind::Trade
        }
    }

    /// SDK-compatible view: exclude status 8. Unknown statuses remain visible;
    /// this predicate alone does not verify their business meaning.
    pub fn is_actual_trade(&self) -> bool {
        self.status_raw != 8
    }

    pub fn is_after_hours_fixed_price(&self) -> bool {
        self.status_raw == 5 && (15 * 60 + 5..=15 * 60 + 30).contains(&self.time_minutes)
    }
}

/// A decoded page, with all raw events and request-derived identity/date.
/// No total count, date echo or completeness guarantee is supplied by 0x0fc6.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct EnhancedHistoryTransactionPage {
    pub market: u16,
    pub code: String,
    /// Requested YYYYMMDD; not confirmed by a date field in the response.
    pub date: u32,
    pub start: u16,
    pub request_count: u16,
    /// All mixed events in wire order, before any filtering.
    pub records: Vec<EnhancedHistoryTransactionData>,
    /// Historical header slot. Preserved, never added to the price accumulator.
    pub price_base_raw_f32: f32,
    pub price_base_raw_bytes: [u8; 4],
    pub raw_payload: Vec<u8>,
    /// None for offline decoding.
    pub source_endpoint: Option<SocketAddr>,
    /// None for offline decoding; UTC capture time for recv().
    pub fetched_at: Option<DateTime<Utc>>,
}

impl EnhancedHistoryTransactionPage {
    pub fn raw_count(&self) -> usize {
        self.records.len()
    }

    pub fn actual_trades(&self) -> impl Iterator<Item = &EnhancedHistoryTransactionData> {
        self.records
            .iter()
            .filter(|record| record.is_actual_trade())
    }

    pub fn auction_snapshots(&self) -> impl Iterator<Item = &EnhancedHistoryTransactionData> {
        self.records
            .iter()
            .filter(|record| record.event_kind() == EnhancedTradeEventKind::AuctionSnapshot)
    }

    pub fn opening_matches(&self) -> impl Iterator<Item = &EnhancedHistoryTransactionData> {
        self.records
            .iter()
            .filter(|record| record.event_kind() == EnhancedTradeEventKind::OpeningMatch)
    }

    pub fn after_hours_trades(&self) -> impl Iterator<Item = &EnhancedHistoryTransactionData> {
        self.records
            .iter()
            .filter(|record| record.is_after_hours_fixed_price())
    }

    /// Advance by raw count, including snapshots. A short nonempty page still
    /// yields a next offset. None means only that this response was empty.
    /// Offset exhaustion is an error, never an empty-page/completion signal.
    pub fn next_start(&self) -> io::Result<Option<u16>> {
        if self.records.is_empty() {
            return Ok(None);
        }
        let next = usize::from(self.start)
            .checked_add(self.raw_count())
            .and_then(|value| u16::try_from(value).ok())
            .ok_or_else(|| io::Error::new(ErrorKind::InvalidInput, "增强历史成交偏移超出u16"))?;
        Ok(Some(next))
    }
}

/// Decode only a decompressed 0x0fc6 payload:
/// count(u16), reference(f32), then time(u16) and five signed varints
/// (price delta, volume, order count, status, reserved).
///
/// The price accumulator starts at zero on every page. The reference slot
/// is retained separately. Zero prices are preserved, including snapshots.
/// The result is atomic: truncated, excessive, invalid or trailing data fails
/// the whole page. Payloads are bounded by the wire's u16 length.
pub fn decode_enhanced_history_transactions(
    request: &EnhancedHistoryTransactionRequest<'_>,
    bytes: &[u8],
) -> io::Result<EnhancedHistoryTransactionPage> {
    crate::operation::check()?;
    if bytes.len() > usize::from(u16::MAX) {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "增强历史成交响应过大",
        ));
    }
    let date = crate::data::date_from_u32(request.date)
        .map_err(|error| io::Error::new(ErrorKind::InvalidInput, error))?;
    let mut reader = Reader::new(bytes);
    let count = reader.u16()?;
    if count > request.count {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "增强历史成交数量超过请求数量",
        ));
    }
    let price_base_raw_bytes = reader.u32()?.to_le_bytes();
    let price_base_raw_f32 = f32::from_le_bytes(price_base_raw_bytes);
    if !price_base_raw_f32.is_finite() {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "增强历史成交参考价非有限值",
        ));
    }
    // A record needs two time bytes plus at least five one-byte varints.
    if usize::from(count) > reader.remaining() / 7 {
        return Err(io::Error::new(
            ErrorKind::UnexpectedEof,
            "增强历史成交响应不足以容纳宣告条数",
        ));
    }
    let mut records = Vec::with_capacity(usize::from(count));
    let mut price_acc_raw = 0i64;
    for index in 0..count {
        crate::operation::check()?;
        let time_minutes = reader.u16()?;
        let price_delta_raw = reader.varint()?;
        let volume = reader.varint()?;
        let order_count = reader.varint()?;
        let status_raw = reader.varint()?;
        let reserved_raw = reader.varint()?;
        price_acc_raw = price_acc_raw
            .checked_add(i64::from(price_delta_raw))
            .ok_or_else(|| io::Error::new(ErrorKind::InvalidData, "增强历史成交价格溢出"))?;
        if time_minutes >= 1440 || price_acc_raw < 0 || volume < 0 || order_count < 0 {
            return Err(io::Error::new(
                ErrorKind::InvalidData,
                "增强历史成交时间、价格、数量或笔数非法",
            ));
        }
        let trade_datetime = date
            .and_hms_opt(
                u32::from(time_minutes / 60),
                u32::from(time_minutes % 60),
                0,
            )
            .ok_or_else(|| io::Error::new(ErrorKind::InvalidData, "增强历史成交时间非法"))?;
        records.push(EnhancedHistoryTransactionData {
            index,
            absolute_index: u32::from(request.start) + u32::from(index),
            time_minutes,
            time: format!("{:02}:{:02}", time_minutes / 60, time_minutes % 60),
            trade_datetime,
            price: price_acc_raw as f64 / 100.0,
            price_delta_raw,
            price_acc_raw,
            volume,
            order_count,
            status_raw,
            reserved_raw,
        });
    }
    if reader.remaining() != 0 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "增强历史成交响应包含多余字节",
        ));
    }
    crate::operation::check()?;
    Ok(EnhancedHistoryTransactionPage {
        market: request.market,
        code: request.code.to_owned(),
        date: request.date,
        start: request.start,
        request_count: request.count,
        records,
        price_base_raw_f32,
        price_base_raw_bytes,
        raw_payload: bytes.to_vec(),
        source_endpoint: None,
        fetched_at: None,
    })
}
