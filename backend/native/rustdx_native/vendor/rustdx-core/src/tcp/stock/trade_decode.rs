//! Shared strict decoder for the new event model and legacy CSV-compatible rows.
use std::{
    io::{self, ErrorKind},
    ops::Range,
};

use super::{TradePage, TradeProtocol, TradeQuery, TradeRecord};
use crate::tcp::reader::Reader;

pub(super) struct RawTradeRecord {
    pub time_minutes: u16,
    pub price_delta_raw: i32,
    pub price_raw: i64,
    pub volume_raw: i32,
    pub trade_count_raw: Option<i32>,
    pub status_raw: i32,
    pub tail_raw: i32,
    pub byte_range: Range<usize>,
}

/// Decode a decompressed single page. Source metadata is unknown for offline
/// payloads. Count and offset use the same constraints as the checked requests.
pub fn decode_trade_page(
    query: &TradeQuery,
    start: u16,
    count: u16,
    bytes: &[u8],
) -> io::Result<TradePage> {
    query.encode(start, count)?;
    let (records, historical_prefix_raw) = decode_records(bytes, count, query.protocol())?;
    Ok(TradePage {
        query: query.clone(),
        start,
        request_count: count,
        records,
        historical_prefix_raw,
        payload: bytes.to_vec(),
        source: None,
    })
}

type DecodedRecords = (Vec<TradeRecord>, Option<[u8; 4]>);

pub(super) fn decode_records(
    bytes: &[u8],
    requested: u16,
    protocol: TradeProtocol,
) -> io::Result<DecodedRecords> {
    let mut reader = Reader::new(bytes);
    let count = reader.u16()?;
    if count > requested {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "逐笔数量超过请求数量",
        ));
    }
    let legacy_history = protocol == TradeProtocol::LegacyHistory0fb5;
    let historical = protocol != TradeProtocol::Today0fc5;
    let prefix = if historical {
        let mut value = [0; 4];
        value.copy_from_slice(reader.take(4)?);
        if protocol == TradeProtocol::EnhancedHistory0fc6 && !f32::from_le_bytes(value).is_finite()
        {
            return Err(io::Error::new(ErrorKind::InvalidData, "0fc6参考价非有限值"));
        }
        Some(value)
    } else {
        None
    };
    let mut records = Vec::with_capacity(usize::from(count));
    let mut price_raw = 0i64;
    for _ in 0..count {
        crate::operation::check()?;
        let begin = bytes.len() - reader.remaining();
        let time_minutes = reader.u16()?;
        let price_delta_raw = reader.varint()?;
        price_raw = price_raw
            .checked_add(i64::from(price_delta_raw))
            .ok_or_else(|| io::Error::new(ErrorKind::InvalidData, "逐笔累计价格溢出"))?;
        let volume_raw = reader.varint()?;
        let trade_count_raw = if legacy_history {
            None
        } else {
            Some(reader.varint()?)
        };
        let status_raw = reader.varint()?;
        let tail_raw = reader.varint()?;
        if time_minutes >= 1440
            || price_raw < 0
            || (price_raw == 0 && protocol != TradeProtocol::EnhancedHistory0fc6)
            || volume_raw < 0
            || trade_count_raw.is_some_and(|count| count < 0)
        {
            return Err(io::Error::new(
                ErrorKind::InvalidData,
                "逐笔时间、价格、数量或计数槽非法",
            ));
        }
        records.push(TradeRecord::from_raw(
            RawTradeRecord {
                time_minutes,
                price_delta_raw,
                price_raw,
                volume_raw,
                trade_count_raw,
                status_raw,
                tail_raw,
                byte_range: begin..bytes.len() - reader.remaining(),
            },
            protocol,
        ));
    }
    if reader.remaining() != 0 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "协议响应包含多余字节",
        ));
    }
    Ok((records, prefix))
}
