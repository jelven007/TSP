//! Immutable, checked requests and separate stock/index response decoders.
use std::io::{self, ErrorKind};

use super::{IndexKlineData, Kline, KlineData};
use crate::tcp::{
    Tcp, Tdx,
    helper::{DateTime, vol_amount},
    reader::Reader,
};

/// Checked K-line parameters. Encoding always uses these private fields.
///
/// Stock and index requests share command 0x052d; choose the response decoder
/// explicitly because index records include four additional bytes.
///
/// ```
/// use rustdx_complete::tcp::stock::KlineRequest;
/// let request = KlineRequest::new(1, "000001", 9, 0, 3).unwrap();
/// assert_eq!(&request.encode()[12..14], &[1, 0]);
/// assert!(KlineRequest::new(1, "000001", 9, 0, 801).is_err());
/// ```
///
/// ```compile_fail
/// use rustdx_complete::tcp::stock::KlineRequest;
/// let mut request = KlineRequest::new(1, "000001", 9, 0, 3).unwrap();
/// request.count = 801;
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KlineRequest<'a> {
    market: u16,
    code: &'a str,
    category: u16,
    start: u16,
    count: u16,
}

impl<'a> KlineRequest<'a> {
    /// Markets 0/1, six ASCII alphanumeric characters, categories 0–11,
    /// and 1–800 records are supported. No market is inferred from the code.
    pub fn new(
        market: u16,
        code: &'a str,
        category: u16,
        start: u16,
        count: u16,
    ) -> io::Result<Self> {
        if market > 1
            || code.len() != 6
            || !code.bytes().all(|b| b.is_ascii_alphanumeric())
            || category > 11
            || !(1..=800).contains(&count)
        {
            return Err(io::Error::new(
                ErrorKind::InvalidInput,
                "市场、六位ASCII代码、周期或K线数量(1–800)非法",
            ));
        }
        Ok(Self {
            market,
            code,
            category,
            start,
            count,
        })
    }

