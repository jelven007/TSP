use super::{BasicQuote, PrecisionSnapshot, command_request::*};
use crate::tcp::{Tcp, reader::Reader};
use serde::Serialize;
use std::{io, sync::Arc};

pub const QUOTE_REFRESH_COMMAND: u16 = 0x0547;
pub const MAX_REFRESH_CODES: usize = 100;
#[derive(Debug, Clone, Serialize)]
pub struct RefreshCursor {
    pub symbol: CommandSymbol,
    pub cursor: u32,
}
#[derive(Debug, Clone, Serialize)]
pub struct RefreshRequest {
    items: Vec<RefreshCursor>,
}
impl RefreshRequest {
    pub fn new(items: Vec<RefreshCursor>) -> io::Result<Self> {
        let stocks: Vec<_> = items
            .iter()
            .map(|i| (i.symbol.market(), i.symbol.code()))
            .collect();
        symbols(&stocks, MAX_REFRESH_CODES)?;
        Ok(Self { items })
    }
    pub fn from_stocks(stocks: &[(u16, &str)]) -> io::Result<Self> {
        Self::new(
            symbols(stocks, MAX_REFRESH_CODES)?
                .into_iter()
                .map(|symbol| RefreshCursor { symbol, cursor: 0 })
                .collect(),
        )
    }
    pub fn items(&self) -> &[RefreshCursor] {
        &self.items
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = (self.items.len() as u16).to_le_bytes().to_vec();
        for item in &self.items {
            item.symbol.encode(&mut body, false);
            body.extend_from_slice(&item.cursor.to_le_bytes());
        }
        crate::tcp::frame::request(QUOTE_REFRESH_COMMAND, id, &body)
    }
    /// One-shot refresh uses an exclusive connection because 0547 can push
    /// frames after its response. Prefer QuoteSubscription for repeated use.
    pub fn recv(&self, tcp: Tcp, metadata: Arc<PrecisionSnapshot>) -> io::Result<QuoteRefresh> {
        let mut subscription = super::QuoteSubscription::from_tcp(
            tcp,
            self.clone(),
            metadata,
            super::SubscriptionOptions::default(),
        )?;
        subscription.refresh()
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct QuoteRefresh {
    pub requested: Vec<CommandSymbol>,
    pub page: CommandPage<BasicQuote>,
    /// After outer decompression, before/after the command's XOR transform.
    #[serde(skip)]
    pub decoded_payload: Arc<[u8]>,
}
pub fn decode_quote_refresh(
    request: &RefreshRequest,
    bytes: &[u8],
    metadata: &PrecisionSnapshot,
) -> io::Result<QuoteRefresh> {
    bounded(bytes)?;
    let decoded: Vec<_> = bytes.iter().map(|b| b ^ 0x93).collect();
    let mut r = Reader::new(&decoded);
    let n = usize::from(r.u16()?);
    let requested: Vec<_> = request.items.iter().map(|i| i.symbol.clone()).collect();
    let records =
        super::extended_quote::split(r.take(r.remaining())?, n, &requested, metadata, true)?;
    for quote in &records {
        let item = request
            .items
            .iter()
            .find(|item| {
                item.symbol.market() == u16::from(quote.market_raw)
                    && item.symbol.code() == quote.code
            })
            .ok_or_else(|| invalid("0547返回未请求证券"))?;
        let cursor = quote.update_cursor.ok_or_else(|| invalid("0547缺少游标"))?;
        if cursor < item.cursor {
            return Err(invalid("0547游标低于请求值，需重建"));
        }
    }
    let mut page = CommandPage::new(QUOTE_REFRESH_COMMAND, records, bytes);
    page.source.metadata_generation = Some(metadata.generation());
    Ok(QuoteRefresh {
        requested,
        page,
        decoded_payload: decoded.into(),
    })
}
