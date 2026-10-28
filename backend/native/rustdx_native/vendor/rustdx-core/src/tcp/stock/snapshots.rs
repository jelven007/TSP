use super::{BasicQuote, PrecisionSnapshot, command_request::*};
use crate::tcp::{Tcp, reader::Reader};
use serde::Serialize;
use std::io;

super::client::batch_budget::impl_heap!(SnapshotsRequest; symbols);

pub const SNAPSHOTS_COMMAND: u16 = 0x054c;
/// 80 is a conservative client batching policy, not a measured server limit.
pub const SNAPSHOT_BATCH_SIZE: usize = 80;
#[derive(Debug, Clone, Serialize)]
pub struct SnapshotsRequest {
    symbols: Vec<CommandSymbol>,
}
impl SnapshotsRequest {
    pub fn new(stocks: &[(u16, &str)]) -> io::Result<Self> {
        Ok(Self {
            symbols: symbols(stocks, SNAPSHOT_BATCH_SIZE)?,
        })
    }
    pub fn encode(&self, id: u32) -> io::Result<Vec<u8>> {
        let mut body = vec![5, 0, 0, 0, 0, 0, 0, 0];
        body.extend_from_slice(&(self.symbols.len() as u16).to_le_bytes());
        for symbol in &self.symbols {
            symbol.encode(&mut body, false);
        }
        crate::tcp::frame::request(SNAPSHOTS_COMMAND, id, &body)
    }
    pub fn recv(&self, tcp: &mut Tcp, metadata: &PrecisionSnapshot) -> io::Result<SnapshotPage> {
        for symbol in &self.symbols {
            if !metadata.is_fresh(symbol.market(), chrono::Utc::now()) {
                return Err(input("快照精度目录过期"));
            }
            metadata.scale(symbol.market(), symbol.code())?;
        }
        let bytes = tcp.exchange_checked(&self.encode(crate::tcp::frame::sequence())?)?;
        let mut page = decode_snapshots(self, &bytes, metadata)?;
        for symbol in &self.symbols {
            if !metadata.is_fresh(symbol.market(), chrono::Utc::now()) {
                return Err(invalid("快照请求跨日"));
            }
        }
        page.page.source.attach(tcp)?;
        Ok(page)
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct SnapshotPage {
    pub request: SnapshotsRequest,
    pub header_raw: u16,
    pub missing_codes: Vec<CommandSymbol>,
    pub page: CommandPage<BasicQuote>,
}
pub fn decode_snapshots(
    request: &SnapshotsRequest,
    bytes: &[u8],
    metadata: &PrecisionSnapshot,
) -> io::Result<SnapshotPage> {
    bounded(bytes)?;
    let mut r = Reader::new(bytes);
    let header_raw = r.u16()?;
    let n = usize::from(r.u16()?);
    let records =
        super::extended_quote::split(r.take(r.remaining())?, n, &request.symbols, metadata, false)?;
    let missing_codes = request
        .symbols
        .iter()
        .filter(|s| {
            !records
                .iter()
                .any(|q| u16::from(q.market_raw) == s.market() && q.code == s.code())
        })
        .cloned()
        .collect();
    let mut page = CommandPage::new(SNAPSHOTS_COMMAND, records, bytes);
    page.source.metadata_generation = Some(metadata.generation());
    Ok(SnapshotPage {
        request: request.clone(),
        header_raw,
        missing_codes,
        page,
    })
}
