//! 051b parallel auxiliary series. Indices have no server-supplied timestamps.
use super::command_request::*;
use crate::tcp::{Tcp, reader::Reader};
use serde::Serialize;
use std::io;

super::client::batch_budget::impl_heap!(IntradayAuxRequest; symbol);

pub const INTRADAY_AUX_COMMAND: u16 = 0x051b;
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum IntradayAuxKind {
    BuySellStrength,
    VolumeComparison,
    Raw(u8),
}
impl IntradayAuxKind {
    pub fn selector(self) -> u8 {
        match self {
            Self::BuySellStrength => 0,
            Self::VolumeComparison => 11,
            Self::Raw(v) => v,
        }
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct IntradayAuxRequest {
    symbol: CommandSymbol,
    kind: IntradayAuxKind,
}
impl IntradayAuxRequest {
    pub fn new(market: u16, code: &str, kind: IntradayAuxKind) -> io::Result<Self> {
        Ok(Self {
            symbol: CommandSymbol::new(market, code)?,
            kind,
        })
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = Vec::with_capacity(28);
        self.symbol.encode(&mut body, true);
        body.resize(27, 0);
        body.push(self.kind.selector());
        crate::tcp::frame::request(INTRADAY_AUX_COMMAND, id, &body)
    }
    pub fn recv(&self, tcp: &mut Tcp) -> io::Result<IntradayAuxSeries> {
        let bytes = tcp.exchange_checked(&self.encode(crate::tcp::frame::sequence())?)?;
        let mut result = decode_intraday_aux(self, &bytes)?;
        result.page.source.attach(tcp)?;
        Ok(result)
    }
}
#[derive(Debug, Clone, Serialize, PartialEq)]
pub enum IntradayAuxPoint {
    IntegerPair {
        index: u16,
        series_a: i64,
        series_b: i64,
    },
    VolumePair {
        index: u16,
        previous_day: f32,
        current_day: f32,
        previous_bits: u32,
        current_bits: u32,
    },
}
#[derive(Debug, Clone, Serialize)]
pub struct IntradayAuxSeries {
    pub request: IntradayAuxRequest,
    pub page: CommandPage<IntradayAuxPoint>,
}
pub fn decode_intraday_aux(
    request: &IntradayAuxRequest,
    bytes: &[u8],
) -> io::Result<IntradayAuxSeries> {
    bounded(bytes)?;
    let mut r = Reader::new(bytes);
    let floating = request.kind.selector() == 11;
    let n = count(&mut r, if floating { 8 } else { 2 }, 32766)?;
    let mut points = Vec::with_capacity(n);
    for index in 0..n {
        points.push(if floating {
            let previous_day = r.f32()?;
            let current_day = r.f32()?;
            IntradayAuxPoint::VolumePair {
                index: index as u16,
                previous_day,
                current_day,
                previous_bits: previous_day.to_bits(),
                current_bits: current_day.to_bits(),
            }
        } else {
            IntradayAuxPoint::IntegerPair {
                index: index as u16,
                series_a: r.varint64()?,
                series_b: r.varint64()?,
            }
        });
    }
    exact(&r)?;
    Ok(IntradayAuxSeries {
        request: request.clone(),
        page: CommandPage::new(INTRADAY_AUX_COMMAND, points, bytes),
    })
}
