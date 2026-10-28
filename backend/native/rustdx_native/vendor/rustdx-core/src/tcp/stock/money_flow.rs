//! 0ffc: dedicated routing and two independent sets of four amount buckets.
use super::command_request::*;
use crate::tcp::{Tcp, reader::Reader};
use serde::Serialize;
use std::{collections::BTreeSet, io};

super::client::batch_budget::impl_heap!(MoneyFlowRequest; symbol);

pub const MONEY_FLOW_COMMAND: u16 = 0x0ffc;
#[derive(Debug, Clone, Serialize)]
pub struct MoneyFlowRequest {
    symbol: CommandSymbol,
}
impl MoneyFlowRequest {
    pub fn new(market: u16, code: &str) -> io::Result<Self> {
        Ok(Self {
            symbol: CommandSymbol::new(market, code)?,
        })
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = Vec::with_capacity(38);
        self.symbol.encode(&mut body, true);
        body.resize(38, 0);
        crate::tcp::frame::request(MONEY_FLOW_COMMAND, (id & 0xff) | 0x007e_2d00, &body)
    }
    pub fn recv(&self, tcp: &mut Tcp) -> io::Result<MoneyFlowBlock> {
        let bytes = tcp.exchange_checked(&self.encode(crate::tcp::frame::sequence())?)?;
        let mut block = decode_money_flow(self, &bytes)?;
        block.page.source.attach(tcp)?;
        Ok(block)
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct MoneyFlowDaily {
    pub date_raw: u32,
    pub raw: [u32; 21],
    pub total_amount: f32,
    pub total_amount_bits: u32,
    pub buckets_raw: [u16; 16],
    /// Super-large, large, medium, small; amounts in the total's wire unit.
    pub net_amounts: [f64; 4],
    pub active_buy_net_amounts: [f64; 4],
    pub main_net: f64,
    /// Percentage points, not a fraction.
    pub main_ratio: f64,
    pub active_buy_net: f64,
    pub active_buy_ratio: f64,
}
#[derive(Debug, Clone, Serialize)]
pub struct MoneyFlowBlock {
    pub request: MoneyFlowRequest,
    pub market_raw: u8,
    pub code: String,
    pub reported_count_raw: u16,
    pub header_raw: Vec<u8>,
    pub page: CommandPage<MoneyFlowDaily>,
}
pub fn decode_money_flow(request: &MoneyFlowRequest, bytes: &[u8]) -> io::Result<MoneyFlowBlock> {
    bounded(bytes)?;
    let mut r = Reader::new(bytes);
    let header_raw = r.take(40)?.to_vec();
    let market_raw = header_raw[0];
    let mut identity = Reader::new(&header_raw[2..8]);
    let code = identity.code()?;
    if u16::from(market_raw) != request.symbol.market() || code != request.symbol.code() {
        return Err(invalid("0ffc证券错配"));
    }
    let reported_count_raw = u16::from_le_bytes([header_raw[38], header_raw[39]]);
    // Some service versions leave the count zero. Only accept an exact record
    // multiple in that case; never guess across instrument block boundaries.
    let n = if reported_count_raw == 0 {
        if !r.remaining().is_multiple_of(88) {
            return Err(invalid("0ffc空计数布局非法"));
        }
        r.remaining() / 88
    } else {
        usize::from(reported_count_raw)
    };
    if n > r.remaining() / 88 {
        return Err(invalid("0ffc数量超过正文"));
    }
    let mut rows = Vec::with_capacity(n);
    let mut dates = BTreeSet::new();
    for _ in 0..n {
        let date_raw = r.u32()?;
        date(date_raw).map_err(|_| invalid("0ffc交易日期非法"))?;
        if !dates.insert(date_raw) {
            return Err(invalid("0ffc日期重复"));
        }
        let mut raw = [0; 21];
        for field in &mut raw {
            *field = r.u32()?;
        }
        let total_amount = f32::from_bits(raw[1]);
        if !total_amount.is_finite() || total_amount < 0.0 {
            return Err(invalid("0ffc总金额非法"));
        }
        let mut buckets_raw = [0; 16];
        for (packed, pair) in raw[10..18].iter().zip(buckets_raw.chunks_exact_mut(2)) {
            pair[0] = *packed as u16;
            pair[1] = (*packed >> 16) as u16;
        }
        let differences: [f64; 8] = std::array::from_fn(|i| {
            f64::from(buckets_raw[2 * i]) - f64::from(buckets_raw[2 * i + 1])
        });
        let net_amounts =
            std::array::from_fn(|i| differences[2 * i] * f64::from(total_amount) / 50_000.0);
        let active_buy_net_amounts =
            std::array::from_fn(|i| differences[2 * i + 1] * f64::from(total_amount) / 50_000.0);
        let main = differences[0] + differences[2];
        let active: f64 = differences.iter().skip(1).step_by(2).sum();
        rows.push(MoneyFlowDaily {
            date_raw,
            raw,
            total_amount,
            total_amount_bits: total_amount.to_bits(),
            buckets_raw,
            net_amounts,
            active_buy_net_amounts,
            main_net: main * f64::from(total_amount) / 50_000.0,
            main_ratio: main / 500.0,
            active_buy_net: active * f64::from(total_amount) / 50_000.0,
            active_buy_ratio: active / 500.0,
        });
    }
    exact(&r)?;
    Ok(MoneyFlowBlock {
        request: request.clone(),
        market_raw,
        code,
        reported_count_raw,
        header_raw,
        page: CommandPage::new(MONEY_FLOW_COMMAND, rows, bytes),
    })
}
