//! Fixed-layout 0x0537 minutes. Fields after the first point are relative to
//! the first point, not to the previous minute.
use std::{io, net::SocketAddr, ops::Range};

use super::{PrecisionSnapshot, PriceScale};
use crate::tcp::{Tcp, reader::Reader};
use serde::Serialize;

#[derive(Debug, Clone, Copy)]
pub struct TodayMinuteRequest<'a> {
    market: u16,
    code: &'a str,
}

impl<'a> TodayMinuteRequest<'a> {
    pub fn new(market: u16, code: &'a str) -> io::Result<Self> {
        super::requests::symbol(market, code)?;
        Ok(Self { market, code })
    }

    pub fn encode(self) -> [u8; 24] {
        let mut request = [0; 24];
        request[..12].copy_from_slice(&[0x0c, 1, 0, 0, 0, 1, 14, 0, 14, 0, 0x37, 5]);
        request[12..14].copy_from_slice(&self.market.to_le_bytes());
        request[14..20].copy_from_slice(self.code.as_bytes());
        request[23] = 0x93;
        request
    }

    pub fn recv(
        self,
        tcp: &mut Tcp,
        metadata: &PrecisionSnapshot,
    ) -> io::Result<TodayMinuteSeries> {
        if !metadata.is_fresh(self.market, chrono::Utc::now()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "StalePrecision: 请刷新分时目录精度",
            ));
        }
        metadata.scale(self.market, self.code)?;
        let bytes = crate::tcp::send_recv_decompress(tcp, &self.encode(), "当日分时0537")?;
        if !metadata.is_fresh(self.market, chrono::Utc::now()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "StalePrecision: 分时请求跨日，请重新预热",
            ));
        }
        let mut series = decode_today_minutes(&self, &bytes, metadata)?;
        series.source_endpoint = Some(tcp.get_ref().0.peer_addr()?);
        series.fetched_at = Some(chrono::Utc::now());
        Ok(series)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TodayMinutePoint {
    pub index: u16,
    /// Synthetic mainland session label: 09:31–11:30, 13:01–15:00.
    /// The wire carries neither time nor date.
    pub time_label: String,
    pub price: f64,
    pub average_price: f64,
    /// Raw minute quantity; no implicit conversion to shares.
    pub volume_raw: i32,
    pub price_field_raw: i32,
    pub average_field_raw: i32,
    pub price_raw: i64,
    pub average_raw: i64,
    pub byte_range: Range<usize>,
}

#[derive(Debug, Clone, Serialize)]
pub struct TodayMinuteSeries {
    pub market: u16,
    pub code: String,
    /// No server date is supplied by 0537.
    pub trading_date: Option<u32>,
    pub points: Vec<TodayMinutePoint>,
    pub reserved_raw: u16,
    pub scale: PriceScale,
    pub metadata_generation: u64,
    pub raw_payload: Vec<u8>,
    pub source_endpoint: Option<SocketAddr>,
    pub fetched_at: Option<chrono::DateTime<chrono::Utc>>,
}

pub fn decode_today_minutes(
    request: &TodayMinuteRequest<'_>,
    bytes: &[u8],
    metadata: &PrecisionSnapshot,
) -> io::Result<TodayMinuteSeries> {
    let scale = metadata.scale(request.market, request.code)?;
    let mut reader = Reader::new(bytes);
    let count = reader.u16()?;
    let reserved_raw = reader.u16()?;
    if count > 240 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "0537点数超出已支持的240分钟会话",
        ));
    }
    let mut points = Vec::with_capacity(usize::from(count));
    let mut base = (0i64, 0i64);
    for index in 0..count {
        crate::operation::check()?;
        let begin = bytes.len() - reader.remaining();
        let price_field_raw = reader.varint()?;
        let average_field_raw = reader.varint()?;
        let volume_raw = reader.varint()?;
        let price_raw = base.0 + i64::from(price_field_raw);
        let average_raw = base.1 + i64::from(average_field_raw);
        if index == 0 {
            base = (price_raw, average_raw);
        }
        if price_raw < 0 || average_raw < 0 || volume_raw < 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "0537价格、均价或数量为负",
            ));
        }
        let minute = if index < 120 {
            571 + index
        } else {
            781 + index - 120
        };
        points.push(TodayMinutePoint {
            index,
            time_label: format!("{:02}:{:02}", minute / 60, minute % 60),
            price: price_raw as f64 / f64::from(scale.divisor()),
            // Average field carries two more decimal places than the price.
            average_price: average_raw as f64 / (100.0 * f64::from(scale.divisor())),
            volume_raw,
            price_field_raw,
            average_field_raw,
            price_raw,
            average_raw,
            byte_range: begin..bytes.len() - reader.remaining(),
        });
    }
    if reader.remaining() != 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "0537响应包含多余字节",
        ));
    }
    Ok(TodayMinuteSeries {
        market: request.market,
        code: request.code.to_owned(),
        trading_date: None,
        points,
        reserved_raw,
        scale,
        metadata_generation: metadata.generation(),
        raw_payload: bytes.to_vec(),
        source_endpoint: None,
        fetched_at: None,
    })
}
