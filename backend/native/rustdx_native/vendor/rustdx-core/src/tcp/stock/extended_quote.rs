//! Shared scalar layout; command-specific headers, depth and tails stay separate.
use super::{PrecisionSnapshot, PriceScale, command_request::*};
use crate::tcp::reader::Reader;
use serde::Serialize;
use std::io;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct QuoteLevel {
    pub price: Option<f64>,
    pub volume_raw: i64,
    pub price_delta_raw: i64,
}
#[derive(Debug, Clone, Serialize)]
pub struct BasicQuote {
    pub market_raw: u8,
    pub code: String,
    pub active_raw: u16,
    pub price: Option<f64>,
    pub previous_close: Option<f64>,
    pub open: Option<f64>,
    pub high: Option<f64>,
    pub low: Option<f64>,
    pub price_fields_raw: [i64; 5],
    pub time_raw: Option<i64>,
    pub update_cursor: Option<u32>,
    pub status_or_unknown_raw: i64,
    pub total_volume_raw: i64,
    pub current_volume_raw: i64,
    pub amount: f64,
    pub amount_raw: u32,
    pub inside_volume_raw: i64,
    pub outside_volume_raw: i64,
    pub after_outside_raw: i64,
    pub open_amount_raw: i64,
    pub bids: Vec<QuoteLevel>,
    pub asks: Vec<QuoteLevel>,
    /// None keeps raw prices usable when a category includes an instrument
    /// outside the supported precision directory (for example market 2).
    pub scale: Option<PriceScale>,
    pub tail_raw: Vec<u8>,
}

pub(crate) fn core(
    r: &mut Reader<'_>,
    metadata: &PrecisionSnapshot,
    refresh: bool,
    depth: usize,
    allow_unknown_precision: bool,
) -> io::Result<BasicQuote> {
    let market_raw = r.u8()?;
    let code = r.code()?;
    let active_raw = r.u16()?;
    let scale = match metadata.scale(u16::from(market_raw), &code) {
        Ok(scale) => Some(scale),
        Err(error)
            if allow_unknown_precision
                && matches!(
                    error.kind(),
                    io::ErrorKind::NotFound | io::ErrorKind::Unsupported
                ) =>
        {
            None
        }
        Err(error) => return Err(error),
    };
    let mut price_fields_raw = [0; 5];
    for field in &mut price_fields_raw {
        *field = r.varint64()?;
    }
    let base = price_fields_raw[0];
    let mut prices = [None; 5];
    for (i, p) in prices.iter_mut().enumerate() {
        let raw = if i == 0 {
            base
        } else {
            add(base, price_fields_raw[i])?
        };
        if raw < 0 {
            return Err(invalid("报价价格为负"));
        }
        *p = scale.map(|s| raw as f64 / f64::from(s.divisor()));
    }
    let (time_raw, update_cursor) = if refresh {
        (None, Some(r.u32()?))
    } else {
        (Some(r.varint64()?), None)
    };
    let status_or_unknown_raw = r.varint64()?;
    let total_volume_raw = r.varint64()?;
    let current_volume_raw = r.varint64()?;
    let amount_raw = r.u32()?;
    let amount = crate::tcp::helper::vol_amount(amount_raw as i32);
    let inside_volume_raw = r.varint64()?;
    let outside_volume_raw = r.varint64()?;
    let after_outside_raw = r.varint64()?;
    let open_amount_raw = r.varint64()?;
    if !amount.is_finite()
        || amount < 0.0
        || [
            total_volume_raw,
            current_volume_raw,
            inside_volume_raw,
            outside_volume_raw,
        ]
        .iter()
        .any(|v| *v < 0)
    {
        return Err(invalid("报价数量或金额非法"));
    }
    let mut bids = Vec::with_capacity(depth);
    let mut asks = Vec::with_capacity(depth);
    for _ in 0..depth {
        let bid = r.varint64()?;
        let ask = r.varint64()?;
        let bid_volume = r.varint64()?;
        let ask_volume = r.varint64()?;
        if bid_volume < 0 || ask_volume < 0 {
            return Err(invalid("盘口数量为负"));
        }
        let bid_raw = add(base, bid)?;
        let ask_raw = add(base, ask)?;
        if bid_raw < 0 || ask_raw < 0 {
            return Err(invalid("盘口价格为负"));
        }
        bids.push(QuoteLevel {
            price: scale.map(|s| bid_raw as f64 / f64::from(s.divisor())),
            volume_raw: bid_volume,
            price_delta_raw: bid,
        });
        asks.push(QuoteLevel {
            price: scale.map(|s| ask_raw as f64 / f64::from(s.divisor())),
            volume_raw: ask_volume,
            price_delta_raw: ask,
        });
    }
    Ok(BasicQuote {
        market_raw,
        code,
        active_raw,
        price: prices[0],
        previous_close: prices[1],
        open: prices[2],
        high: prices[3],
        low: prices[4],
        price_fields_raw,
        time_raw,
        update_cursor,
        status_or_unknown_raw,
        total_volume_raw,
        current_volume_raw,
        amount,
        amount_raw,
        inside_volume_raw,
        outside_volume_raw,
        after_outside_raw,
        open_amount_raw,
        bids,
        asks,
        scale,
        tail_raw: Vec::new(),
    })
}