    pub fn market(self) -> u16 {
        self.market
    }
    pub fn code(self) -> &'a str {
        self.code
    }
    pub fn category(self) -> u16 {
        self.category
    }
    pub fn start(self) -> u16 {
        self.start
    }
    pub fn count(self) -> u16 {
        self.count
    }

    /// Explicit market and period; no code inference or source-unit inference.
    pub fn for_symbol(
        symbol: &'a crate::data::Symbol,
        period: crate::data::Period,
        start: u16,
        count: u16,
    ) -> io::Result<Self> {
        let market = symbol
            .market()
            .tdx_market()
            .map_err(|e| io::Error::new(ErrorKind::InvalidInput, e))?;
        Self::new(market, symbol.code(), period.tdx_category(), start, count)
    }

    /// Standard raw stock bars. Period is derived from this request, so callers
    /// cannot accidentally label minute data as daily data. Volume must be
    /// supplied explicitly because its source unit depends on the instrument.
    pub fn decode_canonical_stock(
        self,
        bytes: &[u8],
        unit: crate::data::VolumeUnit,
    ) -> io::Result<Vec<crate::data::CanonicalBar>> {
        decode_stock_bars(&self, bytes)?
            .iter()
            .map(|bar| {
                crate::data::CanonicalBar::from_kline_request(
                    bar,
                    &self,
                    unit,
                    crate::data::VolumeUnitBasis::CallerSpecified,
                    crate::data::PriceBasis::Raw,
                )
                .map_err(|e| io::Error::new(ErrorKind::InvalidData, e))
            })
            .collect()
    }

    pub fn recv_canonical_stock(
        self,
        tcp: &mut Tcp,
        unit: crate::data::VolumeUnit,
    ) -> io::Result<Vec<crate::data::CanonicalBar>> {
        let bytes = crate::tcp::send_recv_decompress(tcp, &self.encode(), "标准股票K线")?;
        let mut bars = self.decode_canonical_stock(&bytes, unit)?;
        self.attach_acquisition(tcp, &mut bars)?;
        Ok(bars)
    }

    /// Standard index OHLCV. Use decode_index_bars when advancing/declining
    /// security counts are also needed; CanonicalBar has no breadth fields.
    pub fn decode_canonical_index(
        self,
        bytes: &[u8],
        unit: crate::data::VolumeUnit,
    ) -> io::Result<Vec<crate::data::CanonicalBar>> {
        decode_index_bars(&self, bytes)?
            .iter()
            .map(|bar| {
                crate::data::CanonicalBar::from_index_kline_request(
                    bar,
                    &self,
                    unit,
                    crate::data::VolumeUnitBasis::CallerSpecified,
                    crate::data::PriceBasis::Raw,
                )
                .map_err(|e| io::Error::new(ErrorKind::InvalidData, e))
            })
            .collect()
    }

    pub fn recv_canonical_index(
        self,
        tcp: &mut Tcp,
        unit: crate::data::VolumeUnit,
    ) -> io::Result<Vec<crate::data::CanonicalBar>> {
        let bytes = crate::tcp::send_recv_decompress(tcp, &self.encode(), "标准指数K线")?;
        let mut bars = self.decode_canonical_index(&bytes, unit)?;
        self.attach_acquisition(tcp, &mut bars)?;
        Ok(bars)
    }

    fn attach_acquisition(
        self,
        tcp: &Tcp,
        bars: &mut [crate::data::CanonicalBar],
    ) -> io::Result<()> {
        let acquisition = crate::data::BarAcquisition {
            endpoint: tcp.get_ref().0.peer_addr()?,
            connection_id: tcp.connection_id().get(),
            connection_generation: tcp.connection_generation(),
            received_at: chrono::Utc::now(),
        };
        for bar in bars {
            bar.provenance.acquisition = Some(acquisition.clone());
        }
        Ok(())
    }

    /// The 38-byte pytdx `<HIHHHH6sHHHHIIH` request.
    pub fn encode(self) -> [u8; 38] {
        let mut bytes = [0; 38];
        bytes.copy_from_slice(Kline::SEND);
        bytes[12..14].copy_from_slice(&self.market.to_le_bytes());
        bytes[14..20].copy_from_slice(self.code.as_bytes());
        bytes[20..22].copy_from_slice(&self.category.to_le_bytes());
        bytes[24..26].copy_from_slice(&self.start.to_le_bytes());
        bytes[26..28].copy_from_slice(&self.count.to_le_bytes());
        bytes
    }

    pub fn recv_stock(self, tcp: &mut Tcp) -> io::Result<Vec<KlineData<'a>>> {
        let bytes = crate::tcp::send_recv_decompress(tcp, &self.encode(), "股票K线")?;
        decode_stock_bars(&self, &bytes)
    }

    pub fn recv_index(self, tcp: &mut Tcp) -> io::Result<Vec<IndexKlineData<'a>>> {
        let bytes = crate::tcp::send_recv_decompress(tcp, &self.encode(), "指数K线")?;
        decode_index_bars(&self, &bytes)
    }
}

/// Decode stock bars without retaining response bytes or mutating a request.
/// Rejects truncation, excess counts, trailing bytes and invalid calendar/time
/// fields. Results are sorted by time, preserving the legacy ordering contract.
pub fn decode_stock_bars<'a>(
    request: &KlineRequest<'a>,
    bytes: &[u8],
) -> io::Result<Vec<KlineData<'a>>> {
    Ok(decode_stock_bars_raw(request, bytes)?
        .into_iter()
        .map(|bar| bar.bar)
        .collect())
}

/// Shared parsing keeps wire positions alongside the compatibility DTO.
pub(super) struct RawStockBar<'a> {
    pub bar: KlineData<'a>,
    pub raw_volume_u32: u32,
    pub raw_amount_u32: u32,
    pub byte_range: std::ops::Range<usize>,
    pub wire_index: usize,
}

pub(super) fn decode_stock_bars_raw<'a>(
    request: &KlineRequest<'a>,
    bytes: &[u8],
) -> io::Result<Vec<RawStockBar<'a>>> {
    let mut reader = Reader::new(bytes);
    let count = response_count(&mut reader, request.count)?;
    let mut bars = Vec::with_capacity(count);
    let mut base = 0;
    for wire_index in 0..count {
        crate::operation::check()?;
        let mut bar = read_bar(&mut reader, request, &mut base)?;
        bar.wire_index = wire_index;
        bars.push(bar);
    }
    finish(&reader)?;
    bars.sort_by_key(|bar| bar.bar.dt);
    Ok(bars)
}

