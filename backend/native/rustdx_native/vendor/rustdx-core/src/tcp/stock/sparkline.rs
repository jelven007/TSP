use super::command_request::*;
use crate::tcp::{Tcp, reader::Reader};
use serde::Serialize;
use std::io;

super::client::batch_budget::impl_heap!(SparklineRequest; symbol);

pub const SPARKLINE_COMMAND: u16 = 0x0fd1;
#[derive(Debug, Clone, Copy)]
pub struct SparklineOptions {
    pub selector: u8,
    pub window: u16,
}
impl Default for SparklineOptions {
    fn default() -> Self {
        Self {
            selector: 1,
            window: 20,
        }
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct SparklineRequest {
    symbol: CommandSymbol,
    selector: u8,
    window: u16,
}
impl SparklineRequest {
    pub fn new(market: u16, code: &str, options: SparklineOptions) -> io::Result<Self> {
        if options.window == 0 {
            return Err(input("小图window须大于0"));
        }
        Ok(Self {
            symbol: CommandSymbol::new(market, code)?,
            selector: options.selector,
            window: options.window,
        })
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = Vec::with_capacity(37);
        self.symbol.encode(&mut body, true);
        body.resize(24, 0);
        body.extend_from_slice(&[self.selector, 0]);
        body.extend_from_slice(&self.window.to_le_bytes());
        body.extend_from_slice(&0x0100_0000u32.to_le_bytes());
        body.resize(37, 0);
        crate::tcp::frame::request(SPARKLINE_COMMAND, id, &body)
    }
    pub fn recv(&self, tcp: &mut Tcp) -> io::Result<SparklineSeries> {
        let bytes = tcp.exchange_checked(&self.encode(crate::tcp::frame::sequence())?)?;
        let mut result = decode_sparkline(self, &bytes)?;
        result.page.source.attach(tcp)?;
        Ok(result)
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct SparklinePrice {
    pub price: f32,
    pub bits: u32,
}
#[derive(Debug, Clone, Serialize)]
pub struct SparklineSeries {
    pub request: SparklineRequest,
    pub market_raw: u8,
    pub code: String,
    pub selector_echo: u8,
    pub parameter_raw: u32,
    pub capacity_raw: u16,
    pub base_price: f32,
    pub base_bits: u32,
    /// Prices have no supplied timestamps.
    pub page: CommandPage<SparklinePrice>,
}
pub fn decode_sparkline(request: &SparklineRequest, bytes: &[u8]) -> io::Result<SparklineSeries> {
    bounded(bytes)?;
    let mut r = Reader::new(bytes);
    let market_raw = r.u8()?;
    r.skip(1)?;
    let code = r.code()?;
    r.skip(16)?;
    let selector_echo = r.u8()?;
    r.skip(1)?;
    let parameter_raw = r.u32()?;
    r.skip(4)?;
    let capacity_raw = r.u16()?;
    let base_price = r.f32()?;
    let n = count(&mut r, 4, 16373)?;
    if u16::from(market_raw) != request.symbol.market()
        || code != request.symbol.code()
        || selector_echo != request.selector
        || (capacity_raw != 0 && n > usize::from(capacity_raw))
    {
        return Err(invalid("0fd1证券/selector/容量错配"));
    }
    let mut prices = Vec::with_capacity(n);
    for _ in 0..n {
        let price = r.f32()?;
        prices.push(SparklinePrice {
            price,
            bits: price.to_bits(),
        });
    }
    exact(&r)?;
    Ok(SparklineSeries {
        request: request.clone(),
        market_raw,
        code,
        selector_echo,
        parameter_raw,
        capacity_raw,
        base_price,
        base_bits: base_price.to_bits(),
        page: CommandPage::new(SPARKLINE_COMMAND, prices, bytes),
    })
}