/// Find a unique, structurally valid partition. A code marker in a varint or
/// opaque tail is not sufficient evidence of a new record. Work is bounded.
pub(crate) fn split(
    bytes: &[u8],
    n: usize,
    symbols: &[CommandSymbol],
    metadata: &PrecisionSnapshot,
    refresh: bool,
) -> io::Result<Vec<BasicQuote>> {
    if n == 0 {
        if !bytes.is_empty() {
            return Err(invalid("空报价带有额外正文"));
        }
        return Ok(Vec::new());
    }
    if n > symbols.len() || n > 100 || bytes.len() > 65535 {
        return Err(invalid("报价数量超过请求"));
    }
    let markers: Vec<_> = symbols.iter().map(CommandSymbol::marker).collect();
    let mut candidates = Vec::new();
    for (offset, window) in bytes.windows(7).enumerate() {
        if markers.iter().any(|m| window == m) {
            candidates.push(offset);
        }
        if candidates.len() > 512 {
            return Err(invalid("报价边界候选超过预算"));
        }
    }
    if candidates.first() != Some(&0) {
        return Err(invalid("报价首记录证券未请求"));
    }
    let mut search = Partition {
        bytes,
        candidates: &candidates,
        metadata,
        refresh,
        work: 0,
        solutions: Vec::new(),
    };
    search.walk(0, n, &mut Vec::new())?;
    if search.solutions.len() != 1 {
        return Err(invalid("报价记录边界缺失或歧义"));
    }
    search
        .solutions
        .pop()
        .ok_or_else(|| invalid("报价记录不存在"))
}
struct Partition<'a> {
    bytes: &'a [u8],
    candidates: &'a [usize],
    metadata: &'a PrecisionSnapshot,
    refresh: bool,
    work: usize,
    solutions: Vec<Vec<BasicQuote>>,
}
impl Partition<'_> {
    fn walk(&mut self, start: usize, left: usize, path: &mut Vec<BasicQuote>) -> io::Result<()> {
        self.work += 1;
        if self.work > 2048 {
            return Err(invalid("报价边界解析预算耗尽"));
        }
        crate::operation::check()?;
        let mut r = Reader::new(&self.bytes[start..]);
        let Ok(mut quote) = core(
            &mut r,
            self.metadata,
            self.refresh,
            if self.refresh { 5 } else { 1 },
            false,
        ) else {
            return Ok(());
        };
        if path
            .iter()
            .any(|p| p.market_raw == quote.market_raw && p.code == quote.code)
        {
            return Ok(());
        }
        let end = start + r.position();
        if left == 1 {
            quote.tail_raw = self.bytes[end..].to_vec();
            path.push(quote);
            self.solutions.push(path.clone());
            path.pop();
        } else {
            for i in 0..self.candidates.len() {
                let next = self.candidates[i];
                if next < end {
                    continue;
                }
                // Even if every remaining marker starts a record, this branch
                // cannot satisfy the declared count. Prune before recursing so
                // an ordinary 100-code page does not exhaust the work budget.
                if self.candidates.len() - i < left - 1 {
                    break;
                }
                quote.tail_raw = self.bytes[end..next].to_vec();
                path.push(quote.clone());
                self.walk(next, left - 1, path)?;
                path.pop();
                if self.solutions.len() > 1 {
                    break;
                }
            }
        }
        Ok(())
    }
}