/// Decode index bars, including the u16 advancing/declining security counts.
/// Its record layout must not be used for stock responses.
pub fn decode_index_bars<'a>(
    request: &KlineRequest<'a>,
    bytes: &[u8],
) -> io::Result<Vec<IndexKlineData<'a>>> {
    let mut reader = Reader::new(bytes);
    let count = response_count(&mut reader, request.count)?;
    let mut bars = Vec::with_capacity(count);
    let mut base = 0;
    for _ in 0..count {
        crate::operation::check()?;
        let bar = read_bar(&mut reader, request, &mut base)?.bar;
        bars.push(IndexKlineData {
            dt: bar.dt,
            code: bar.code,
            open: bar.open,
            close: bar.close,
            high: bar.high,
            low: bar.low,
            vol: bar.vol,
            amount: bar.amount,
            up_count: reader.u16()?,
            down_count: reader.u16()?,
        });
    }
    finish(&reader)?;
    bars.sort_by_key(|bar| bar.dt);
    Ok(bars)
}

fn response_count(reader: &mut Reader<'_>, requested: u16) -> io::Result<usize> {
    let count = reader.u16()?;
    if count > requested {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "K线数量超过请求数量",
        ));
    }
    Ok(count as usize)
}

fn finish(reader: &Reader<'_>) -> io::Result<()> {
    if reader.remaining() != 0 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "K线响应包含多余字节或解码类型不匹配",
        ));
    }
    Ok(())
}

fn read_datetime(reader: &mut Reader<'_>, category: u16) -> io::Result<DateTime> {
    let (year, month, day, minutes) = if matches!(category, 0..=3 | 7 | 8) {
        let packed = reader.u16()? as u32;
        (
            (packed >> 11) + 2004,
            packed % 2048 / 100,
            packed % 2048 % 100,
            reader.u16()? as u32,
        )
    } else {
        let packed = reader.u32()?;
        (packed / 10000, packed % 10000 / 100, packed % 100, 900)
    };
    // Validate before narrowing or converting through YYYYMMDD: invalid month
    // or day fields must never normalize into a different, valid date.
    if !(1..=9999).contains(&year)
        || chrono::NaiveDate::from_ymd_opt(year as i32, month, day).is_none()
        || minutes >= 24 * 60
    {
        return Err(io::Error::new(ErrorKind::InvalidData, "K线日期或时分非法"));
    }
    Ok(DateTime {
        year: year as u16,
        month: month as u16,
        day: day as u16,
        hour: (minutes / 60) as u16,
        minute: (minutes % 60) as u16,
    })
}

fn read_bar<'a>(
    reader: &mut Reader<'_>,
    request: &KlineRequest<'a>,
    base: &mut i64,
) -> io::Result<RawStockBar<'a>> {
    let start = reader.position();
    let dt = read_datetime(reader, request.category)?;
    let open = i64::from(reader.varint()?);
    let close = i64::from(reader.varint()?);
    let high = i64::from(reader.varint()?);
    let low = i64::from(reader.varint()?);
    // At most 800 i32 deltas: all intermediate sums fit in i64.
    *base += open;
    let raw_volume_u32 = reader.u32()?;
    let raw_amount_u32 = reader.u32()?;
    let bar = KlineData {
        dt,
        code: request.code,
        open: *base as f64 / 1000.0,
        close: (*base + close) as f64 / 1000.0,
        high: (*base + high) as f64 / 1000.0,
        low: (*base + low) as f64 / 1000.0,
        vol: vol_amount(raw_volume_u32 as i32),
        amount: super::reported_turnover::decode_nonnegative_binary32(raw_amount_u32)?,
    };
    crate::data::validate_bar_values(bar.open, bar.high, bar.low, bar.close, bar.vol, bar.amount)
        .map_err(|e| io::Error::new(ErrorKind::InvalidData, e))?;
    *base += close;
    Ok(RawStockBar {
        bar,
        raw_volume_u32,
        raw_amount_u32,
        byte_range: start..reader.position(),
        wire_index: 0,
    })
}
